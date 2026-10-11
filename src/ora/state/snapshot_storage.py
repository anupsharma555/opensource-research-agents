"""Internal, lossless storage for cumulative research snapshot projections.

Each ordered array is split into small immutable chunks. A snapshot stores a
manifest of exact component references; unchanged chunks are reused by hash.
Components are ordinary blob-owning records so backup and integrity scans see
their bytes without a second storage mechanism.
"""

from __future__ import annotations

import hashlib
import json

from .blobs import IntegrityFault
from .store import StateStore, Transaction, canonical

FORMAT = "ora.snapshot.components.v1"
CHUNK_SIZE = 64
COMPONENT_KIND = "snapshot_component"


def prepare_snapshot(store: StateStore, base: dict) -> tuple[dict, list[tuple[dict, dict]]]:
    """Install immutable bytes before acceptance; return manifest and components."""
    scalars, arrays, components = {}, {}, {}
    for name, value in base.items():
        if not isinstance(value, list):
            scalars[name] = value
            continue
        pointers = []
        for start in range(0, len(value), CHUNK_SIZE):
            chunk = value[start:start + CHUNK_SIZE]
            encoded = canonical(chunk).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            pointer = {"kind": COMPONENT_KIND, "id": f"snapshot-component-{digest}", "version": 1}
            pointers.append(pointer)
            if digest not in components:
                components[digest] = (pointer, {"blob": store.put_blob(encoded)})
        arrays[name] = pointers
    manifest = {"format": FORMAT, "scalars": scalars, "arrays": arrays}
    return store.put_blob(canonical(manifest).encode()), list(components.values())


def accept_components(tx, components: list[tuple[dict, dict]]) -> list[dict]:
    """Reuse exact records or create them in the snapshot acceptance transaction."""
    accepted = []
    for pointer, body in components:
        existing = tx.get(**pointer)
        if existing is None:
            existing = tx.put(COMPONENT_KIND, pointer["id"], body)
        elif existing["body"] != body:
            raise IntegrityFault("Snapshot component identity has different saved bytes")
        else:
            tx._verify_blob(body["blob"])
        accepted.append(existing)
    return accepted


def read_snapshot(store: StateStore, record: dict) -> dict:
    """Read a legacy full blob or reconstruct an exact ordered component view."""
    body = record["body"]
    encoded_manifest = store.read_blob(body["blob"]["sha256"])
    if body.get("storage_format") is not None and len(encoded_manifest) != body["blob"]["size"]:
        raise IntegrityFault("Saved snapshot manifest byte count changed")
    saved = json.loads(encoded_manifest)
    if body.get("storage_format") is None:
        return saved
    if body["storage_format"] != FORMAT or not isinstance(saved, dict) or saved.get("format") != FORMAT:
        raise IntegrityFault("Saved snapshot storage format is unavailable")
    if not isinstance(saved.get("scalars"), dict) or not isinstance(saved.get("arrays"), dict):
        raise IntegrityFault("Saved snapshot manifest is invalid")
    snapshot = dict(saved["scalars"])
    db = store._connect()
    try:
        reader = Transaction(store, db)
        for name, pointers in saved["arrays"].items():
            if name in snapshot or not isinstance(pointers, list):
                raise IntegrityFault("Saved snapshot array manifest is invalid")
            values = []
            for pointer in pointers:
                if not isinstance(pointer, dict) or set(pointer) != {"kind", "id", "version"} or pointer["kind"] != COMPONENT_KIND or pointer["version"] != 1:
                    raise IntegrityFault("Saved snapshot component reference is invalid")
                component = reader.get(**pointer)
                if component is None:
                    raise IntegrityFault("Saved snapshot component is unavailable")
                blob = component["body"].get("blob")
                if not isinstance(blob, dict) or set(blob) != {"sha256", "size"} or pointer["id"] != f"snapshot-component-{blob['sha256']}":
                    raise IntegrityFault("Saved snapshot component identity is invalid")
                encoded = store.read_blob(blob["sha256"])
                if len(encoded) != blob["size"]:
                    raise IntegrityFault("Saved snapshot component byte count changed")
                chunk = json.loads(encoded)
                if not isinstance(chunk, list) or len(chunk) > CHUNK_SIZE:
                    raise IntegrityFault("Saved snapshot component is not an array chunk")
                values.extend(chunk)
            snapshot[name] = values
    finally:
        db.close()
    return snapshot
