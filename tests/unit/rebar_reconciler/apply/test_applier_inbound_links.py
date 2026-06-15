"""Tests for the Cycle 3 inbound link-apply path in _apply_inbound_update.

Behavior under test:
  - payload['links'] entries with action='add' are written into rebar via the
    LIBRARY facade rebar.link(local_id, target_id, relation, repo_root=...).
    rebar.link owns relation validation, hierarchy promotion, and the LINK
    event write, so the applier delegates rather than hand-writing events.
  - repo_root threads from the leaf signature into the rebar.link call.
  - rebar.link failures are non-fatal (logged, not raised); links_applied counts
    only the successful writes.
  - Malformed entries (missing target_id/relation, action != add) are skipped.

rebar.link is MONKEYPATCHED so the test does NOT touch a real store.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


def _make_inbound_update_mutation(applier_mod, payload, target="DIG-1"):
    mut_mod = applier_mod._load_mutation_module()
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.update,
        target=target,
        payload=payload,
        provenance={"source": "test"},
    )


def test_inbound_link_add_calls_rebar_link(applier, tmp_path, monkeypatch):
    """payload['links'] add entry → rebar.link(local_id, target_id, relation,
    repo_root=...). rebar.link is mocked so no real store is written."""
    import rebar

    calls: list[tuple] = []

    def fake_link(src, dst, relation, *, repo_root=None):
        calls.append((src, dst, relation, repo_root))

    monkeypatch.setattr(rebar, "link", fake_link)

    mutation = _make_inbound_update_mutation(
        applier,
        {
            "local_id": "loc-a",
            "fields": {},
            "labels": [],
            "comments": [],
            "links": [{"action": "add", "target_id": "loc-b", "relation": "blocks"}],
        },
    )

    result = applier._apply_inbound_update(mutation, repo_root=tmp_path)

    assert calls == [("loc-a", "loc-b", "blocks", tmp_path)], (
        f"rebar.link not called with expected args. Calls: {calls}"
    )
    assert result.payload["links_applied"] == 1


def test_inbound_link_repo_root_threads_through(applier, tmp_path, monkeypatch):
    """The repo_root passed to the leaf is forwarded to rebar.link unchanged."""
    import rebar

    seen_repo_root: list = []

    def fake_link(src, dst, relation, *, repo_root=None):
        seen_repo_root.append(repo_root)

    monkeypatch.setattr(rebar, "link", fake_link)

    mutation = _make_inbound_update_mutation(
        applier,
        {
            "local_id": "loc-a",
            "links": [{"action": "add", "target_id": "loc-b", "relation": "relates_to"}],
        },
    )

    applier._apply_inbound_update(mutation, repo_root=tmp_path)

    assert seen_repo_root == [tmp_path]


def test_inbound_link_failure_is_non_fatal(applier, tmp_path, monkeypatch, caplog):
    """A rebar.link failure is logged and does not raise; links_applied excludes
    the failed write."""
    import rebar

    def boom_link(src, dst, relation, *, repo_root=None):
        raise RuntimeError("cycle guard")

    monkeypatch.setattr(rebar, "link", boom_link)

    mutation = _make_inbound_update_mutation(
        applier,
        {
            "local_id": "loc-a",
            "links": [{"action": "add", "target_id": "loc-b", "relation": "blocks"}],
        },
    )

    with caplog.at_level(logging.WARNING):
        result = applier._apply_inbound_update(mutation, repo_root=tmp_path)

    assert result.payload["links_applied"] == 0
    assert any("rebar.link failed" in r.message for r in caplog.records)


def test_inbound_link_malformed_entries_skipped(applier, tmp_path, monkeypatch):
    """Entries missing target_id/relation, or action != add, are skipped."""
    import rebar

    calls: list[tuple] = []

    def fake_link(src, dst, relation, *, repo_root=None):
        calls.append((src, dst, relation))

    monkeypatch.setattr(rebar, "link", fake_link)

    mutation = _make_inbound_update_mutation(
        applier,
        {
            "local_id": "loc-a",
            "links": [
                {"action": "add", "target_id": "loc-b"},  # missing relation
                {"action": "add", "relation": "blocks"},  # missing target_id
                {"action": "remove", "target_id": "loc-c", "relation": "blocks"},  # not add
                "not-a-dict",
            ],
        },
    )

    result = applier._apply_inbound_update(mutation, repo_root=tmp_path)

    assert calls == []
    assert result.payload["links_applied"] == 0
