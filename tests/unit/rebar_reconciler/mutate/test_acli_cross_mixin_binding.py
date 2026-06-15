"""[P0] Real-AcliClient cross-mixin binding test (gates the acli.py split).

acli.py is split into mixins — ``AcliClient(AcliRestMixin, AcliGraphMixin)`` —
whose method bodies call ``self.<other_method>`` across the new module
boundary. A wrong base order / a missing mixin would pass the mock-heavy
field-coverage suite (which patches methods on the class directly) but fail
live, because the REAL dispatch never resolves the unbound method.

This test constructs a REAL ``AcliClient`` with ONLY the two lowest transport
seams stubbed — ``_run`` (the ACLI subprocess seam) and
``urllib.request.urlopen`` (the REST seam) — and asserts that one method per
cluster actually dispatches into the expected seam. It deliberately covers the
8 graph-mixin methods that have ZERO other test references:
``set_relationship``, ``get_issue_links``, ``delete_issue_link``,
``get_parent_map``, ``get_comment_map``, ``update_comment``,
``delete_comment``, ``update_issuetype``.

Behaviour-preserving: it is green on the pre-split monolith (all methods on
AcliClient) and must stay green after the mixin reparenting (methods resolved
via MRO).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
ACLI_PATH = SCRIPTS_DIR / "rebar_reconciler" / "acli.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _bootstrap_pkg() -> None:
    """Register the rebar_reconciler package so by-path acli load resolves siblings."""
    if "rebar_reconciler" not in sys.modules:
        import types as _types

        pkg = _types.ModuleType("rebar_reconciler")
        pkg.__path__ = [str(SCRIPTS_DIR / "rebar_reconciler")]
        sys.modules["rebar_reconciler"] = pkg


@pytest.fixture(scope="module")
def acli_mod() -> ModuleType:
    _bootstrap_pkg()
    spec = importlib.util.spec_from_file_location("acli_xmixin_under_test", ACLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(acli_mod: ModuleType) -> Any:
    return acli_mod.AcliClient(
        jira_url="https://test.atlassian.net",
        user="svc@example.com",
        api_token="fake-token",
        jira_project="DIG",
    )


# ---------------------------------------------------------------------------
# Subprocess-seam graph methods: each must dispatch into self._run.
# ---------------------------------------------------------------------------


def _stub_run(client: Any, stdout: str) -> list[list[str]]:
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        # **kwargs tolerates the bug-d843 retry_on_timeout flag callers now pass.
        captured.append(cmd)
        result = MagicMock()
        result.stdout = stdout
        return result

    client._run = fake_run  # type: ignore[method-assign]
    return captured


def test_set_relationship_dispatches_to_run(client: Any) -> None:
    captured = _stub_run(client, json.dumps({"successCount": 1}))
    out = client.set_relationship("DIG-1", "DIG-2", "Blocks")
    assert out == {"status": "created", "from": "DIG-1", "to": "DIG-2"}
    assert captured and captured[0][:4] == ["jira", "workitem", "link", "create"]
    assert "DIG-1" in captured[0] and "DIG-2" in captured[0]


def test_get_issue_links_reads_issuelinks_via_rest(client: Any) -> None:
    # get_issue_links reads via the REST API (not the ACLI ``link list`` command,
    # which omits the linked-issue key) and returns fields.issuelinks verbatim
    # in the REST-nested shape.
    links = [{"id": "1", "type": {"name": "Blocks"}, "outwardIssue": {"key": "DIG-2"}}]
    captured: dict[str, str] = {}

    def fake_rest_get(path: str) -> dict:
        captured["path"] = path
        return {"fields": {"issuelinks": links}}

    client._direct_rest_get = fake_rest_get
    out = client.get_issue_links("DIG-1")
    assert out == links
    assert "/rest/api/3/issue/DIG-1" in captured["path"]
    assert "issuelinks" in captured["path"]


def test_get_issue_links_empty_when_no_issuelinks_field(client: Any) -> None:
    client._direct_rest_get = lambda path: {"fields": {}}
    assert client.get_issue_links("DIG-1") == []


def test_delete_issue_link_dispatches_to_run(client: Any) -> None:
    captured = _stub_run(client, json.dumps({"successCount": 1}))
    out = client.delete_issue_link("10042")
    assert out == {"status": "deleted", "link_id": "10042"}
    assert captured[0][:4] == ["jira", "workitem", "link", "delete"]
    assert "10042" in captured[0]


def test_update_comment_dispatches_to_run(client: Any) -> None:
    captured = _stub_run(client, json.dumps({"id": "9"}))
    out = client.update_comment("DIG-1", "9", "edited body")
    assert out == {"id": "9"}
    assert captured[0][:4] == ["jira", "workitem", "comment", "update"]


def test_get_issue_link_types_dispatches_to_run(client: Any) -> None:
    types = [{"id": "1", "name": "Blocks"}]
    captured = _stub_run(client, json.dumps(types))
    out = client.get_issue_link_types()
    assert out == types
    assert captured[0][:5] == ["jira", "workitem", "link", "type", "list"]


def test_add_label_dispatches_to_run(client: Any) -> None:
    # add_label sanitizes (jira_fields) then dispatches via self._run (graph mixin).
    captured = _stub_run(client, json.dumps({"successCount": 1}))
    client.add_label("DIG-1", "rebar-id:abc")
    assert captured[0][:3] == ["jira", "workitem", "edit"]


# ---------------------------------------------------------------------------
# REST-seam graph methods: each must dispatch through a _direct_rest_* helper
# down to urllib.request.urlopen (the AcliRestMixin transport).
# ---------------------------------------------------------------------------


def _patch_urlopen(acli_mod: ModuleType, payload: Any) -> Any:
    """Patch the process-wide ``urllib.request.urlopen`` (a shared singleton, so
    every ``_direct_rest_*`` helper — wherever it lives after the split — lands
    here) and accumulate (method, url, body) tuples.
    """
    calls: list[tuple[str, str, bytes | None]] = []

    class _Resp:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    def fake_urlopen(req: Any, timeout: int = 10) -> _Resp:
        calls.append((req.get_method(), req.full_url, req.data))
        return _Resp(json.dumps(payload).encode("utf-8"))

    import urllib.request as _ur

    return patch.object(_ur, "urlopen", side_effect=fake_urlopen), calls


def test_get_parent_map_dispatches_to_rest(client: Any, acli_mod: ModuleType) -> None:
    payload = {
        "issues": [{"key": "DIG-1", "fields": {"parent": {"key": "DIG-9"}}}],
        "isLast": True,
    }
    cm, calls = _patch_urlopen(acli_mod, payload)
    with cm:
        out = client.get_parent_map("DIG")
    assert out == {"DIG-1": "DIG-9"}
    assert calls and calls[0][0] == "POST" and calls[0][1].endswith("/rest/api/3/search/jql")


def test_get_comment_map_dispatches_to_rest(client: Any, acli_mod: ModuleType) -> None:
    payload = {
        "issues": [{"key": "DIG-1", "fields": {"comment": {"comments": [], "total": 0}}}],
        "isLast": True,
    }
    cm, calls = _patch_urlopen(acli_mod, payload)
    with cm:
        out = client.get_comment_map("DIG")
    assert out == {"DIG-1": {"comments": [], "total": 0}}
    assert calls[0][0] == "POST" and calls[0][1].endswith("/rest/api/3/search/jql")


def test_update_issuetype_dispatches_to_rest(client: Any, acli_mod: ModuleType) -> None:
    cm, calls = _patch_urlopen(acli_mod, {})
    with cm:
        client.update_issuetype("DIG-1", "Story")
    assert calls and calls[0][0] == "PUT"
    assert calls[0][1].endswith("/rest/api/3/issue/DIG-1")
    assert b"issuetype" in (calls[0][2] or b"") and b"Story" in (calls[0][2] or b"")


def test_delete_comment_dispatches_to_rest(client: Any, acli_mod: ModuleType) -> None:
    cm, calls = _patch_urlopen(acli_mod, {})
    with cm:
        client.delete_comment("DIG-1", "9")
    assert calls and calls[0][0] == "DELETE"
    assert calls[0][1].endswith("/rest/api/3/issue/DIG-1/comment/9")


def test_update_priority_method_dispatches_to_rest(client: Any, acli_mod: ModuleType) -> None:
    # The AcliClient.update_priority METHOD (graph mixin) is REST PUT — distinct
    # from the module-level update_priority free function.
    cm, calls = _patch_urlopen(acli_mod, {})
    with cm:
        client.update_priority("DIG-1", "High")
    assert calls and calls[0][0] == "PUT"
    assert b"priority" in (calls[0][2] or b"") and b"High" in (calls[0][2] or b"")


# ---------------------------------------------------------------------------
# Cross-mixin chain: a graph method that reaches a REST helper proves the two
# mixins compose on one instance (the MRO hazard the split introduces).
# ---------------------------------------------------------------------------


def test_set_parent_graph_reaches_rest_put(client: Any, acli_mod: ModuleType) -> None:
    cm, calls = _patch_urlopen(acli_mod, {})
    with cm:
        client.set_parent("DIG-1", "DIG-9")
    assert calls and calls[0][0] == "PUT"
    assert calls[0][1].endswith("/rest/api/3/issue/DIG-1")
    assert b"parent" in (calls[0][2] or b"") and b"DIG-9" in (calls[0][2] or b"")


def test_mro_has_both_mixin_clusters(client: Any) -> None:
    """Both transport clusters must resolve on the single concrete instance."""
    # Subprocess-seam (graph) + REST-seam (rest) + core all bound:
    for meth in (
        "set_relationship",
        "get_issue_links",
        "delete_issue_link",
        "get_parent_map",
        "get_comment_map",
        "update_comment",
        "delete_comment",
        "update_issuetype",
        "_direct_rest_get",
        "_rest_urlopen_with_retry",
        "_run",
        "search_issues",
    ):
        assert callable(getattr(client, meth)), f"AcliClient missing {meth} (MRO broken)"
