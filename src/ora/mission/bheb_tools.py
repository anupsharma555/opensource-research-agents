"""Bind optional BHEB search leads to the mission and checked source intake."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from ora.evidence import EvidenceError


def bheb_settings(project_root: Path) -> dict[str, str]:
    """Read only the two OSRA-owned BHEB settings, honoring environment overrides."""
    names = {"OSRA_BHEB_CONFIG", "OSRA_OPENAI_API_KEY"}
    values = {}
    try:
        lines = (project_root / ".env").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    for line in lines:
        name, separator, value = line.partition("=")
        name = name.strip().removeprefix("export ")
        if separator and name in names:
            values[name] = value.strip().strip("\"'")
    values.update({name: os.environ[name].strip() for name in names if name in os.environ})
    return values


def public_bheb_summary(summary: dict) -> dict:
    return {
        key: summary[key]
        for key in (
            "coverage",
            "source_count",
            "reason",
            "access_posture_counts",
            "filter_coverage_counts",
        )
        if key in summary
    }


def _scope(spec) -> dict[str, str]:
    return {
        "mission_id": spec.mission_id,
        "run_id": spec.run_id,
        "actor_id": spec.actor_id,
        "task_id": spec.proposal_ref["id"],
    }


def _available(host, spec) -> bool:
    run = host._run(spec.run_id)["body"]
    if host.bheb is None or run.get("bheb_corpus_enabled") is not True:
        return False
    summary = host.bheb.summary()
    return bool(
        summary.get("enabled") is True
        and summary.get("config_digest") == run.get("bheb_config_digest")
    )


def task_bound_bheb_copy(host, spec, copy_ref: dict) -> bool:
    binding = host.store.get(
        "bheb_source_binding", f"bheb-binding:{copy_ref['id']}:{copy_ref['version']}"
    )
    if binding is None:
        return False
    body = binding["body"]
    return bool(
        body.get("copy_ref") == copy_ref
        and body.get("mission_id") == spec.mission_id
        and body.get("run_id") == spec.run_id
        and body.get("actor_id") == spec.actor_id
        and body.get("task_id") == spec.proposal_ref["id"]
        and _available(host, spec)
        and host.bheb.current_source_use(
            body["source_id"], body["source_version"], body["upload_sha256"]
        )
    )


def current_bheb_use_for_intent(host, tx, intent: dict) -> bool:
    """Read current external policy immediately before a model dispatch marker."""
    for pointer in [
        *intent.get("source_refs", []),
        *intent.get("context_source_dependency_refs", []),
    ]:
        if pointer.get("kind") != "source_copy":
            continue
        binding = tx.get(
            "bheb_source_binding",
            f"bheb-binding:{pointer['id']}:{pointer['version']}",
        )
        if binding is None:
            continue
        body = binding["body"]
        if (
            body.get("copy_ref") != pointer
            or (body.get("mission_id") != intent.get("mission_id")
                and not _library_dependency_visible(host, tx, intent, pointer))
            or host.bheb is None
            or host.bheb.summary().get("config_digest") != body.get("config_digest")
            or not host.bheb.current_source_use(
                body["source_id"], body["source_version"], body["upload_sha256"]
            )
        ):
            return False
    return True


def _library_dependency_visible(host, tx, intent: dict, pointer: dict) -> bool:
    """Allow exact library selections or scoped metadata, never arbitrary copies."""
    from ora.mission.evidence_library import _binding_current

    actor = tx.get("agent_instance", intent.get("instance_id"), 1)
    if actor is None or actor["body"].get("mission_id") != intent.get("mission_id"):
        return False
    scope = SimpleNamespace(
        mission_id=intent["mission_id"], run_id=actor["body"]["run_id"],
        actor_id=actor["id"], proposal_ref={"id": intent["task_id"]},
    )
    if _binding_current(host, tx, scope, pointer):
        return True
    if pointer in intent.get("source_refs", []):
        return False  # Metadata discovery alone cannot admit source passages.
    rows = tx.db.execute(
        "SELECT r.body FROM records r JOIN heads h USING(kind,id,version) "
        "WHERE r.kind='context_tool_result' AND json_extract(r.body,'$.mission_id')=?", (scope.mission_id,)
    )
    for row in rows:
        result = json.loads(row["body"])
        response = tx.get(**result["response_ref"])
        call = tx.get(**response["body"]["call_ref"]) if response else None
        if (call is None or call["body"].get("instance_id") != scope.actor_id
                or call["body"].get("task_id") != scope.proposal_ref["id"]):
            continue
        payload = json.loads(host.store.read_blob(result["blob"]["sha256"]))
        if payload.get("name") != "search_evidence_library":
            continue
        content = json.loads(payload["content"])
        if pointer in content.get("source_dependency_refs", []):
            return True
    return False


def sync_bheb_source_uses(host) -> None:
    """Withdraw cached intake grants when the exact external source use disappears."""
    after_id = ""
    while True:
        bindings = host.store.records("bheb_source_binding", limit=500, after_id=after_id)
        if not bindings:
            break
        for binding in bindings:
            _sync_one_bheb_source_use(host, binding)
        after_id = bindings[-1]["id"]


def _sync_one_bheb_source_use(host, binding: dict) -> None:
    body = binding["body"]
    copy_ref = body["copy_ref"]
    allowed = bool(
        host.bheb is not None
        and host.bheb.summary().get("config_digest") == body.get("config_digest")
        and host.bheb.current_source_use(
            body["source_id"], body["source_version"], body["upload_sha256"]
        )
    )
    if allowed:
        return
    use = host.store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
    if use is None or use["body"].get("policy_status") != "granted":
        return

    def revoke(tx):
        current = tx.get("source_use", use["id"])
        if current is None or current["body"].get("policy_status") != "granted":
            return None
        return tx.put(
            "source_use",
            use["id"],
            {
                **current["body"],
                "allowed_uses": ["discovery"],
                "policy_status": "revoked",
                "revocation_reason": "bheb_current_use_unavailable",
            },
            expected_version=current["version"],
        )

    host.store.transact(
        f"bheb-revoke:{copy_ref['id']}:{copy_ref['version']}",
        {"copy_ref": copy_ref, "upload_sha256": body["upload_sha256"]},
        revoke,
        mission_id=body["mission_id"],
    )


def search_bheb(host, spec, args: dict) -> dict:
    if not _available(host, spec):
        return {"status": "unavailable", "reason": "corpus_binding_unavailable", "hits": []}
    capacity = host.capacity.get(host.executor._capacity_id(spec))
    # A new generation may dispatch only after a known rate rejection. Preserve
    # the previous attempt even for refresh=True if a recovery path invokes this
    # handler again after a completed or uncertain external effect.
    operation_id = f"{spec.work_id}:g{capacity['generation']}" if capacity else spec.work_id
    prior_ids = (
        [f"{spec.work_id}:g{generation}" for generation in range(capacity["generation"] - 1, 0, -1)]
        if capacity else []
    )
    for prior_id in [*prior_ids, spec.work_id]:
        prior = host.store.get("bheb_search", "bheb-search:" + prior_id)
        if prior is not None:
            if prior["body"]["phase"] != "waiting":
                operation_id = prior_id
            break
    result = host.bheb.search(
        _scope(spec),
        operation_id,
        args["query"],
        filters=args.get("filters"),
        limit=args.get("limit", 6),
        refresh=args.get("refresh", False),
    )
    summary = host.bheb.summary()
    result["corpus_summary"] = public_bheb_summary(summary)
    return result


def inspect_bheb(host, spec, args: dict) -> tuple[dict, tuple[dict, ...]]:
    if not _available(host, spec):
        return {"status": "source_unavailable", "reason": "corpus_binding_unavailable"}, ()
    checked = host.bheb.inspect(_scope(spec), args["hit_id"], args["search_operation_id"])
    if checked.get("status") != "source_available":
        return checked, ()

    # The adapter may offer exact source bytes only after manifest/hash and
    # explicit model-use checks. Never put the returned text straight in a model
    # result: intake must classify it and create a current source-use record.
    text = checked.get("source_text")
    if (
        not isinstance(text, str)
        or not text
        or checked.get("rights_status") != "permitted"
        or not isinstance(checked.get("rights_basis"), str)
        or not checked["rights_basis"].strip()
        or not isinstance(checked.get("source_sha256"), str)
        or hashlib.sha256(text.encode("utf-8")).hexdigest() != checked["source_sha256"]
    ):
        return {
            "status": "source_unavailable",
            "reason": "source_verification_failed",
            "hit_id": args["hit_id"],
        }, ()
    required = (
        "title",
        "source_url",
        "source_version",
        "retrieved_at",
        "declared_surface",
        "release_manifest_checksum_sha256",
        "publication_receipt_checksum_sha256",
    )
    if any(not isinstance(checked.get(key), str) or not checked[key].strip() for key in required):
        return {
            "status": "source_unavailable",
            "reason": "source_identity_incomplete",
            "hit_id": args["hit_id"],
        }, ()
    if checked["declared_surface"] not in {"abstract", "passage", "full_text"}:
        return {
            "status": "source_unavailable",
            "reason": "source_surface_unsupported",
            "hit_id": args["hit_id"],
        }, ()
    matched_text = checked.get("matched_text")
    exact_match = bool(
        isinstance(matched_text, str)
        and 0 < len(matched_text) <= 5000
        and text.count(matched_text) == 1
    )
    try:
        identity_basis = (
            "Exact BHEB source manifest and publication receipt checksums "
            f"{checked['release_manifest_checksum_sha256']} / "
            f"{checked['publication_receipt_checksum_sha256']}; "
            "adapter verified file version, body hash, and explicit current source-use policy"
        )
        identifiers = {}
        doi = checked.get("doi")
        if isinstance(doi, str) and doi.startswith("10."):
            identifiers["doi"] = doi
        publication = host.evidence.register_publication(
            f"mission-bheb-publication:{spec.work_id}",
            identifiers=identifiers,
            title=checked["title"],
            verification=identity_basis,
        )
        if publication.get("publication_ref") is None:
            return {
                "status": "source_unavailable",
                "reason": "publication_identity_ambiguous",
                "hit_id": args["hit_id"],
            }, ()
        result = host.intake.inspect_response(
            f"mission-bheb-intake:{spec.work_id}",
            publication["publication_ref"],
            content=text.encode("utf-8"),
            content_type="text/plain",
            source_url=checked["source_url"],
            retrieved_at=checked["retrieved_at"],
            source_version=checked["source_version"],
            rights_status="permitted",
            rights_basis=checked["rights_basis"],
            declared_surface=checked["declared_surface"],
            expected_doi=doi if "doi" in identifiers else None,
            selected_texts=[matched_text] if exact_match else [],
            identity_basis=identity_basis,
        )
    except EvidenceError:
        return {
            "status": "source_unavailable",
            "reason": "intake_rejected_source",
            "hit_id": args["hit_id"],
        }, ()
    copy_ref = result.get("copy_ref")
    use_ref = result.get("source_use_ref")
    passage_refs = result.get("passage_refs") or []
    if copy_ref is None or use_ref is None:
        return {
            "status": "source_unavailable",
            "reason": result.get("reason", "no_checked_passage"),
            "classification": result.get("classification"),
            "hit_id": args["hit_id"],
        }, ()
    binding_body = {
        "schema": "ora.bheb.source_binding.v1",
        "mission_id": spec.mission_id,
        "run_id": spec.run_id,
        "actor_id": spec.actor_id,
        "task_id": spec.proposal_ref["id"],
        "copy_ref": copy_ref,
        "source_id": checked["source_id"],
        "source_version": checked["source_version"],
        "upload_sha256": checked["upload_sha256"],
        "publication_receipt_checksum_sha256": checked["publication_receipt_checksum_sha256"],
        "config_digest": host.bheb.summary().get("config_digest"),
    }
    host.store.transact(
        f"mission-bheb-bind:{spec.work_id}",
        binding_body,
        lambda tx: tx.put(
            "bheb_source_binding",
            f"bheb-binding:{copy_ref['id']}:{copy_ref['version']}",
            binding_body,
        ),
        mission_id=spec.mission_id,
    )
    sync_bheb_source_uses(host)
    try:
        current_use = host.intake._current_model_use(copy_ref)
    except EvidenceError:
        current_use = None
    if current_use != use_ref:
        return {
            "status": "source_unavailable",
            "reason": "current_use_changed",
            "hit_id": args["hit_id"],
        }, ()
    if not passage_refs:
        return {
            "status": "source_ready_for_read",
            "hit_id": args["hit_id"],
            "classification": result["classification"],
            "source_id": copy_ref["id"],
            "source_version": copy_ref["version"],
            "reason": "search_excerpt_did_not_identify_one_exact_source_passage",
            "next_action": "Use read_source_window to select and inspect an exact passage.",
        }, ()
    return {
        "status": "inspected",
        "hit_id": args["hit_id"],
        "classification": result["classification"],
        "source_id": copy_ref["id"],
        "source_version": copy_ref["version"],
        "passages": [
            {
                "span_id": host.store.get(**pointer)["body"]["span_id"],
                "locator": host.store.get(**pointer)["body"]["locator"],
            }
            for pointer in passage_refs
        ],
        "next_action": "Use read_source_window for further checked text and cite exact source/span IDs.",
    }, (copy_ref,)
