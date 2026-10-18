"""Offline Browserless rendering receipt and replay boundaries."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from ora.retrieval.providers import HTTPResult
from ora.retrieval.rendering import render_public_document
from ora.state import StateError

URL = "https://example.org/source"
HTML = b"<html><body><script>document.write('detail')</script></body></html>"
RENDERED = "<html><body><main>Rendered detail from source</main></body></html>"
DOCUMENT_REF = {"kind": "public_document_fetch", "id": "ordinary", "version": 1}
HIT_REF = {"kind": "retrieval_hit", "id": "hit", "version": 1}
RESPONSE_REF = {"kind": "retrieval_delivery", "id": "delivery", "version": 1}
VALUES = {"OSRA_BROWSERLESS_BASE_URL": "https://production-sfo.browserless.io",
          "BROWSERLESS_API_KEY": "synthetic-token"}


class Store:
    def __init__(self, *, rights="unknown", content=HTML):
        self.records = {
            ("public_document_fetch", "ordinary", 1): {"kind": "public_document_fetch", "id": "ordinary", "version": 1,
                "body": {"mission_id": "mission", "run_id": "run", "actor_id": "actor", "task_id": "task",
                         "representation": "ordinary_html", "hit_ref": HIT_REF, "response_ref": RESPONSE_REF,
                         "hit_rights_status": rights, "requested_url": URL,
                         "content_sha256": hashlib.sha256(content).hexdigest(),
                         "detected_content_type": "text/html", "blob": {"sha256": "blob"}}},
            ("retrieval_hit", "hit", 1): {"kind": "retrieval_hit", "id": "hit", "version": 1, "body": {}}
        }
        self.content = content

    def get(self, kind, id, version=None):
        if version is None:
            matches = [value for (k, name, _), value in self.records.items() if k == kind and name == id]
            return matches[-1] if matches else None
        return self.records.get((kind, id, version))

    def read_blob(self, sha256):
        assert sha256 == "blob"
        return self.content

    def now(self):
        return "2026-10-15T12:00:00Z"

    def transact(self, operation_id, request, callback, *, mission_id):
        assert mission_id == "mission"
        return callback(self)

    def put(self, kind, id, body):
        record = {"kind": kind, "id": id, "version": 1, "body": body}
        self.records[(kind, id, 1)] = record
        return record


class Intake:
    def __init__(self, store):
        self.store = store
        self.fetched = []

    def save_public_document_fetch(self, operation_id, hit_ref, response_ref, *, fetched, **scope):
        assert hit_ref == HIT_REF and response_ref == RESPONSE_REF
        self.fetched.append(fetched)
        pointer = {"kind": "public_document_fetch", "id": f"public-document-fetch:{operation_id}", "version": 1}
        self.store.records[(pointer["kind"], pointer["id"], 1)] = {**pointer, "body": fetched}
        return pointer

    def inspect_public_document(self, operation_id, pointer, **scope):
        return {"classification": "web_document", "copy_ref":
                {"kind": "source_copy", "id": "rendered-copy", "version": 1}}


def setup(*, rights="unknown"):
    store = Store(rights=rights)
    intake = Intake(store)
    spec = SimpleNamespace(work_id="work", mission_id="mission", run_id="run",
                           actor_id="actor", proposal_ref={"id": "task"})
    args = {"ordinary_document_ref": DOCUMENT_REF, "reason": "Ordinary HTML omitted the client-rendered detail"}
    return store, intake, spec, args


def result(*, final=URL, redirects=None, status=200, wrapped=True):
    data = {"html": RENDERED, "requested_url": URL,
        "final_url": final, "navigation_url": final, "navigation_status": status,
        "redirect_chain": redirects if redirects is not None else []}
    return HTTPResult(200, json.dumps({"data": data, "type": "application/json"} if wrapped else data).encode(), {})


def test_rendered_copy_has_checked_navigation_provenance_and_fixed_program(monkeypatch):
    monkeypatch.setattr("ora.mission.shared_materials.shared_hit_access", lambda *_: True)
    store, intake, spec, args = setup()
    calls = []

    def transport(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return result()

    output, refs = render_public_document(store, intake, spec, args, VALUES, transport=transport)
    assert output["status"] == "inspected" and output["ordinary_document_ref"] == DOCUMENT_REF
    assert refs == (output["copy_ref"],)
    assert len(calls) == 1 and calls[0][0:2] == ("POST", VALUES["OSRA_BROWSERLESS_BASE_URL"] + "/function")
    request = calls[0][2]
    assert request["body"]["context"] == {"url": URL}
    assert "redirectChain()" in request["body"]["code"]
    assert "page.content()" in request["body"]["code"]
    assert "await page.setRequestInterception(true)" in request["body"]["code"]
    assert "request.isNavigationRequest()" in request["body"]["code"]
    assert "target.hostname !== selected.hostname" in request["body"]["code"]
    assert request["body"]["code"].index("setRequestInterception") < request["body"]["code"].index("page.goto")
    assert "stealth" not in json.dumps(request["body"]).lower()
    assert "unblock" not in json.dumps(request["body"]).lower()
    assert request["headers"]["Authorization"] == "Bearer synthetic-token"
    assert intake.fetched[0]["final_url"] == URL
    assert intake.fetched[0]["redirect_chain"] == []
    assert intake.fetched[0]["content"] == RENDERED.encode()
    render_public_document(store, intake, spec, args, VALUES, transport=transport)
    assert len(calls) == 1 and len(intake.fetched) == 1


def test_source_permission_denial_and_missing_configuration_do_not_dispatch(monkeypatch):
    monkeypatch.setattr("ora.mission.shared_materials.shared_hit_access", lambda *_: True)
    store, intake, spec, args = setup(rights="restricted")
    with pytest.raises(StateError, match="permissions"):
        render_public_document(store, intake, spec, args, VALUES,
                               transport=lambda *_a, **_k: pytest.fail("render dispatched"))
    store, intake, spec, args = setup()
    output, refs = render_public_document(store, intake, spec, args, {},
                                          transport=lambda *_a, **_k: pytest.fail("render dispatched"))
    assert output["status"] == "unavailable" and refs == ()


def test_same_host_http_redirect_is_recorded_as_observed(monkeypatch):
    monkeypatch.setattr("ora.mission.shared_materials.shared_hit_access", lambda *_: True)
    store, intake, spec, args = setup()
    final = "https://example.org/current-source"
    chain = [{"from_url": URL, "to_url": final, "status": 302}]
    output, _ = render_public_document(
        store, intake, spec, args, VALUES, transport=lambda *_a, **_k: result(final=final, redirects=chain),
    )
    assert output["status"] == "inspected"
    assert intake.fetched[0]["final_url"] == final
    assert intake.fetched[0]["redirect_chain"] == chain
    assert intake.fetched[0]["content_type"] == "text/html"


def test_direct_function_data_response_is_accepted(monkeypatch):
    monkeypatch.setattr("ora.mission.shared_materials.shared_hit_access", lambda *_: True)
    store, intake, spec, args = setup()
    output, _ = render_public_document(
        store, intake, spec, args, VALUES, transport=lambda *_a, **_k: result(wrapped=False),
    )
    assert output["status"] == "inspected"
    assert intake.fetched[0]["content"] == RENDERED.encode()


def test_known_http_error_replays_without_second_dispatch(monkeypatch):
    monkeypatch.setattr("ora.mission.shared_materials.shared_hit_access", lambda *_: True)
    store, intake, spec, args = setup()
    calls = []

    def transport(*_args, **_kwargs):
        calls.append(True)
        return HTTPResult(429, b"provider text omitted", {})

    first, _ = render_public_document(store, intake, spec, args, VALUES, transport=transport)
    second, _ = render_public_document(store, intake, spec, args, VALUES, transport=transport)
    assert first == second == {"status": "unavailable", "http_status": 429}
    assert len(calls) == 1
    assert not intake.fetched


@pytest.mark.parametrize("response", [
    result(final="https://other.example/source"),
    result(final="https://example.org/elsewhere"),
    result(status=403),
])
def test_unverified_navigation_stays_uncertain_and_is_not_replayed(monkeypatch, response):
    monkeypatch.setattr("ora.mission.shared_materials.shared_hit_access", lambda *_: True)
    store, intake, spec, args = setup()
    calls = []

    def transport(*_args, **_kwargs):
        calls.append(True)
        return response

    with pytest.raises(StateError, match="unverifiable"):
        render_public_document(store, intake, spec, args, VALUES, transport=transport)
    output, refs = render_public_document(store, intake, spec, args, VALUES, transport=transport)
    assert output["status"] == "uncertain" and refs == ()
    assert len(calls) == 1 and not intake.fetched


def test_transport_exception_leaves_uncertain_dispatch_without_retry(monkeypatch):
    monkeypatch.setattr("ora.mission.shared_materials.shared_hit_access", lambda *_: True)
    store, intake, spec, args = setup()
    calls = []

    def transport(*_args, **_kwargs):
        calls.append(True)
        raise TimeoutError("synthetic timeout")

    with pytest.raises(TimeoutError):
        render_public_document(store, intake, spec, args, VALUES, transport=transport)
    output, _ = render_public_document(store, intake, spec, args, VALUES, transport=transport)
    assert output["status"] == "uncertain" and len(calls) == 1
