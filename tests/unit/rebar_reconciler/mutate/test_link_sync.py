"""Link/relationship (blocks/relates) sync — RED→GREEN for bug 3f04.

Two independent root causes, both confirmed live and in code:

  OUTBOUND (local → Jira): production routes through the batch path
  (``batch_dispatch.update_one``), not the typed leaf ``_apply_outbound_update``
  (which handles links). ``_mutation_to_batch_dict`` surfaced ``comments`` and
  ``labels`` but OMITTED ``links``, and ``update_one`` had no link dispatch — so
  the link entry was dropped before any ``set_relationship`` call. The pass still
  reported ``links=1`` / ``error=None`` / ``applied`` (it counts the mutation, not
  the link sub-op).

  INBOUND (Jira → local): the snapshot never carried ``issuelinks`` — the fetcher
  enriched ``parent`` and ``comment`` but not links — so the inbound link differ
  (and the outbound differ's dedup) read ``jira_fields.get("issuelinks")`` and
  always saw nothing. Same class as the inbound-comment bug 0ee6.

These tests use stub clients only; no live Jira.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
_REC = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


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
def batch() -> ModuleType:
    return _load("batch_dispatch_linksync_test", _REC / "batch_dispatch.py")


# ===========================================================================
# OUTBOUND — batch path must carry + dispatch links
# ===========================================================================


def _update_mutation(target: str, links: list[dict]) -> SimpleNamespace:
    """A typed-Mutation-like object for _mutation_to_batch_dict."""
    return SimpleNamespace(
        action=SimpleNamespace(value="update"),
        direction=SimpleNamespace(value="outbound"),
        target=target,
        payload={"changed_fields": {}, "comments": [], "labels": [], "links": links},
    )


def test_mutation_to_batch_dict_surfaces_links(batch: ModuleType) -> None:
    """The batch-dict normalizer must surface the ``links`` payload (was dropped)."""
    links = [{"action": "add", "type": "Blocks", "to_key": "REB-2", "relation": "blocks"}]
    out = batch._mutation_to_batch_dict(_update_mutation("REB-1", links))
    assert out.get("links") == links, (
        f"_mutation_to_batch_dict must surface links so update_one can dispatch them; got {out!r}"
    )


class _RecordingClient:
    def __init__(self, existing_links: list | None = None) -> None:
        self.set_relationship_calls: list[tuple] = []
        self.update_issue_calls: list[tuple] = []
        self._existing_links = existing_links or []

    def update_issue(self, key, **fields):
        self.update_issue_calls.append((key, fields))
        return {"status": "updated"}

    def get_issue_links(self, key):
        return self._existing_links

    def set_relationship(self, from_key, to_key, link_type="Blocks"):
        self.set_relationship_calls.append((from_key, to_key, link_type))
        return {"status": "created"}


def _batch_dict(key: str, links: list[dict]) -> dict:
    return {
        "action": "update",
        "direction": "outbound",
        "key": key,
        "fields": {},
        "local_id": "",
        "follow_on": None,
        "comments": [],
        "labels": [],
        "links": links,
    }


def test_update_one_dispatches_link_adds(batch: ModuleType) -> None:
    """update_one must call set_relationship for each link 'add' (was a no-op)."""
    client = _RecordingClient(existing_links=[])
    mutation = _batch_dict(
        "REB-1", [{"action": "add", "type": "Blocks", "to_key": "REB-2", "relation": "blocks"}]
    )
    batch.update_one(mutation, client)
    assert client.set_relationship_calls == [("REB-1", "REB-2", "Blocks")], (
        f"expected one set_relationship(REB-1, REB-2, Blocks); got {client.set_relationship_calls}"
    )


def test_update_one_skips_already_present_link(batch: ModuleType) -> None:
    """A link already present in Jira (either direction) is not re-added (dedup)."""
    existing = [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "REB-2"}}]
    client = _RecordingClient(existing_links=existing)
    mutation = _batch_dict(
        "REB-1", [{"action": "add", "type": "Blocks", "to_key": "REB-2", "relation": "blocks"}]
    )
    batch.update_one(mutation, client)
    assert client.set_relationship_calls == [], (
        f"existing link must be deduped (no re-add); got {client.set_relationship_calls!r}"
    )


def test_update_one_swaps_endpoints_for_depends_on(batch: ModuleType) -> None:
    """A depends_on dep (swap=True) must be written to Jira as "B Blocks A" (bug c8ed).

    depends_on maps to ("Blocks", swap=True): "REB-1 depends_on REB-2" == "REB-2 Blocks
    REB-1", so REB-2 is the outward/blocker side. The applier previously ignored the swap
    and wrote "REB-1 Blocks REB-2" (inverted) — backwards Jira links + perpetual inbound
    cycle-warning noise. It must swap --out/--in for a swap=True entry.
    """
    client = _RecordingClient(existing_links=[])
    mutation = _batch_dict(
        "REB-1",
        [
            {
                "action": "add",
                "type": "Blocks",
                "to_key": "REB-2",
                "relation": "depends_on",
                "swap": True,
            }
        ],
    )
    batch.update_one(mutation, client)
    assert client.set_relationship_calls == [("REB-2", "REB-1", "Blocks")], (
        f"depends_on must swap endpoints (REB-2 blocks REB-1); got {client.set_relationship_calls}"
    )


# ===========================================================================
# INBOUND — fetcher must enrich the snapshot with issuelinks
# ===========================================================================


def _load_fetcher(name: str) -> ModuleType:
    return _load(name, _REC / "fetcher.py")


def test_fetcher_enriches_issuelinks(tmp_path: Path) -> None:
    """fetch_snapshot merges get_issuelinks_map results into snapshot entries.

    Without this, the inbound link differ (and the outbound dedup) never see Jira
    links — the structural blindness behind bug 3f04 (inbound) and the perpetual
    outbound link re-emission churn.
    """
    fetcher_mod = _load_fetcher("fetcher_issuelinks_enrich_test")
    link = {"type": {"name": "Blocks"}, "outwardIssue": {"key": "DIG-91"}}

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
            return {}

        def get_issuelinks_map(self, project, jql=None):
            return {"DIG-90": [link]}

    class _StubAcliMod:
        @staticmethod
        def AcliClient(**kwargs):
            return _StubClient()

    fetcher_mod._load_acli = lambda: _StubAcliMod
    (tmp_path / "bridge_state" / "snapshots").mkdir(parents=True)
    output_path = fetcher_mod.fetch_snapshot("test-pass", repo_root=tmp_path)

    snapshot = json.loads(output_path.read_text())
    assert snapshot["DIG-90"].get("issuelinks") == [link], (
        f"issuelinks field not enriched onto the snapshot: {snapshot['DIG-90']}"
    )


def test_inbound_differ_reads_enriched_issuelinks() -> None:
    """Contract: given a snapshot entry carrying issuelinks, the inbound differ
    emits a local dep. (Guards the enriched-snapshot → inbound-dep path end to end.)"""
    inbound = _load("inbound_differ_linksync_test", _REC / "inbound_differ.py")

    class _BindingStore:
        def __init__(self, m):
            self._m = m

        def get_local_id(self, jira_key):
            return self._m.get(jira_key)

    snapshot = {
        "REB-1": {
            "summary": "x",
            "issuelinks": [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "REB-2"}}],
        }
    }
    binding = _BindingStore({"REB-1": "local-1", "REB-2": "local-2"})
    local_by_id = {
        "local-1": {"ticket_id": "local-1", "title": "x", "deps": []},
    }
    muts, _ = inbound.compute_inbound_mutations(snapshot, binding, local_by_id)
    links = [lk for m in muts for lk in m.links]
    assert any(
        lk.get("relation") == "depends_on" and lk.get("target_id") == "local-2" for lk in links
    ), f"inbound differ should emit a dep from the enriched issuelink; got {links!r}"
