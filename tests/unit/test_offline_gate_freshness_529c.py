"""Offline gates: only a provably behind or diverged clone is stale (529c-ffa4-5227-4324).

Operator decision 2026-11-03: the client must run offline without knowing anything about
the remote store. Unpublished local writes (the push-pending marker) therefore no longer
make the store stale for any gate. A clone whose HEAD is behind, or has diverged from, its
ALREADY-FETCHED remote-tracking ref is still refused, and ``review-plan`` refuses it before
any LLM call. The job status also labels a replayed review result and stops recommending
``sign-review`` when there is no PASS result to sign from.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from _git_upkeep import init_bare_remote

from rebar._store import freshness, push_state

pytestmark = pytest.mark.unit

TICKET = "529c-ffa4-5227-4324"


def _git(d: Path, *a: str) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"git {' '.join(a)} failed: {r.stderr}"
    return r


def _init_tracker(tracker: Path) -> None:
    tracker.mkdir()
    _git(tracker, "init", "-q")
    _git(tracker, "config", "user.email", "t@e.com")
    _git(tracker, "config", "user.name", "T")
    (tracker / "seed.json").write_text("{}\n")
    _git(tracker, "add", "seed.json")
    _git(tracker, "commit", "-q", "-m", "seed")


def _commit_local(tracker: Path, name: str) -> None:
    (tracker / name).write_text('{"body": "written offline"}\n')
    _git(tracker, "add", name)
    _git(tracker, "commit", "-q", "-m", f"ticket: {name}")


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A tracker level with its origin's fetched ``tickets`` branch."""
    origin = tmp_path / "origin.git"
    tracker = tmp_path / "tracker"
    init_bare_remote(origin)
    _init_tracker(tracker)
    _git(tracker, "remote", "add", "origin", str(origin))
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")
    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")
    return tracker


def _advance_remote_and_fetch(tracker: Path) -> None:
    """Publish from a second clone, then fetch: HEAD is now behind its fetched ref."""
    origin = _git(tracker, "remote", "get-url", "origin").stdout.strip()
    other = tracker.parent / "other"
    subprocess.run(
        ["git", "clone", "-q", "-b", "tickets", origin, str(other)], check=True, capture_output=True
    )
    _git(other, "config", "user.email", "o@e.com")
    _git(other, "config", "user.name", "O")
    _commit_local(other, "elsewhere.json")
    _git(other, "push", "-q", "origin", "HEAD:tickets")
    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")


def _mark_pending(tracker: Path) -> None:
    push_state.record_failure(
        str(tracker), "push-transport-failed", "ssh: connect: timed out", "origin/tickets"
    )


def _push_pending_ahead(store: Path) -> Path:
    _commit_local(store, "offline.json")
    _mark_pending(store)
    return store


def _push_pending_level(store: Path) -> Path:
    _mark_pending(store)
    return store


def _behind(store: Path) -> Path:
    _advance_remote_and_fetch(store)
    return store


def _diverged(store: Path) -> Path:
    _advance_remote_and_fetch(store)
    _commit_local(store, "local.json")
    return store


# ── the three gates, driven against a real clone ─────────────────────────────────────


def _claim_gate(tracker: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """The plan-review claim gate, with a valid attestation stubbed in."""
    from rebar import signing
    from rebar.llm.plan_review import attest, attest_gate

    monkeypatch.setattr(freshness, "resolve_tracker", lambda *_a, **_k: str(tracker))
    monkeypatch.setattr(
        signing, "verify_signature", lambda *_a, **_k: {"verified": True, "verdict": "certified"}
    )
    monkeypatch.setattr(attest, "compute_validity", lambda *_a, **_k: {"valid": True})
    return attest_gate.claim_gate_check(TICKET)


def _close_plan_review_gate(tracker: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """The plan-review close gate, with a valid attestation stubbed in."""
    from rebar import signing
    from rebar._commands import gates
    from rebar.llm.plan_review import attest

    monkeypatch.setattr(gates, "gate_enabled", lambda *_a, **_k: gates.GateState.ENABLED)
    monkeypatch.setattr(
        signing, "verify_signature", lambda *_a, **_k: {"verified": True, "verdict": "certified"}
    )
    monkeypatch.setattr(
        attest, "compute_validity", lambda *_a, **_k: {"valid": True, "verdict": "certified"}
    )
    return gates.close_plan_review_gate_check(
        TICKET, {"ticket_type": "story"}, tracker=str(tracker)
    )


class _PastFreshness(Exception):
    """Raised by the first step after the completion gate's freshness assertion."""


def _completion_close_gate(tracker: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The completion close gate up to the step that follows its freshness assertion.

    Returns ``"passed"`` when the gate got past freshness, else the ``CommandError``.
    """
    from rebar._commands import close_precheck
    from rebar._commands._seam import CommandError

    def _past(*_a, **_k):
        raise _PastFreshness

    monkeypatch.setattr(freshness, "resolve_tracker", lambda *_a, **_k: str(tracker))
    monkeypatch.setattr(close_precheck, "_gate_skip_expectation", lambda *_a, **_k: None)
    monkeypatch.setattr(close_precheck, "_ensure_duplicate_close_is_linked", _past)
    try:
        close_precheck._completion_precheck(
            TICKET, "story", str(tmp_path), None, reason="", force_close=""
        )
    except _PastFreshness:
        return "passed"
    except CommandError as exc:
        return exc
    raise AssertionError("the completion precheck returned without reaching either outcome")


_ALLOWED = [
    pytest.param(_push_pending_ahead, id="push_pending_ahead"),
    pytest.param(_push_pending_level, id="push_pending_level"),
]
_REFUSED = [
    pytest.param(_behind, "BEHIND", id="behind"),
    pytest.param(_diverged, "DIVERGED", id="diverged"),
]


@pytest.mark.parametrize("make", _ALLOWED)
def test_claim_gate_allows_push_pending(store: Path, make, monkeypatch) -> None:
    check = _claim_gate(make(store), monkeypatch)
    assert check["ok"] is True, f"unpublished local writes blocked an offline claim: {check}"


@pytest.mark.parametrize("make", _ALLOWED)
def test_close_plan_review_gate_allows_push_pending(store: Path, make, monkeypatch) -> None:
    check = _close_plan_review_gate(make(store), monkeypatch)
    assert check["ok"] is True, f"unpublished local writes blocked an offline close: {check}"


@pytest.mark.parametrize("make", _ALLOWED)
def test_completion_close_gate_allows_push_pending(
    store: Path, make, monkeypatch, tmp_path: Path
) -> None:
    outcome = _completion_close_gate(make(store), monkeypatch, tmp_path)
    assert outcome == "passed", f"unpublished local writes blocked completion: {outcome}"


@pytest.mark.parametrize(("make", "word"), _REFUSED)
def test_claim_gate_refuses_behind_or_diverged(store: Path, make, word, monkeypatch) -> None:
    check = _claim_gate(make(store), monkeypatch)
    assert check["ok"] is False
    assert check["verdict"] == freshness.STALE_VERDICT
    assert word in check["reason"]


@pytest.mark.parametrize(("make", "word"), _REFUSED)
def test_close_plan_review_gate_refuses_behind_or_diverged(
    store: Path, make, word, monkeypatch
) -> None:
    check = _close_plan_review_gate(make(store), monkeypatch)
    assert check["ok"] is False
    assert check["verdict"] == freshness.STALE_VERDICT
    assert word in check["reason"]


@pytest.mark.parametrize(("make", "word"), _REFUSED)
def test_completion_close_gate_refuses_behind_or_diverged(
    store: Path, make, word, monkeypatch, tmp_path: Path
) -> None:
    outcome = _completion_close_gate(make(store), monkeypatch, tmp_path)
    assert outcome != "passed", "a behind/diverged clone reached the billable verifier"
    assert freshness.STALE_VERDICT in outcome.message
    assert word in outcome.message


def test_push_pending_marker_is_still_recorded_and_untouched(store: Path) -> None:
    """Only the marker's effect on freshness changes; fsck and doctor still need it."""
    _push_pending_ahead(store)
    assert freshness.store_freshness(str(store))["fresh"] is True
    assert push_state.read_status(str(store))["state"] == "pending"


# ── no fetch, no remote: nothing to compare against means fresh ──────────────────────


def test_no_fetch_counts_as_fresh(tmp_path: Path) -> None:
    """A remote that was configured but never fetched gives no basis for staleness."""
    origin = tmp_path / "origin.git"
    tracker = tmp_path / "tracker"
    init_bare_remote(origin)
    _init_tracker(tracker)
    _git(tracker, "remote", "add", "origin", str(origin))
    _commit_local(tracker, "offline.json")
    _mark_pending(tracker)
    result = freshness.store_freshness(str(tracker))
    assert result["fresh"] is True, f"a never-fetched clone was reported stale: {result}"


def test_no_remote_counts_as_fresh(tmp_path: Path) -> None:
    tracker = tmp_path / "tracker"
    _init_tracker(tracker)
    _commit_local(tracker, "offline.json")
    _mark_pending(tracker)
    result = freshness.store_freshness(str(tracker))
    assert result["fresh"] is True, f"a clone with no remote was reported stale: {result}"


# ── review-plan refuses a stale clone before any LLM call ────────────────────────────


class _NeverRunner:
    def preflight(self):
        raise AssertionError("runner preflight must not execute on a stale store")

    def run(self, request):
        raise AssertionError("the LLM must not run on a stale store")


@pytest.mark.parametrize(("make", "word"), _REFUSED)
def test_review_plan_refuses_stale_clone_before_llm(store: Path, make, word, monkeypatch) -> None:
    from rebar.llm import plan_review
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import claimability, context_assembly, relation_snapshot

    tracker = make(store)
    monkeypatch.setattr(freshness, "resolve_tracker", lambda *_a, **_k: str(tracker))
    monkeypatch.setattr(claimability, "not_claimable_verdict", lambda *_a, **_k: None)
    monkeypatch.setattr(relation_snapshot, "collect_plan_relation_snapshot", lambda *a, **k: None)

    def _no_context(*_a, **_k):
        raise AssertionError("context assembly ran on a stale store")

    monkeypatch.setattr(context_assembly, "assemble_context", _no_context)
    for force in (False, True):
        verdict = plan_review._run_plan_review(
            TICKET,
            cfg=LLMConfig(),
            runner=_NeverRunner(),
            sign=True,
            emit_sidecar=True,
            advisory_cap=None,
            repo_root=None,
            force=force,
        )
        assert verdict["verdict"] == "INDETERMINATE"
        assert verdict["coverage"]["llm_ran"] is False
        assert verdict["signature"] == {"signed": False, "reason": freshness.STALE_VERDICT}
        assert verdict["indeterminate"][0]["id"] == freshness.STALE_VERDICT
        assert word in verdict["indeterminate"][0]["reason"]


def test_review_plan_proceeds_on_push_pending_clone(store: Path, monkeypatch) -> None:
    """An offline clone with unpublished writes reaches context assembly."""
    from rebar.llm import plan_review
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import (
        claimability,
        context_assembly,
        generation,
        relation_snapshot,
    )

    tracker = _push_pending_ahead(store)
    monkeypatch.setattr(freshness, "resolve_tracker", lambda *_a, **_k: str(tracker))
    monkeypatch.setattr(claimability, "not_claimable_verdict", lambda *_a, **_k: None)
    monkeypatch.setattr(relation_snapshot, "collect_plan_relation_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(generation, "from_snapshot", lambda *_a, **_k: None)

    def _reached(*_a, **_k):
        raise _PastFreshness

    monkeypatch.setattr(context_assembly, "assemble_context", _reached)
    with pytest.raises(_PastFreshness):
        plan_review._run_plan_review(
            TICKET,
            cfg=LLMConfig(),
            runner=_NeverRunner(),
            sign=True,
            emit_sidecar=True,
            advisory_cap=None,
            repo_root=None,
        )


# ── gate_status labels a replayed review result ──────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import rebar
    from rebar import _mcp_inflight as inflight

    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=root, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(root))
    rebar.init_repo(repo_root=str(root))
    inflight.reset_registry()
    return root


def _write_review_result(repo: Path, ticket_id: str, reviewed_at: int, verdict: str) -> None:
    ticket_dir = repo / ".tickets-tracker" / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    name = f"{reviewed_at}-00000000-0000-4000-8000-000000000000-REVIEW_RESULT.json"
    event = {
        "event_type": "REVIEW_RESULT",
        "data": {
            "schema": "plan_review_result_v2",
            "verdict": verdict,
            "ticket_id": ticket_id,
            "findings": [{"id": "f1", "finding": "f1"}],
        },
    }
    (ticket_dir / name).write_text(json.dumps(event), encoding="utf-8")


def _record_run(ticket_id: str, reviewed_at: int, result: dict) -> str:
    from rebar._mcp_llm import _gate_sidecar_fields
    from rebar.llm import gate_runs

    job_id = f"{time.time_ns()}-job"
    record = {
        "job_id": job_id,
        "ticket_id": ticket_id,
        "gate_type": "plan_review",
        "status": "passed",
        "verdict": result["verdict"],
        "finished_at": time.time(),
    }
    record.update(_gate_sidecar_fields("plan_review", result))
    gate_runs.record_gate_run(record)
    return job_id


def test_replayed_review_result_is_labelled_replayed(repo: Path) -> None:
    import rebar
    from rebar.llm import gate_runs

    tid = rebar.create_ticket("bug", "replayed BLOCK")
    old = time.time_ns() - 10**9
    _write_review_result(repo, tid, old, "BLOCK")
    replay = {
        "verdict": "BLOCK",
        "runner": "reused",
        "sidecar_emitted": False,
        "sidecar_reviewed_at": old,
    }
    out = gate_runs.gate_run_status(_record_run(tid, old, replay))
    assert out["findings"]["reason"] == "replayed-review-result-sidecar", out
    assert out["findings"]["decided_by_run"] is False
    assert out["findings"]["reviewed_at"] == old


def test_fresh_review_result_is_decided_by_this_run(repo: Path) -> None:
    import rebar
    from rebar.llm import gate_runs

    tid = rebar.create_ticket("bug", "fresh BLOCK")
    now = time.time_ns()
    _write_review_result(repo, tid, now, "BLOCK")
    fresh = {
        "verdict": "BLOCK",
        "runner": "claude",
        "sidecar_emitted": True,
        "sidecar_reviewed_at": now,
    }
    out = gate_runs.gate_run_status(_record_run(tid, now, fresh))
    assert out["findings"]["reason"] == "current-review-result-sidecar", out
    assert out["findings"]["readable"] is True
    assert out["findings"]["decided_by_run"] is True


def test_legacy_record_without_replay_flag_reports_unknown_decider(repo: Path) -> None:
    import rebar
    from rebar.llm import gate_runs

    tid = rebar.create_ticket("bug", "legacy record")
    now = time.time_ns()
    _write_review_result(repo, tid, now, "BLOCK")
    job_id = f"{now}-legacy"
    gate_runs.record_gate_run(
        {
            "job_id": job_id,
            "ticket_id": tid,
            "gate_type": "plan_review",
            "status": "passed",
            "verdict": "BLOCK",
            "sidecar_emitted": True,
            "sidecar_reviewed_at": now,
            "finished_at": time.time(),
        }
    )
    out = gate_runs.gate_run_status(job_id)
    assert out["findings"]["reason"] == "current-review-result-sidecar"
    assert out["findings"]["decided_by_run"] is None


# ── sign-review is suggested only when a PASS result file exists ─────────────────────


def _unsigned_reason(repo: Path, tid: str) -> str:
    from rebar.llm.plan_review import attest_gate

    check = attest_gate.claim_gate_check(tid, repo_root=str(repo))
    assert check["ok"] is False and check["verdict"] == "unsigned", check
    return check["reason"]


def test_no_result_file_omits_sign_review(repo: Path) -> None:
    import rebar

    tid = rebar.create_ticket("task", "never reviewed")
    reason = _unsigned_reason(repo, tid)
    assert "sign-review" not in reason, reason
    assert "review-plan" in reason


def test_block_result_file_omits_sign_review(repo: Path) -> None:
    import rebar

    tid = rebar.create_ticket("task", "reviewed and blocked")
    _write_review_result(repo, tid, time.time_ns(), "BLOCK")
    reason = _unsigned_reason(repo, tid)
    assert "sign-review" not in reason, reason


def test_pass_result_file_suggests_sign_review(repo: Path) -> None:
    import rebar

    tid = rebar.create_ticket("task", "passed but signing failed")
    _write_review_result(repo, tid, time.time_ns(), "PASS")
    reason = _unsigned_reason(repo, tid)
    assert f"rebar sign-review {tid}" in reason, reason
