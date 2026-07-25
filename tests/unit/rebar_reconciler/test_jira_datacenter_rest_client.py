"""JiraDataCenterClient REST v2 / bearer-auth transport contract (mocked HTTP).

Exercises the endpoint/verb/header/body contract with an injected ``opener`` so no
live server is needed: every mutation targets ``/rest/api/2``, carries the
``Authorization: Bearer <PAT>`` header, and sends plain-text (not ADF) bodies. Also
pins the probe status-passthrough and the no-retry-on-HTTPError rule.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from rebar_reconciler.adapters.jira_datacenter.rest_client import JiraDataCenterClient


class _Resp:
    def __init__(self, body: bytes = b"{}", status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status


class _Recorder:
    def __init__(self, responses=None) -> None:
        self.requests: list = []
        self._responses = list(responses or [])

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return self._responses.pop(0) if self._responses else _Resp()


def _client(opener):
    return JiraDataCenterClient(
        base_url="https://dc.example/", pat="PAT123", project="MPSMO", opener=opener
    )


def _json_body(req) -> dict:
    return json.loads(req.data.decode("utf-8"))


def test_get_issue_uses_v2_and_bearer():
    rec = _Recorder([_Resp(json.dumps({"key": "MPSMO-1", "fields": {}}).encode())])
    _client(rec).get_issue("MPSMO-1")
    req = rec.requests[0]
    assert req.get_method() == "GET"
    assert "/rest/api/2/issue/MPSMO-1" in req.full_url
    assert req.get_header("Authorization") == "Bearer PAT123"


def test_create_issue_posts_plain_text_fields():
    rec = _Recorder([_Resp(json.dumps({"key": "MPSMO-9"}).encode(), 201)])
    _client(rec).create_issue(
        {"title": "T", "ticket_type": "bug", "description": "plain body", "priority": "High"}
    )
    req = rec.requests[0]
    assert req.get_method() == "POST"
    assert req.full_url.endswith("/rest/api/2/issue")
    fields = _json_body(req)["fields"]
    assert fields["project"]["key"] == "MPSMO"
    assert fields["summary"] == "T"
    assert fields["issuetype"]["name"] == "Bug"
    assert fields["description"] == "plain body"
    assert fields["priority"]["name"] == "High"


def test_add_comment_sends_plain_body():
    rec = _Recorder([_Resp(json.dumps({"id": "1"}).encode(), 201)])
    _client(rec).add_comment("MPSMO-1", "hello")
    req = rec.requests[0]
    assert req.full_url.endswith("/rest/api/2/issue/MPSMO-1/comment")
    assert _json_body(req) == {"body": "hello"}


def test_set_relationship_uses_v2_issuelink_shape():
    rec = _Recorder([_Resp(b"", 201)])
    _client(rec).set_relationship("MPSMO-1", "MPSMO-2", "Blocks")
    req = rec.requests[0]
    assert req.full_url.endswith("/rest/api/2/issueLink")
    payload = _json_body(req)
    assert payload["type"]["name"] == "Blocks"
    assert payload["outwardIssue"]["key"] == "MPSMO-1"
    assert payload["inwardIssue"]["key"] == "MPSMO-2"


def test_add_label_uses_update_op():
    rec = _Recorder([_Resp(b"", 204)])
    _client(rec).add_label("MPSMO-1", "rebar-id:abc")
    req = rec.requests[0]
    assert req.get_method() == "PUT"
    assert _json_body(req) == {"update": {"labels": [{"add": "rebar-id:abc"}]}}


def test_search_issues_returns_issue_list():
    rec = _Recorder([_Resp(json.dumps({"issues": [{"key": "MPSMO-1"}], "total": 1}).encode())])
    issues = _client(rec).search_issues("project = MPSMO")
    assert [i["key"] for i in issues] == ["MPSMO-1"]
    assert "/rest/api/2/search" in rec.requests[0].full_url


def test_transition_by_name_matches_and_posts_id():
    listing = {"transitions": [{"id": "31", "name": "Done", "to": {"name": "Done"}}]}
    rec = _Recorder([_Resp(json.dumps(listing).encode()), _Resp(b"", 204)])
    _client(rec).transition_issue_by_name("MPSMO-1", "done")
    assert rec.requests[0].get_method() == "GET"
    post = rec.requests[1]
    assert post.get_method() == "POST"
    assert _json_body(post) == {"transition": {"id": "31"}}


def test_transition_by_name_raises_when_absent():
    rec = _Recorder([_Resp(json.dumps({"transitions": []}).encode())])
    with pytest.raises(RuntimeError):
        _client(rec).transition_issue_by_name("MPSMO-1", "Done")


def test_probe_issue_returns_status_and_swallows_httperror():
    payload = {"fields": {"status": {"name": "Open"}}}
    ok = _Recorder([_Resp(json.dumps(payload).encode(), 200)])
    assert _client(ok).probe_issue("MPSMO-1") == (200, payload)

    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    assert _client(_raise).probe_issue("MPSMO-1") == (404, {})


def test_httperror_is_not_retried():
    calls = {"n": 0}

    def _raise(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    with pytest.raises(urllib.error.HTTPError):
        _client(_raise).get_issue("MPSMO-1")
    assert calls["n"] == 1
