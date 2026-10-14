"""A guided offline mission keeps working while a PubMed summary leg waits."""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import drain, service, tool


def _last_tool_result(packet: dict, name: str) -> dict:
    message = next(item for item in reversed(packet["messages"])
                   if item["role"] == "tool" and item["name"] == name)
    return json.loads(message["content"])


def test_pubmed_saved_leg_resumes_after_other_route_supplies_cited_evidence(
    tmp_path, monkeypatch,
) -> None:
    import ora.retrieval.discovery as discovery

    query = "synthetic therapy comparison"
    abstract = (
        "In a synthetic randomized comparison, the intervention group had a "
        "three-point mean improvement at eight weeks."
    )
    first_args = {"route": "pubmed.search", "query": query, "limit": 1,
                  "offset": 0, "from_date": "2020-01-01"}
    seen_model = []
    seen_http = []
    selected = {}

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        seen_model.append(packet)
        step = len(seen_model)
        if step == 1:
            return tool("search_scholarly", first_args, step)
        prior = _last_tool_result(packet, "search_scholarly" if step in {2, 3, 5}
                                  else "inspect_hit")
        if step == 2:
            assert prior["route"] == "pubmed.search"
            assert prior["outcome"] == "provider_failure"
            assert prior["hits"] == []
            assert prior["retry_at"] is not None
            selected["retry_at"] = prior["retry_at"]
            selected["esearch_ref"] = prior["esearch_ref"]
            selected["resume_args"] = prior["resume_request"]
            assert selected["resume_args"] == {
                **first_args, "resume_esearch_ref": selected["esearch_ref"],
            }
            assert host.store.get(**selected["esearch_ref"])["body"]["id_list"] == ["111"]
            return tool("search_scholarly", {
                "route": "europe_pmc.search", "query": query, "limit": 1,
            }, step)
        if step == 3:
            assert prior["route"] == "europe_pmc.search"
            assert prior["outcome"] == "success" and len(prior["hits"]) == 1
            assert discovery.time.time() < datetime.fromisoformat(
                selected["retry_at"]
            ).timestamp()
            selected["europe_hit_id"] = prior["hits"][0]["hit_id"]
            assert [route for route, _ in seen_http] == [
                "pubmed.esearch", "pubmed.esummary.wait", "europe_pmc.search",
            ]
            return tool("inspect_hit", {"hit_id": selected["europe_hit_id"]}, step)
        if step == 4:
            assert prior["status"] == "inspected"
            selected["source_id"] = prior["source_id"]
            selected["span_id"] = prior["passages"][0]["span_id"]
            source_packets = [
                json.loads(item["content"])["source_packets"]
                for item in packet["messages"] if item["role"] == "user"
                and isinstance(item.get("content"), str)
                and item["content"].startswith('{"source_packets"')
            ]
            assert source_packets
            source_record = host.store.get("source_copy", selected["source_id"])
            assert any(
                source["id"] == source_record["id"]
                and source["version"] == source_record["version"]
                and source["copy_sha256"] == source_record["body"]["content_sha256"]
                and abstract in source["text"]
                for sources in source_packets for source in sources
            )
            assert host.store.get(**selected["esearch_ref"])["body"]["id_list"] == ["111"]
            now = discovery.time.time()
            monkeypatch.setattr(discovery.time, "time", lambda: now + 61)
            return tool("search_scholarly", selected["resume_args"], step)
        assert step == 5
        assert prior["route"] == "pubmed.search"
        assert prior["outcome"] == "success"
        assert prior["esearch_ref"] == selected["esearch_ref"]
        assert len(prior["hits"]) == 1
        assert prior["hits"][0]["access_surface"] == "metadata"
        selected["pubmed_hit_id"] = prior["hits"][0]["hit_id"]
        assert abstract in json.dumps(packet["messages"])
        return tool("publish_result", {
            "title": "Synthetic abstract finding",
            "text": "The inspected Europe PMC abstract reports a three-point mean improvement at eight weeks.",
            "citations": [{"source_id": selected["source_id"],
                           "span_id": selected["span_id"]}],
            "limitations": [
                "The resumed PubMed summary is bibliographic metadata; its source text was not inspected.",
            ],
        }, step)

    def discovery_transport(method, url, *, body=None, headers=None):
        assert method == "GET"
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if parsed.path.endswith("esearch.fcgi"):
            seen_http.append(("pubmed.esearch", params))
            return HTTPResult(200, json.dumps({
                "esearchresult": {"count": "1", "idlist": ["111"]},
            }).encode(), {})
        if parsed.path.endswith("esummary.fcgi"):
            prior_count = sum(route.startswith("pubmed.esummary") for route, _ in seen_http)
            if prior_count == 0:
                seen_http.append(("pubmed.esummary.wait", params))
                return HTTPResult(429, b"{}", {"retry-after": "60"})
            seen_http.append(("pubmed.esummary.resumed", params))
            return HTTPResult(200, json.dumps({
                "result": {"uids": ["111"], "111": {
                    "uid": "111", "title": "Synthetic therapy trial",
                    "pubdate": "2024 Jan", "doi": "10.1234/synthetic-therapy",
                }},
            }).encode(), {})
        assert "europepmc" in url
        seen_http.append(("europe_pmc.search", params))
        return HTTPResult(200, json.dumps({
            "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
                "source": "MED", "id": "222", "pmid": "222",
                "doi": "10.1234/synthetic-europe", "title": "Synthetic comparison abstract",
                "abstractText": abstract, "isOpenAccess": "Y",
            }]},
        }).encode(), {})

    host = service(tmp_path, monkeypatch, model_transport)
    host.discovery_transport = discovery_transport
    try:
        run = host.start(LiveStartRequest(
            request_id="guided-pubmed-partial-europe-progress",
            question="Find and inspect a synthetic therapy comparison while recovering PubMed metadata",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=30)
        assert host.state(run.run_id).snapshot.mission.status == "completed", (
            len(seen_model), host._control(run.run_id, "agent-synthesis")["body"]["waiting_reason"]
        )
        assert len(seen_model) == 5
        assert [route for route, _ in seen_http] == [
            "pubmed.esearch", "pubmed.esummary.wait", "europe_pmc.search",
            "pubmed.esummary.resumed",
        ]
        assert all(params["id"] == ["111"] for route, params in seen_http
                   if route.startswith("pubmed.esummary"))
        assert host.store.get(**selected["esearch_ref"])["body"]["id_list"] == ["111"]
        europe_hit = host.store.get("retrieval_hit", selected["europe_hit_id"])
        pubmed_hit = host.store.get("retrieval_hit", selected["pubmed_hit_id"])
        source = host.store.get("source_copy", selected["source_id"])
        assert europe_hit["body"]["source"]["content"] == abstract
        assert pubmed_hit["body"]["source"]["access_surface"] == "metadata"
        assert source["body"]["content_sha256"]
        assert host.store.resolve("source_use.copy", f"{source['id']}:{source['version']}")[
            "body"]["retrieval_hit_ref"] == ref(europe_hit)
        deliveries = host.store.records("retrieval_delivery")
        assert len(deliveries) == 3
        resumed = next(item for item in deliveries if item["body"]["envelope"]["request"].get(
            "resumeEsearchRef"))
        resumed_request = resumed["body"]["envelope"]["request"]
        first_request = next(item["body"]["envelope"]["request"] for item in deliveries
                             if item["body"]["envelope"]["outcome"] == "provider_failure")
        assert resumed_request["resumeEsearchRef"] == selected["esearch_ref"]
        assert resumed_request["query"] == first_request["query"] == query
        assert resumed_request["filters"] == first_request["filters"] == {
            "from_date": "2020-01-01",
        }
        assert resumed_request["requestedWindow"] == first_request["requestedWindow"] == {
            "cursor": None, "limit": 1, "offset": 0,
        }
        report = host.store.get(**host._control(run.run_id, "agent-synthesis")["body"]["report_ref"])
        assert "three-point mean improvement" in report["body"]["body"]
        assert "bibliographic metadata" in report["body"]["limitations"][0]
        assert report["body"]["citations"] == [{
            "sourceId": selected["source_id"], "spanId": selected["span_id"],
        }]
        assert report["body"]["citation_input_provenance"][0]["input_status"] == (
            "exact_passage_in_producing_call"
        )
        assert host.state(run.run_id).snapshot.artifacts[0].citations[0].source_id == (
            selected["source_id"]
        )
    finally:
        host.close()
