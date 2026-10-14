"""Registered skill material is readable only for an active, pinned role skill."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ora.mission.skill_activation import pin_catalog
from ora.mission.skill_resources import read_skill_resource
from ora.state import StateError
from tests.mission.test_service import ROOT

CRITIC = "independent_methods_reviewer_critic"
SYNTHESIS = "evidence_preparation_synthesis"


def _copy_package(tmp_path: Path) -> Path:
    for relative in ("runtime_skills", "roles", "src/ora/skills"):
        shutil.copytree(ROOT / relative, tmp_path / relative)
    return tmp_path


def _read(root: Path, role_id: str, skill_id: str, resource: str, *,
          active: list[str] | None = None, pins: dict | None = None, **window) -> dict:
    role_pins = pins if pins is not None else pin_catalog(root)[role_id]
    return read_skill_resource(
        root, role_id=role_id, pins=role_pins,
        active_skill_ids=active if active is not None else [],
        skill_id=skill_id, resource=resource, **window,
    )


@pytest.mark.parametrize("resource", ["rich_instructions", "example", "helper_source"])
def test_registered_resources_reconstruct_from_exact_pages(resource):
    pages = []
    window = {"limit": 97}
    while True:
        page = _read(ROOT, CRITIC, "study-appraisal", resource,
                     active=["study-appraisal"], **window)
        pages.append(page)
        assert len(page["text"]) <= 97
        if page["next_request"] is None:
            break
        window = {key: value for key, value in page["next_request"].items()
                  if key != "tool"}
        window.pop("skill_id")
        window.pop("resource")
    text = "".join(page["text"] for page in pages)
    assert len(text) == pages[-1]["total_chars"]
    assert all(page["skill_version"] == "1.0.3" for page in pages)
    assert all(page["content_sha256"] == pages[0]["content_sha256"] for page in pages)
    if resource == "example":
        assert json.loads(text)["synthetic"] is True
        assert pages[0]["metadata"] == {"synthetic": True}
        assert "not fresh source evidence" in pages[0]["qualification"]
    elif resource == "helper_source":
        assert "def prepare_result_appraisal" in text
        assert pages[0]["metadata"]["function"] == "prepare_result_appraisal"
        assert pages[0]["metadata"]["module"] == "ora.skills.appraisal"
        assert pages[0]["metadata"]["dependencies"] == ["pydantic==2.13.5"]
        assert "not execution" in pages[0]["qualification"]
    else:
        assert "# Study appraisal" in text
        assert "not evidence" in pages[0]["qualification"]


def test_inactive_optional_and_foreign_role_skill_are_denied():
    with pytest.raises(StateError, match="active"):
        _read(ROOT, CRITIC, "study-appraisal", "rich_instructions")
    with pytest.raises(StateError, match="role-granted"):
        _read(ROOT, SYNTHESIS, "study-appraisal", "example",
              active=["study-appraisal"])
    # A mandatory role-granted skill is active under the existing lifecycle rule.
    assert _read(ROOT, SYNTHESIS, "evidence-linking", "example")["text"]


@pytest.mark.parametrize("resource,relative", [
    ("rich_instructions", "runtime_skills/evidence_linking/RICH.md"),
    ("example", "runtime_skills/evidence_linking/example.json"),
    ("helper_source", "src/ora/skills/evidence_links.py"),
])
def test_changed_resource_bytes_are_denied(tmp_path, resource, relative):
    root = _copy_package(tmp_path / "package")
    pins = pin_catalog(root)[SYNTHESIS]
    first = _read(root, SYNTHESIS, "evidence-linking", resource, pins=pins,
                  limit=40)
    target = root / relative
    target.write_bytes(target.read_bytes() + b"\nchanged")
    with pytest.raises((StateError, ValueError), match="hash|changed|unavailable"):
        _read(root, SYNTHESIS, "evidence-linking", resource, pins=pins,
              offset=first["end_offset"], content_sha256=first["content_sha256"])


def test_symlink_escape_and_manifest_path_traversal_are_denied(tmp_path):
    root = _copy_package(tmp_path / "package")
    pins = pin_catalog(root)[SYNTHESIS]
    target = root / "runtime_skills/evidence_linking/RICH.md"
    outside = tmp_path / "outside.md"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises((StateError, ValueError), match="escapes|unavailable"):
        _read(root, SYNTHESIS, "evidence-linking", "rich_instructions", pins=pins)
    target.unlink()
    target.write_bytes(outside.read_bytes())
    manifest_path = root / "runtime_skills/evidence_linking/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["instruction_variants"]["rich"]["path"] = "../../outside.md"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises((StateError, ValueError), match="escapes|changed|unavailable"):
        _read(root, SYNTHESIS, "evidence-linking", "rich_instructions", pins=pins)


def test_unknown_resource_and_changed_continuation_hash_are_denied():
    with pytest.raises(StateError, match="Unknown skill resource"):
        _read(ROOT, SYNTHESIS, "evidence-linking", "../../.env")
    first = _read(ROOT, SYNTHESIS, "evidence-linking", "helper_source", limit=40)
    with pytest.raises(StateError, match="changed"):
        _read(ROOT, SYNTHESIS, "evidence-linking", "helper_source",
              offset=first["end_offset"], content_sha256="0" * 64)
