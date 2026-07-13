"""Comment-state enrichment tests (Action viability, bug 8b25 follow-on).

The live comment fetch previously issued one ``acli comment list`` per
commented ticket every pass (~1-2s each, fleet-wide). A pre-differ enrichment
now issues ONE paged ``POST /rest/api/3/search/jql`` with fields=["comment"]
and merges the comment field into each snapshot entry, so the differ dedups
comments WITHOUT a per-ticket round-trip. The per-ticket get_comments fallback
is kept only for entries the search omits; the never-emit-blind invariant
stays intact.

Contracts under test:
  1. enrichment lets _diff_comments dedup without client.get_comments being
     called (snapshot-carried path);
  2. a ticket absent from the search enrichment falls back to get_comments;
  3. a search failure → per-ticket fallback + warning, snapshot still written.

All tests use mock clients; no live Jira calls.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
_REC = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"
OUTBOUND_DIFFER_PATH = _REC / "outbound_differ.py"


def _load(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load("outbound_differ_comment_enrich_test", OUTBOUND_DIFFER_PATH)


class StubBindingStore:
    def __init__(self, bindings: dict[str, str]) -> None:
        self._bindings = bindings

    def get_baseline(self, local_id):
        # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
        return None

    def is_pending(self, local_id):
        return False

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


class RecordingClient:
    """Records get_comments calls so we can assert they were (not) made."""

    def __init__(self, comments: list | None = None) -> None:
        self.get_comments_calls: list[str] = []
        self._comments = comments or []

    def get_comments(self, jira_key: str) -> list:
        self.get_comments_calls.append(jira_key)
        return self._comments


def _ticket(ticket_id: str, comment_bodies: list[str]) -> dict:
    return {
        "ticket_id": ticket_id,
        "title": "t",
        "description": "d",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "a",
        "tags": [],
        "comments": [{"body": b} for b in comment_bodies],
    }


# ---------------------------------------------------------------------------
# 1. Enrichment present → differ dedups WITHOUT calling client.get_comments
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enriched_snapshot_skips_get_comments(outbound_differ: ModuleType) -> None:
    """A snapshot entry carrying the enriched comment field dedups w/o a client call."""
    store = StubBindingStore({"local-1": "DIG-1"})
    ticket = _ticket("local-1", ["hello world"])
    # Snapshot enriched by the fetcher: the comment field mirrors Jira's
    # already-present comment, so the differ must emit NO add and NOT call
    # client.get_comments.
    jira_snapshot = {
        "DIG-1": {
            "summary": "t",
            "description": "d",
            "issuetype": {"name": "Task"},
            "priority": {"name": "Medium"},
            "status": {"name": "To Do"},
            "assignee": {"displayName": "a"},
            "labels": [],
            "comment": {"comments": [{"body": "hello world"}]},
        }
    }
    client = RecordingClient()
    mutations, _ = outbound_differ.compute_outbound_mutations(
        [ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
        config=outbound_differ.OutboundDiffConfig(client=client),
    )
    assert client.get_comments_calls == [], (
        "enriched snapshot must NOT trigger a per-ticket get_comments call"
    )
    comment_muts = [c for m in mutations for c in m.comments if c.get("action") == "add"]
    assert comment_muts == [], f"comment already mirrored — expected no add, got: {comment_muts}"


# ---------------------------------------------------------------------------
# 2. Ticket absent from enrichment → falls back to client.get_comments
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unenriched_snapshot_falls_back_to_get_comments(
    outbound_differ: ModuleType,
) -> None:
    """An entry the search omitted (no comment key) falls back to get_comments."""
    store = StubBindingStore({"local-2": "DIG-2"})
    ticket = _ticket("local-2", ["new comment"])
    # Snapshot entry lacks the comment key (search omitted this ticket).
    jira_snapshot = {
        "DIG-2": {
            "summary": "t",
            "description": "d",
            "issuetype": {"name": "Task"},
            "priority": {"name": "Medium"},
            "status": {"name": "To Do"},
            "assignee": {"displayName": "a"},
            "labels": [],
            # no "comment" key → live fallback
        }
    }
    client = RecordingClient(comments=[])  # Jira has no comments
    mutations, _ = outbound_differ.compute_outbound_mutations(
        [ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
        config=outbound_differ.OutboundDiffConfig(client=client),
    )
    assert client.get_comments_calls == ["DIG-2"], (
        "an unenriched entry must fall back to client.get_comments"
    )
    comment_muts = [c for m in mutations for c in m.comments if c.get("action") == "add"]
    assert len(comment_muts) == 1, (
        f"local comment not on Jira → one add expected, got: {comment_muts}"
    )


# ---------------------------------------------------------------------------
# 3. Fetcher: get_comment_map failure → per-ticket fallback + warning
# ---------------------------------------------------------------------------


def _load_fetcher(name: str) -> ModuleType:
    fetcher_path = _REC / "fetcher.py"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, fetcher_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.mark.unit
def test_fetcher_enriches_comment_field(tmp_path: Path) -> None:
    """fetch_snapshot merges get_comment_map results into snapshot entries."""
    fetcher_mod = _load_fetcher("fetcher_comment_enrich_test")

    class _StubClient:
        _called = False

        def search_issues(self, jql, start_at=0, max_results=50):
            if not self._called:
                self._called = True
                return [{"key": "DIG-90", "fields": {"summary": "Test"}}]
            return []

        def get_parent_map(self, project, jql=None):
            return {}

        def get_comment_map(self, project, jql=None):
            return {"DIG-90": {"comments": [{"body": "enriched comment"}]}}

    class _StubAcliMod:
        @staticmethod
        def AcliClient(**kwargs):
            return _StubClient()

    fetcher_mod._load_acli = lambda: _StubAcliMod
    (tmp_path / "bridge_state" / "snapshots").mkdir(parents=True)
    output_path = fetcher_mod.fetch_snapshot("test-pass", repo_root=tmp_path)

    snapshot = json.loads(output_path.read_text())
    assert snapshot["DIG-90"].get("comment") == {"comments": [{"body": "enriched comment"}]}, (
        f"comment field not enriched: {snapshot['DIG-90']}"
    )


@pytest.mark.unit
def test_fetcher_comment_enrichment_failure_degrades(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """get_comment_map failure → no comment key (per-ticket fallback) + warning."""
    import logging

    fetcher_mod = _load_fetcher("fetcher_comment_degrade_test")

    class _FailingClient:
        _called = False

        def search_issues(self, jql, start_at=0, max_results=50):
            if not self._called:
                self._called = True
                return [{"key": "DIG-95", "fields": {"summary": "Test"}}]
            return []

        def get_parent_map(self, project, jql=None):
            return {}

        def get_comment_map(self, project, jql=None):
            raise RuntimeError("REST comment fetch failed")

    class _StubAcliMod:
        @staticmethod
        def AcliClient(**kwargs):
            return _FailingClient()

    fetcher_mod._load_acli = lambda: _StubAcliMod
    (tmp_path / "bridge_state" / "snapshots").mkdir(parents=True)
    with caplog.at_level(logging.WARNING):
        output_path = fetcher_mod.fetch_snapshot("test-pass", repo_root=tmp_path)

    snapshot = json.loads(output_path.read_text())
    # Snapshot still written; entry carries NO comment key → per-ticket fallback.
    assert "DIG-95" in snapshot
    assert "comment" not in snapshot["DIG-95"], (
        "on enrichment failure the entry must lack a comment key (fallback path)"
    )
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "comment enrichment failed" in joined
