"""A peer discovers exact sibling pointers without gaining source or report text."""

from __future__ import annotations

import asyncio
import json

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.evidence.hit_use import checked_hit_dependencies
from ora.mission.context import WorkingContext
from ora.mission.shared_materials import (
    shared_hit_access,
    shared_material_entries,
    shared_material_entry,
    shared_material_view,
)
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import _search_spec, service, tool


def test_sibling_hit_pointer_obeys_exact_hit_and_current_copy_use(tmp_path, monkeypatch):
    def no_model(*_args, **_kwargs):
        raise AssertionError("No model dispatch is needed")

    host = service(tmp_path, monkeypatch, no_model)
    found_ids = iter(("123", "456"))

    def discovery(*_args, **_kwargs):
        source_id = next(found_ids)
        return HTTPResult(200, json.dumps({
            "hitCount": 1, "nextCursorMark": "*",
            "resultList": {"result": [{
                "source": "MED", "id": source_id, "pmid": source_id,
                "title": f"Synthetic abstract {source_id}",
                "abstractText": "A permitted synthetic abstract with a checked passage. " * 7,
            }]},
        }).encode(), {})

    host.discovery_transport = discovery
    try:
        run = host.start(LiveStartRequest(
            request_id="shared-hit-pointers", question="Review synthetic results",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        origin = host._control(run.run_id, "agent-synthesis")["body"]
        reader = host._control(run.run_id, "agent-critique")["body"]
        scope = {"mission_id": origin["mission_id"], "actor_id": reader["actor_id"],
                 "task_id": reader["task_id"]}
        first, _ = host._search(_search_spec(host, run.run_id, suffix="first"), {
            "route": "europe_pmc.search", "query": "synthetic first", "limit": 1,
        })
        hit_id = first["hits"][0]["hit_id"]
        hit = host.store.get("retrieval_hit", hit_id)
        entry = shared_material_entry(host.store, pointer=ref(hit), **scope)
        assert entry is not None
        assert entry["type"] == "shared_scholarly_hit"
        assert entry["fetch"] == {"tool": "inspect_hit", "hit_id": hit_id}
        assert entry["origin_actor_id"] == origin["actor_id"]
        assert entry["origin_display_name"] == host.store.get(
            "agent_instance", origin["actor_id"], 1
        )["body"]["display_name"]
        assert entry["source_dependency_refs"] == []
        assert "source_dependency_refs" not in shared_material_view(entry)
        assert shared_material_view(entry)["fetch"] == entry["fetch"]
        assert "abstractText" not in json.dumps(entry)
        assert shared_material_entry(
            host.store, pointer=ref(hit), mission_id="another-mission",
            actor_id=reader["actor_id"], task_id=reader["task_id"],
        ) is None
        browser_ref = host.store.transact(
            "synthetic-browser-hit-for-index", {},
            lambda tx: ref(tx.put("retrieval_hit", "synthetic-browser-hit-for-index", {
                **hit["body"], "route": "groq.browser_search.url_leads",
            })),
            mission_id=origin["mission_id"],
        )
        assert shared_material_entry(host.store, pointer=browser_ref, **scope) is None

        inspected, _ = host._inspect_hit(
            _search_spec(host, run.run_id, suffix="inspect"), {"hit_id": hit_id},
        )
        assert inspected["status"] == "inspected"
        inspected_copy = {"kind": "source_copy", "id": inspected["source_id"],
                          "version": inspected["source_version"]}
        status = WorkingContext(host.store)._source_status(host.store.get(**inspected_copy))
        assert status["model_input_status"] == "permitted"
        assert status["current_model_input_status"] == "permitted"
        assert shared_material_entry(host.store, pointer=ref(hit), **scope)[
            "source_dependency_refs"
        ] == [inspected_copy]
        assert shared_material_view(shared_material_entry(
            host.store, pointer=ref(hit), **scope,
        )) == shared_material_view(entry)
        in_tx = host.store.transact(
            "shared-hit-transactional-read", {},
            lambda tx: {"entry": shared_material_entry(tx, pointer=ref(hit), **scope)},
            mission_id=origin["mission_id"],
        )["entry"]
        assert in_tx["source_dependency_refs"] == [inspected_copy]
        first_copy = host.store.get(**inspected_copy)
        first_use = host.store.resolve(
            "source_use.copy", f"{inspected_copy['id']}:{inspected_copy['version']}"
        )
        first_quality = host.store.get(**first_use["body"]["quality_ref"])
        generic_version = "synthetic-client-independent-version"
        new_quality = host.store.transact(
            "same-hit-new-quality-before-use", {},
            lambda tx: ref(tx.put("intake_quality", "same-hit-new-quality-before-use", {
                **first_quality["body"], "source_version": generic_version,
            })),
            mission_id=origin["mission_id"],
        )
        new_copy = host.store.transact(
            "same-hit-new-copy-before-use", {},
            lambda tx: ref(tx.put("source_copy", "same-hit-new-copy-before-use", {
                **first_copy["body"], "source_version": generic_version,
            })),
            mission_id=origin["mission_id"],
        )
        assert checked_hit_dependencies(host.store, hit) == [inspected_copy]
        # A second checked intake candidate without any use alias is not a
        # grant or a permanent veto while the earlier use is still permitted.
        assert checked_hit_dependencies(
            host.store, hit, exclude_copy_ref=inspected_copy,
        ) is None  # An already-granted copy cannot be hidden as the new copy.
        assert host.store.transact(
            "same-hit-new-copy-use-fence", {},
            lambda tx: {"dependencies": checked_hit_dependencies(
                tx, hit, exclude_copy_ref=new_copy, exclude_quality_ref=new_quality,
            )},
            mission_id=origin["mission_id"],
        )["dependencies"] == [inspected_copy]

        def grant_new(tx):
            use = tx.put("source_use", "same-hit-new-copy-use", {
                **first_use["body"], "copy_ref": new_copy,
                "quality_ref": new_quality,
            })
            tx.bind("source_use.copy", f"{new_copy['id']}:{new_copy['version']}",
                    "source_use", use["id"])
            return ref(use)

        host.store.transact("same-hit-grant-new-copy", {}, grant_new,
                            mission_id=origin["mission_id"])
        assert checked_hit_dependencies(
            host.store, hit, exclude_copy_ref=new_copy,
            exclude_quality_ref=new_quality,
        ) is None  # Exclusion is only valid before the first use grant.
        use = host.store.resolve(
            "source_use.copy", f"{inspected['source_id']}:{inspected['source_version']}"
        )

        def revoke(tx):
            previous = tx.get(**ref(use))
            return ref(tx.put("source_use", previous["id"], {
                **previous["body"], "allowed_uses": [], "policy_status": "revoked",
            }, expected_version=previous["version"]))

        host.store.transact("revoke-shared-hit", {}, revoke,
                            mission_id=origin["mission_id"])
        status = WorkingContext(host.store)._source_status(host.store.get(**inspected_copy))
        assert status["model_input_status"] == "restricted"
        assert status["current_model_input_status"] == "restricted"
        assert not shared_hit_access(host.store, hit)
        assert shared_material_entry(host.store, pointer=ref(hit), **scope) is None
        assert host.store.transact(
            "shared-hit-transactional-revocation-read", {},
            lambda tx: {"entry": shared_material_entry(tx, pointer=ref(hit), **scope)},
            mission_id=origin["mission_id"],
        )["entry"] is None
        independent_same_bytes = host.store.transact(
            "independent-same-bytes-hit", {},
            lambda tx: ref(tx.put("retrieval_hit", "independent-same-bytes-hit", {
                **hit["body"], "request_id": "independent-same-bytes-acquisition",
            })),
            mission_id=origin["mission_id"],
        )
        assert shared_material_entry(
            host.store, pointer=independent_same_bytes, **scope,
        ) is not None  # Revocation is exact-hit scoped, even with identical bytes.

        second, _ = host._search(_search_spec(host, run.run_id, suffix="second"), {
            "route": "europe_pmc.search", "query": "synthetic independent", "limit": 1,
        })
        second_hit = host.store.get("retrieval_hit", second["hits"][0]["hit_id"])
        entries = shared_material_entries(host.store, **scope)
        assert ref(hit) not in [item["ref"] for item in entries]
        assert ref(second_hit) in [item["ref"] for item in entries]
    finally:
        host.close()


def test_accepted_sibling_report_pointer_withholds_title_after_revoke(
    tmp_path, monkeypatch,
):
    def transport(*_args, **_kwargs):
        return tool("publish_result", {
            "title": "Synthetic note review",
            "text": "A synthetic working-note finding with a distinct source ancestor.",
            "citations": [], "limitations": ["Synthetic note only."],
        }, 1)

    host = service(tmp_path, monkeypatch, transport)
    host.credential_profiles.is_run_enabled = lambda _profile, _run: True
    host.session.adapter.credential_profiles.is_enabled = lambda _profile: True
    try:
        run = host.start(LiveStartRequest(
            request_id="shared-report-siblings", question="Review a synthetic note",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        origin = host._control(run.run_id, "agent-synthesis")["body"]
        source_text = b"Synthetic local text behind a permission-linked working note."
        blob = host.store.put_blob(source_text)

        def source(tx):
            publication = tx.put("publication", "shared-report-publication", {
                "title": "Synthetic local text",
            })
            encounter = tx.put("source_encounter", "shared-report-encounter", {
                "publication": ref(publication), "route": "user_supplied",
                "status": "accepted",
            })
            copy = tx.put("source_copy", "shared-report-copy", {
                "mission_id": origin["mission_id"], "rights_status": "permitted",
                "publication": ref(publication), "encounter": ref(encounter),
                "mime_type": "text/plain", "blob": blob,
                "content_sha256": blob["sha256"], "retain_bytes": True,
                "inspection_state": "checked", "validation_record": "synthetic-local-check",
                "passage_ids": [],
            })
            use = tx.put("source_use", "shared-report-use", {
                "copy_ref": ref(copy), "allowed_uses": ["model_input"],
                "policy_status": "granted",
            })
            tx.bind("source_use.copy", "shared-report-copy:1", "source_use", use["id"])
            return {"copy": ref(copy), "use": ref(use)}

        saved = host.store.transact("shared-report-source", {}, source,
                                    mission_id=origin["mission_id"])
        host.notes.write(
            "shared-report-note", mission_id=origin["mission_id"],
            actor_id=origin["actor_id"], task_ref=origin["proposal_ref"],
            note_id="shared-report-note", category="hypothesis",
            text="A synthetic working-note finding with a distinct source ancestor.",
            input_refs=[saved["copy"]],
        )
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        asyncio.run(host.executor.run_ready(origin["mission_id"]))
        assert host._advance(host._control(run.run_id, "agent-synthesis"))
        assert host._schedule_tool(host._control(run.run_id, "agent-synthesis"))
        asyncio.run(host.executor.run_ready(origin["mission_id"]))
        assert host._advance(host._control(run.run_id, "agent-synthesis"))
        report = host.store.records("report")[0]
        reader = host._control(run.run_id, "agent-critique")["body"]
        scope = {"mission_id": reader["mission_id"], "actor_id": reader["actor_id"],
                 "task_id": reader["task_id"]}
        before = shared_material_entry(host.store, pointer=ref(report), **scope)
        assert before is not None
        assert before["type"] == "shared_accepted_report"
        assert before["status"] == "accepted_text_available"
        assert before["title"] == "Synthetic note review"
        assert before["origin_display_name"] == host.store.get(
            "agent_instance", origin["actor_id"], 1
        )["body"]["display_name"]
        assert before["reader_tool"] == "read_context_record"
        assert "A synthetic working-note finding" not in json.dumps(before)

        candidate = host.store.transact(
            "unaccepted-sibling-candidate", {},
            lambda tx: ref(tx.put("report", "unaccepted-sibling-candidate", {
                "mission_id": reader["mission_id"], "title": "Unaccepted synthetic candidate",
                "body": "No board outcome links this report.", "citations": [],
                "citation_input_provenance": [], "limitations": [],
            })),
            mission_id=reader["mission_id"],
        )
        assert shared_material_entry(host.store, pointer=candidate, **scope) is None

        def revoke(tx):
            previous = tx.get(**saved["use"])
            return ref(tx.put("source_use", previous["id"], {
                **previous["body"], "allowed_uses": [], "policy_status": "revoked",
            }, expected_version=previous["version"]))

        host.store.transact("revoke-shared-report", {}, revoke,
                            mission_id=origin["mission_id"])
        after = shared_material_entry(host.store, pointer=ref(report), **scope)
        assert after is not None
        assert after["status"] == "accepted_text_withheld"
        assert after["title"] is None
        assert ref(report) in [item["ref"] for item in shared_material_entries(host.store, **scope)]
    finally:
        host.close()
