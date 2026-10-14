"""Recheck current use before an inspected scholarly hit is reused.

An exact retrieval hit is a discovery lead, not permission to recreate source
text. This predicate finds checked copies of the same hit and retained bytes,
then checks both the selected and current copy/use decisions in one reader.
"""

from __future__ import annotations

from ora.state import StateStore, Transaction
from ora.state.store import unpack


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _exact(pointer: object, kind: str) -> bool:
    return bool(
        type(pointer) is dict
        and set(pointer) == {"kind", "id", "version"}
        and pointer.get("kind") == kind
        and type(pointer.get("id")) is str and pointer["id"]
        and type(pointer.get("version")) is int and pointer["version"] > 0
    )


def _heads(reader: StateStore | Transaction, kind: str) -> list[dict]:
    if isinstance(reader, Transaction):
        return [unpack(row) for row in reader.db.execute(
            "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
            "WHERE r.kind=? ORDER BY r.id", (kind,),
        )]
    after, records = "", []
    while True:
        page = reader.records(kind, limit=500, after_id=after)
        records.extend(page)
        if len(page) < 500:
            return records
        after = page[-1]["id"]


def _explicit_prohibition(body: dict) -> bool:
    # Exact model/session prohibition fields, checked here without an evidence
    # dependency on the model bridge or mission-context module.
    use_record = body.get("use_record")
    return bool(
        body.get("allow_model_input") is False
        or body.get("allowed_for_model") is False
        or body.get("model_use") in {"restricted", "prohibited"}
        or body.get("rights_status") in {"restricted", "not_retained"}
        or (isinstance(use_record, dict)
            and (use_record.get("allow_model_input") is False
                 or use_record.get("allowed_for_model") is False
                 or use_record.get("use") in {"restricted", "prohibited"}))
    )


def _granted_use(reader: StateStore | Transaction, copy: dict) -> bool:
    pointer = _ref(copy)
    policy = reader.resolve("source_use.copy", f"{copy['id']}:{copy['version']}")
    body = policy["body"] if policy and policy.get("kind") == "source_use" else None
    return bool(
        body is not None
        and body.get("copy_ref") == pointer
        and body.get("policy_status") == "granted"
        and isinstance(body.get("allowed_uses"), list)
        and "model_input" in body["allowed_uses"]
        and not _explicit_prohibition(body)
    )


def _checked_copy_current_use(reader: StateStore | Transaction, copy: dict) -> bool:
    """Match WorkingContext._source_status's selected/current permit rule.

    The context view calls an exact copy permitted only when its bound selected
    policy and the current copy's bound policy both grant model input, with no
    explicit prohibition on either copy. This helper uses the same fields and
    deliberately treats missing/malformed policies as unavailable.
    """
    current = reader.get("source_copy", copy["id"])
    return bool(
        copy["body"].get("inspection_state") == "checked"
        and current is not None
        and not _explicit_prohibition(copy["body"])
        and not _explicit_prohibition(current["body"])
        and _granted_use(reader, copy)
        and _granted_use(reader, current)
    )


def _quality_identity(body: dict) -> tuple:
    return tuple(body.get(key) for key in (
        "source_sha256", "source_url", "source_version", "retrieved_at", "rights_basis",
    ))


def _copy_identity(body: dict) -> tuple:
    return tuple(body.get(key) for key in (
        "content_sha256", "reference", "source_version", "retrieved_at", "rights_basis",
    ))


def _same_hit_copy(reader: StateStore | Transaction, hit: dict,
                   copy: dict, digest: str) -> bool:
    if copy["body"].get("content_sha256") != digest:
        return False
    encounter_ref = copy["body"].get("encounter")
    encounter = reader.get(**encounter_ref) if _exact(encounter_ref, "source_encounter") else None
    return bool(encounter is not None and encounter["body"].get("retrieval_hit") == _ref(hit))


def _matching_copies(reader: StateStore | Transaction, hit: dict,
                     digest: str) -> list[dict]:
    pointer = _ref(hit)
    copies = {}
    for head in _heads(reader, "source_use"):
        for version in range(1, head["version"] + 1):
            use = reader.get("source_use", head["id"], version)
            if use is None or use["body"].get("retrieval_hit_ref") != pointer:
                continue
            copy_ref = use["body"].get("copy_ref")
            copy = reader.get(**copy_ref) if _exact(copy_ref, "source_copy") else None
            if copy is not None and copy["body"].get("content_sha256") == digest:
                copies[(copy["id"], copy["version"])] = copy
    for head in _heads(reader, "source_copy"):
        for version in range(1, head["version"] + 1):
            copy = reader.get("source_copy", head["id"], version)
            if copy is not None and _same_hit_copy(reader, hit, copy, digest):
                copies[(copy["id"], copy["version"])] = copy
    return list(copies.values())


def checked_hit_dependencies(
    reader: StateStore | Transaction, hit: dict, *,
    exclude_copy_ref: dict | None = None,
    exclude_quality_ref: dict | None = None,
) -> list[dict] | None:
    """Return permitted exact checked copies, or None when reinspection is unsafe.

    `exclude_copy_ref` and `exclude_quality_ref` are for intake's just-created
    copy before its first use grant. The exact quality record proves this
    inspection's hit/bytes/URL/version/time basis without mission-specific
    version naming. Intake must pass its local refs, never model input.
    """
    if (not isinstance(hit, dict) or hit.get("kind") != "retrieval_hit"
            or reader.get("retrieval_hit", hit.get("id")) != hit):
        return None
    digest = hit["body"].get("source", {}).get("content_sha256")
    if exclude_copy_ref is not None:
        excluded = reader.get(**exclude_copy_ref) if _exact(exclude_copy_ref, "source_copy") else None
        quality = reader.get(**exclude_quality_ref) if _exact(
            exclude_quality_ref, "intake_quality"
        ) else None
        if (not isinstance(digest, str) or not digest or excluded is None
                or quality is None
                or excluded["body"].get("inspection_state") != "checked"
                or quality["body"].get("retrieval_hit_ref") != _ref(hit)
                or quality["body"].get("source_sha256") != digest
                or quality["body"].get("classification")
                not in {"abstract", "passage", "partial", "full_text"}
                or _copy_identity(excluded["body"]) != _quality_identity(quality["body"])
                or reader.resolve("source_use.copy", f"{excluded['id']}:{excluded['version']}")
                is not None):
            return None
    elif exclude_quality_ref is not None:
        return None
    if not isinstance(digest, str) or not digest:
        return []
    dependencies = []
    for copy in _matching_copies(reader, hit, digest):
        if copy["body"].get("inspection_state") != "checked":
            continue
        if exclude_copy_ref == _ref(copy):
            continue
        bound = reader.resolve("source_use.copy", f"{copy['id']}:{copy['version']}")
        if bound is None:
            # Intake saves a checked copy before its separate use transaction.
            # A failed grant leaves that immutable copy without any model-use
            # authority. It must not poison later recovery after a prior use
            # is restored, and it never becomes a dependency or a grant here.
            current = reader.get("source_copy", copy["id"])
            if (_explicit_prohibition(copy["body"])
                    or current is not None and _explicit_prohibition(current["body"])):
                return None
            continue
        if not _checked_copy_current_use(reader, copy):
            return None
        dependencies.append(_ref(copy))
    return sorted(dependencies, key=lambda pointer: (pointer["id"], pointer["version"]))


def hit_allows_reinspection(
    reader: StateStore | Transaction, hit: dict, *,
    exclude_copy_ref: dict | None = None,
    exclude_quality_ref: dict | None = None,
) -> bool:
    """Check the same exact authority used by shared-hit metadata and intake."""
    return checked_hit_dependencies(
        reader, hit, exclude_copy_ref=exclude_copy_ref,
        exclude_quality_ref=exclude_quality_ref,
    ) is not None
