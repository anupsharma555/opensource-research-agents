"""Offline role/skill package behavior and failure isolation."""

from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ora.contracts.models import EvidencePacket
from ora.skills import (
    RegistryError,
    SkillUnavailable,
    load_registry,
    prepare_claim_link,
    prepare_result_appraisal,
)

ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES = {"source_read_by_id", "study_appraisal"}
ROLE_NAMES = [
    "Literature acquisition",
    "Evidence preparation and synthesis",
    "Independent methods reviewer and critic",
    "Questions, competing hypotheses and ranking",
    "Experimental design",
    "Data management",
    "Analysis",
    "Project and collaboration planning",
    "Shared research memory and completion reporting",
]


def copied_package(directory: str) -> Path:
    root = Path(directory)
    shutil.copytree(ROOT / "runtime_skills", root / "runtime_skills")
    shutil.copytree(ROOT / "roles", root / "roles")
    (root / "src" / "ora").mkdir(parents=True)
    shutil.copytree(ROOT / "src" / "ora" / "skills", root / "src" / "ora" / "skills")
    return root


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def refresh_roles_hash(root: Path) -> None:
    index_path = root / "runtime_skills" / "registry.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["roles"]["sha256"] = hashlib.sha256((root / "roles" / "roles.json").read_bytes()).hexdigest()
    write_json(index_path, index)


class RuntimeSkillTests(unittest.TestCase):
    def test_live_plan_role_names_and_shared_skill_identity(self) -> None:
        registry = load_registry(ROOT, CAPABILITIES)
        self.assertEqual([role["name"] for role in registry.roles.values()], ROLE_NAMES)
        self.assertEqual(len(registry.skills), 2)
        a = registry.start_skill_attempt("evidence_preparation_synthesis", "evidence-linking", "A")
        b = registry.start_skill_attempt("independent_methods_reviewer_critic", "evidence-linking", "B")
        self.assertEqual((a.skill_id, a.skill_version, a.helper_sha256), (b.skill_id, b.skill_version, b.helper_sha256))
        self.assertEqual(registry.start_skill_attempt("independent_methods_reviewer_critic", "study-appraisal", "C").skill_id, "study-appraisal")

    def test_project_owned_helpers_use_canonical_evidence_packet_and_do_not_certify_meaning(self) -> None:
        shared = json.loads((ROOT / "runtime_skills" / "evidence_linking" / "example.json").read_text(encoding="utf-8"))
        source = EvidencePacket.model_validate(shared["input"]["evidence"])
        result = prepare_claim_link(source, claim=shared["input"]["claim"], locator=shared["input"]["locator"], relation="supports")
        self.assertEqual(result, shared["expected_helper_output"])
        self.assertEqual(result["semantic_support_status"], "pending_independent_review")
        with self.assertRaisesRegex(ValueError, "locator"):
            prepare_claim_link(source, claim="Unrelated claim", locator="Invented page", relation="supports")
        with self.assertRaises(TypeError):
            prepare_claim_link(shared["input"]["evidence"], claim="Claim", locator="Abstract, sentence 3", relation="supports")

    def test_specialist_record_keeps_four_judgments_and_exact_result_scope(self) -> None:
        example = json.loads((ROOT / "runtime_skills" / "study_appraisal" / "example.json").read_text(encoding="utf-8"))
        values = copy.deepcopy(example["input"])
        evidence = EvidencePacket.model_validate(values.pop("evidence"))
        record = prepare_result_appraisal(evidence, **values)
        self.assertEqual(record["scientific_validity_status"], example["expected_status"])
        self.assertEqual(record["artifact_sha256"], evidence.artifact.sha256)
        self.assertEqual(set(record["judgments"]), {"reporting_completeness", "risk_of_bias", "applicability", "precision"})
        self.assertIn("unknown", record["judgments"]["risk_of_bias"])
        values["judgments"].pop("precision")
        with self.assertRaisesRegex(ValueError, "separate reporting"):
            prepare_result_appraisal(evidence, **values)

    def test_parallel_role_attempts_do_not_share_mutable_state(self) -> None:
        registry = load_registry(ROOT, CAPABILITIES)
        def work(role_id: str, attempt_id: str) -> dict:
            attempt = registry.start_skill_attempt(role_id, "evidence-linking", attempt_id)
            attempt.state["draft"] = [attempt_id]
            return {"role": attempt.role_id, "attempt": attempt.attempt_id, "state": attempt.state}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda args: work(*args), [
                ("evidence_preparation_synthesis", "attempt-1"),
                ("independent_methods_reviewer_critic", "attempt-2"),
            ]))
        self.assertEqual(first["state"], {"draft": ["attempt-1"]})
        self.assertEqual(second["state"], {"draft": ["attempt-2"]})
        self.assertIsNot(first["state"], second["state"])

    def test_missing_required_file_and_duplicate_skill_id_report_exact_fault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = copied_package(directory)
            (root / "runtime_skills" / "evidence_linking" / "CONCISE.md").unlink()
            with self.assertRaisesRegex(RegistryError, "evidence-linking concise instructions"):
                load_registry(root, CAPABILITIES)
        with tempfile.TemporaryDirectory() as directory:
            root = copied_package(directory)
            index_path = root / "runtime_skills" / "registry.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["skills"].append(copy.deepcopy(index["skills"][0]))
            write_json(index_path, index)
            with self.assertRaisesRegex(RegistryError, "Duplicate runtime skill IDs"):
                load_registry(root, CAPABILITIES)

    def test_incompatible_role_version_is_reported_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = copied_package(directory)
            roles_path = root / "roles" / "roles.json"
            roles = json.loads(roles_path.read_text(encoding="utf-8"))
            roles["roles"][1]["skill_refs"][0]["version"] = "2.0.0"
            write_json(roles_path, roles)
            refresh_roles_hash(root)
            with self.assertRaisesRegex(RegistryError, "incompatible with role request 2.0.0"):
                load_registry(root, CAPABILITIES)

    def test_optional_specialist_missing_package_or_capability_is_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = copied_package(directory)
            (root / "runtime_skills" / "study_appraisal" / "manifest.json").unlink()
            registry = load_registry(root, CAPABILITIES)
            self.assertIn("Optional study-appraisal unavailable", registry.unavailable["study-appraisal"])
            registry.start_skill_attempt("evidence_preparation_synthesis", "evidence-linking", "still-works")
            with self.assertRaises(SkillUnavailable):
                registry.start_skill_attempt("independent_methods_reviewer_critic", "study-appraisal", "unavailable")
        registry = load_registry(ROOT, {"source_read_by_id"})
        self.assertIn("needs capability: study_appraisal", registry.unavailable["study-appraisal"])
        registry.start_skill_attempt("independent_methods_reviewer_critic", "evidence-linking", "shared-still-works")

    def test_provenance_hash_dependency_and_instruction_variants(self) -> None:
        registry = load_registry(ROOT, CAPABILITIES)
        short = registry.render_instructions("evidence-linking", "concise")
        rich = registry.render_instructions("evidence-linking", "rich")
        self.assertLess(len(short), len(rich))
        self.assertIn("pending", rich)
        with tempfile.TemporaryDirectory() as directory:
            root = copied_package(directory)
            (root / "src" / "ora" / "skills" / "evidence_links.py").write_text("# modified helper\n", encoding="utf-8")
            with self.assertRaisesRegex(RegistryError, "helper content hash differs"):
                load_registry(root, CAPABILITIES)
        with tempfile.TemporaryDirectory() as directory:
            root = copied_package(directory)
            path = root / "runtime_skills" / "evidence_linking" / "manifest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["helper"].pop("license")
            write_json(path, manifest)
            with self.assertRaisesRegex(RegistryError, "incomplete helper provenance"):
                load_registry(root, CAPABILITIES)
        with tempfile.TemporaryDirectory() as directory:
            root = copied_package(directory)
            path = root / "runtime_skills" / "evidence_linking" / "manifest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["helper"]["dependencies"] = ["pydantic==0.0.0"]
            write_json(path, manifest)
            with self.assertRaisesRegex(RegistryError, "requires pydantic==0.0.0"):
                load_registry(root, CAPABILITIES)

    def test_offline_qualification_packet_tracks_actual_role_skill_versions(self) -> None:
        registry = load_registry(ROOT, CAPABILITIES)
        packet = json.loads((ROOT / "runtime_skills" / "qualification" / "packet.json").read_text(encoding="utf-8"))
        self.assertEqual(packet["proof_type"], "offline_fixture")
        self.assertTrue((ROOT / packet["source"]).is_file())
        self.assertEqual(packet["future_model_comparison"]["run_status"], "not_run")
        for case in packet["role_skill_cases"]:
            for variant in case["variants"]:
                self.assertTrue(registry.render_instructions(case["skill_id"], variant).strip())
            attempt = registry.start_skill_attempt(case["role_id"], case["skill_id"], "qualification-fixture")
            self.assertEqual(attempt.skill_version, "1.0.0")


if __name__ == "__main__":
    unittest.main()
