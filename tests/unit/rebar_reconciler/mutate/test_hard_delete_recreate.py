"""c244 — Jira hard-delete -> outbound re-create (finishes the lost epic-3e36).

On a Jira hard-delete the reconciler preserves the LOCAL content and now re-creates
the Jira issue in the SAME pass: ``_apply_inbound_delete`` emits a
``create_after_hard_delete`` follow-on, and ``applier.apply()`` reconstructs the CREATE
fields from the still-present local ticket and injects a standard outbound CREATE into
the pass's batch (so it flows through ``create_one`` — JQL dedup + bind_confirm + REST
budget). These tests cover the branch reachability (both ``reason`` and the legacy
``probe_outcome`` key), the fields reconstruction + skip paths, dedup/deferral, the
non-hard_delete guard, and the full one-pass end-to-end.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
MUTATION_PATH = APPLIER_PATH.parent / "mutation.py"


def _load_mutation_module():
    canonical = "rebar_reconciler.mutation"
    if canonical in sys.modules:
        return sys.modules[canonical]
    spec = importlib.util.spec_from_file_location(canonical, MUTATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_hard_delete_recreate", APPLIER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_hard_delete_recreate"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mut_mod():
    return _load_mutation_module()


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("REBAR_ENV_ID", raising=False)
    monkeypatch.delenv("REBAR_AUTHOR", raising=False)
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    (tracker / ".env-id").write_text("test-env-id", encoding="utf-8")
    return tmp_path


def _seed_local_ticket(repo_root: Path, local_id: str, *, title: str = "Preserved local ticket"):
    """Write a compiled ``.cache.json`` (``state`` key) for *local_id*, as the store does."""
    tdir = repo_root / ".tickets-tracker" / local_id
    tdir.mkdir(parents=True, exist_ok=True)
    state = {
        "ticket_id": local_id,
        "title": title,
        "description": "still here after the Jira hard-delete",
        "ticket_type": "task",
        "status": "open",
        "priority": "medium",
    }
    (tdir / ".cache.json").write_text(json.dumps({"state": state}), encoding="utf-8")
    return tdir


def _make_mutation(mut_mod, *, direction, action, target, payload=None):
    return mut_mod.Mutation(
        direction=direction,
        action=action,
        target=target,
        payload=payload or {},
        provenance={"source": "test"},
    )


def _inbound_delete(mut_mod, target, branch, *, key="reason"):
    return _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.delete,
        target=target,
        payload={key: branch},
    )


def _recording_acli():
    """A fake acli module whose client records create_issue and reports a JQL miss."""
    client = MagicMock()
    client.search_issues = MagicMock(return_value=[])  # JQL miss -> proceed to create
    client.create_issue = MagicMock(return_value={"key": "REB-NEW-1"})
    mod = types.ModuleType("acli_recording_stub")
    mod.AcliClient = lambda **_: client  # type: ignore[attr-defined]
    return mod, client


def _patch_apply_deps(applier, monkeypatch, acli_mod):
    monkeypatch.setattr(applier, "_load_acli", lambda: acli_mod)
    fake_conc = types.ModuleType("concurrency_stub")
    fake_conc.snapshot_head = lambda _repo_root: "deadbeef" * 5  # type: ignore[attr-defined]

    class _Result:
        ok = True
        event = None
        value = None

    def _rebase_retry(_repo_root, write_fn, **_kwargs):
        write_fn()
        return _Result()

    fake_conc.rebase_retry = _rebase_retry  # type: ignore[attr-defined]
    monkeypatch.setattr(applier, "_load_concurrency", lambda: fake_conc)


# ── branch reachability (canonical `reason` + legacy `probe_outcome` fallback) ──


def test_reason_key_enters_hard_delete_branch(applier, mut_mod, fixture_repo):
    """The canonical `reason` key reaches the hard_delete branch and emits the
    create_after_hard_delete follow-on."""
    mut = _inbound_delete(mut_mod, "jira-reb-hd-a", "hard_delete", key="reason")
    result = applier._apply_inbound_delete(mut, repo_root=fixture_repo)
    fo = result.payload.get("follow_on")
    assert fo is not None and fo["action"] == "create_after_hard_delete"
    assert fo["local_id"] == "jira-reb-hd-a"


def test_probe_outcome_key_still_enters_hard_delete_branch(applier, mut_mod, fixture_repo):
    """The legacy `probe_outcome` key still reaches the branch (the fallback is NOT dead)."""
    mut = _inbound_delete(mut_mod, "jira-reb-hd-b", "hard_delete", key="probe_outcome")
    result = applier._apply_inbound_delete(mut, repo_root=fixture_repo)
    fo = result.payload.get("follow_on")
    assert fo is not None and fo["action"] == "create_after_hard_delete"


# ── fields reconstruction + skip paths ──


def test_recreate_builds_create_dict_from_local_cache(applier, fixture_repo):
    _seed_local_ticket(fixture_repo, "reb-local-1", title="Recreate me")
    fo = {"action": "create_after_hard_delete", "local_id": "reb-local-1"}
    d = applier._build_hard_delete_recreate(fo, fixture_repo, None)
    assert d is not None
    assert d["action"] == "create" and d["local_id"] == "reb-local-1"
    assert d["fields"].get("summary") == "Recreate me"
    assert d["key"] == ""  # fresh create — no existing Jira target


def test_recreate_skips_when_local_ticket_absent(applier, fixture_repo):
    fo = {"action": "create_after_hard_delete", "local_id": "reb-missing"}
    assert applier._build_hard_delete_recreate(fo, fixture_repo, None) is None


def test_recreate_skips_on_malformed_cache(applier, fixture_repo):
    tdir = fixture_repo / ".tickets-tracker" / "reb-corrupt"
    tdir.mkdir(parents=True)
    (tdir / ".cache.json").write_text("{not json", encoding="utf-8")
    fo = {"action": "create_after_hard_delete", "local_id": "reb-corrupt"}
    assert applier._build_hard_delete_recreate(fo, fixture_repo, None) is None


# ── idempotency + deferral (through create_one, the normal outbound path) ──


def test_recreate_jql_dedup_no_double_create(applier, fixture_repo):
    """A re-create dict run through create_one dedups on the JQL label — no double create."""
    _seed_local_ticket(fixture_repo, "reb-dedup", title="Dedup me")
    d = applier._build_hard_delete_recreate(
        {"action": "create_after_hard_delete", "local_id": "reb-dedup"}, fixture_repo, None
    )
    client = MagicMock()
    client.search_issues = MagicMock(return_value=[{"key": "REB-EXIST-9"}])  # JQL HIT
    client.create_issue = MagicMock()
    out = applier.create_one(d, client, rest_calls=0, repo_root=fixture_repo)
    client.create_issue.assert_not_called()
    assert out and out.get("status") == "dedup-create-skipped"


def test_recreate_deferred_when_budget_exhausted(applier, fixture_repo):
    """When the per-pass REST budget is exhausted, the re-create defers (no REST call)."""
    _seed_local_ticket(fixture_repo, "reb-deferred", title="Defer me")
    d = applier._build_hard_delete_recreate(
        {"action": "create_after_hard_delete", "local_id": "reb-deferred"}, fixture_repo, None
    )
    client = MagicMock()
    client.search_issues = MagicMock()
    client.create_issue = MagicMock()
    deferred: list = []
    out = applier.create_one(
        d, client, rest_calls=10_000, deferred_creates=deferred, repo_root=fixture_repo
    )
    assert out is None
    assert deferred == [d]
    client.search_issues.assert_not_called()
    client.create_issue.assert_not_called()


# ── guard + end-to-end ──


def test_non_hard_delete_branches_do_not_recreate(applier, mut_mod, fixture_repo, monkeypatch):
    """out_of_window / trash / redirect never re-create — only hard_delete does."""
    _seed_local_ticket(fixture_repo, "jira-reb-oow", title="No recreate")
    acli_mod, client = _recording_acli()
    _patch_apply_deps(applier, monkeypatch, acli_mod)
    mut = _inbound_delete(mut_mod, "jira-reb-oow", "out_of_window")
    applier.apply([mut], "pass-guard", repo_root=fixture_repo)
    client.create_issue.assert_not_called()


def test_recreate_injected_into_outbound_batch(applier, mut_mod, fixture_repo, monkeypatch):
    """A hard-delete inbound mutation drives an outbound CREATE for the still-present
    local ticket, THROUGH create_one (the fake client records the create)."""
    _seed_local_ticket(fixture_repo, "jira-reb-hd-e", title="Reborn ticket")
    acli_mod, client = _recording_acli()
    _patch_apply_deps(applier, monkeypatch, acli_mod)
    mut = _inbound_delete(mut_mod, "jira-reb-hd-e", "hard_delete")
    applier.apply([mut], "pass-recreate", repo_root=fixture_repo)
    client.create_issue.assert_called_once()


def test_hard_delete_recreate_end_to_end_one_pass(applier, mut_mod, fixture_repo, monkeypatch):
    """Full one-pass E2E: the recorded create carries the reconstructed fields (the local
    ticket's title -> Jira summary/title), proving the whole inbound->outbound path."""
    _seed_local_ticket(fixture_repo, "jira-reb-hd-f", title="End to end title")
    acli_mod, client = _recording_acli()
    _patch_apply_deps(applier, monkeypatch, acli_mod)
    mut = _inbound_delete(mut_mod, "jira-reb-hd-f", "hard_delete")
    applier.apply([mut], "pass-e2e", repo_root=fixture_repo)
    client.create_issue.assert_called_once()
    call = client.create_issue.call_args
    ticket_data = call.args[0] if call.args else call.kwargs
    # create_one translates the differ's Jira snapshot field names to the bridge schema.
    assert (
        ticket_data.get("title") == "End to end title"
        or ticket_data.get("summary") == "End to end title"
    ), f"reconstructed fields not carried into create; got {ticket_data!r}"
