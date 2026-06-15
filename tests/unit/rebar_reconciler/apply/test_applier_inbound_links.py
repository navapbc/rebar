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
import subprocess
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


# ---------------------------------------------------------------------------
# bug d843 I1: real-store integration — _apply_inbound_update drives the REAL
# rebar.link against an initialized store and the link is OBSERVABLE via the
# public read (rebar.deps), not merely that rebar.link was called.
# ---------------------------------------------------------------------------


@pytest.fixture
def real_store(tmp_path, monkeypatch):
    """An initialized rebar store with two tickets; returns (repo, a_id, b_id)."""
    import rebar

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"], cwd=repo, check=True)
    monkeypatch.setenv("_TICKET_TEST_NO_SYNC", "1")
    rebar.init_repo(repo_root=str(repo))
    a_id = rebar.create_ticket("task", "Blocker A", repo_root=str(repo))
    b_id = rebar.create_ticket("task", "Blocked B", repo_root=str(repo))
    return repo, a_id, b_id


def test_inbound_link_real_store_is_observable_via_deps(applier, real_store):
    """I1: _apply_inbound_update with a links payload calls the REAL rebar.link
    (NOT mocked), and the created link is observable through the public
    rebar.deps read — proving an end-to-end write, not just arg pass-through."""
    import rebar

    repo, a_id, b_id = real_store

    mutation = _make_inbound_update_mutation(
        applier,
        {
            "local_id": a_id,
            "links": [{"action": "add", "target_id": b_id, "relation": "blocks"}],
        },
        target=a_id,
    )

    result = applier._apply_inbound_update(mutation, repo_root=str(repo))

    assert result.payload["links_applied"] == 1
    # OBSERVABLE: the link is durably in the store, visible via the public read.
    deps = rebar.deps(a_id, repo_root=str(repo))["deps"]
    assert any(d["target_id"] == b_id and d["relation"] == "blocks" for d in deps), (
        f"link not observable via rebar.deps after inbound apply: {deps}"
    )


def test_inbound_link_real_store_invalid_relation_rejected(applier, real_store):
    """I1: a bogus relation is rejected by rebar.link — non-fatal (no crash),
    links_applied == 0, and NO link is written to the store."""
    import rebar

    repo, a_id, b_id = real_store

    mutation = _make_inbound_update_mutation(
        applier,
        {
            "local_id": a_id,
            "links": [{"action": "add", "target_id": b_id, "relation": "not_a_relation"}],
        },
        target=a_id,
    )

    # Does not raise — the rebar.link failure is swallowed and surfaced as a count.
    result = applier._apply_inbound_update(mutation, repo_root=str(repo))

    assert result.payload["links_applied"] == 0
    deps = rebar.deps(a_id, repo_root=str(repo))["deps"]
    assert deps == [], f"a rejected-relation link must not be written: {deps}"
