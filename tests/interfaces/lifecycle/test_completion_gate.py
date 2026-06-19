"""Close-gate coverage for the completion-verification gate (epic c7c5).

The gate (rebar._commands.transition._completion_precheck, wired into transition_compute) is
opt-in via ``verify.require_completion_verification_for_close``. These tests monkeypatch
``rebar.llm.verify_completion`` (the gate calls it by module attribute, so the patch is seen) —
NO model/network — and assert the gate's deterministic behavior:

  * gate OFF (default) → close without running the verifier;
  * gate ON + PASS → close succeeds AND a SIGNATURE is written AFTER the close (certified);
  * gate ON + FAIL → blocked (exit 1), ticket stays in_progress, no signature;
  * gate ON + verifier raises (missing extra/key, any error) → fail-CLOSED block;
  * --force-close → closes WITHOUT verifying or signing (withholds the attestation);
  * a bug close with no valid --reason is rejected BEFORE the (billable) verifier runs;
  * an unreadable config fails this gate OFF with a warning (does not block / preempt).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar._commands import transition as _t
from rebar._engine_support.resolver import resolve_ticket_id
from rebar import config as _config


def _enable(repo: Path) -> None:
    (repo / ".rebar").mkdir(exist_ok=True)
    # DOTTED legacy keys — the [section] INI form is silently dropped by .conf parsing (BL-1).
    (repo / ".rebar" / "config.conf").write_text(
        "verify.require_completion_verification_for_close = true\n"
    )


def _commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"], cwd=str(repo), check=True,
        capture_output=True,
    )


def _make(repo: Path, ttype: str = "task") -> str:
    desc = "Body.\n\n## Acceptance Criteria\n- [ ] done\n\n## Success Criteria\n- [ ] x\n\n## Context\nc\n"
    tid = rebar.create_ticket(ttype, f"gate {ttype}", description=desc, repo_root=str(repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    return tid


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def _rid(tid: str, repo: Path) -> str:
    return resolve_ticket_id(tid, str(_config.tracker_dir(str(repo))))


PASS = lambda ticket_id, **kw: {"verdict": "PASS", "findings": [], "runner": "fake", "model": "m"}
FAIL = lambda ticket_id, **kw: {
    "verdict": "FAIL", "runner": "fake", "model": "m",
    "findings": [{"criterion": "AC1", "detail": "missing", "severity": "high", "dimension": "completion"}],
}


def _never(ticket_id, **kw):  # must NOT be called
    raise AssertionError("verify_completion was called when it must not be")


def _boom(ticket_id, **kw):  # simulate missing extra/key / any verifier failure
    from rebar.llm.errors import LLMConfigError

    raise LLMConfigError("the langgraph runner needs the 'agents' extra")


def test_gate_off_by_default_does_not_verify(rebar_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    tid = _make(rebar_repo)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_gate_pass_closes_and_signs_after_close(rebar_repo: Path, monkeypatch) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    tid = _make(rebar_repo)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"
    v = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert v["verdict"] == "certified", v
    # the SIGNATURE event is written (a SIGNATURE event exists on the ticket)
    assert "completion-verifier: PASS" in v["manifest"]


def test_gate_pass_signs_strictly_after_close(rebar_repo: Path, monkeypatch) -> None:
    """ORDERING (behavioral): the verdict is signed AFTER a confirmed close — the SIGNATURE
    commit must land later than the STATUS→closed commit in the tracker history, so a
    failed/raced close can never leave an orphan 'certified' signature on an unclosed ticket.
    Read the durable git history of the tracker branch (not internal call order)."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    tid = _make(rebar_repo)
    rid = _rid(tid, rebar_repo)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))

    tracker = str(_config.tracker_dir(str(rebar_repo)))
    # Subjects newest→oldest; restrict to this ticket so other writes don't interleave.
    log = subprocess.run(
        ["git", "-C", tracker, "log", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    subjects = [s for s in log if rid in s]
    sig_idx = next(i for i, s in enumerate(subjects) if "SIGNATURE" in s)
    status_idx = next(i for i, s in enumerate(subjects) if "STATUS" in s)
    # Newest-first ⇒ the SIGNATURE commit (newer) has the SMALLER index than STATUS-closed.
    assert sig_idx < status_idx, subjects
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "certified"


def test_gate_fail_blocks_and_keeps_open(rebar_repo: Path, monkeypatch) -> None:
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", FAIL)
    tid = _make(rebar_repo)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    # BEHAVIORAL: the close is BLOCKED (non-zero exit), the ticket stays in_progress, and no
    # SIGNATURE is written. CONTRACTUAL: the FAIL finding's `criterion` is surfaced to the
    # operator (so they know WHICH requirement to address) — we assert the load-bearing
    # criterion token, NOT the prose of the error sentence (which is a change-detector).
    assert ei.value.returncode == 1
    assert "AC1" in ei.value.stderr  # the failing criterion is surfaced (contract)
    assert _status(tid, rebar_repo) == "in_progress"
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "unsigned"


def test_gate_missing_llm_fails_closed(rebar_repo: Path, monkeypatch) -> None:
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _boom)
    tid = _make(rebar_repo)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    # BEHAVIORAL fail-CLOSED: an unavailable verifier (missing extra / any error) BLOCKS the
    # close (exit 1) and leaves the ticket in_progress with no signature — it must NOT close on
    # an error. CONTRACTUAL: the message points at the `agents` extra (load-bearing remediation
    # token), not at any exact sentence.
    assert ei.value.returncode == 1
    assert "agents" in ei.value.stderr.lower()
    assert _status(tid, rebar_repo) == "in_progress"
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "unsigned"


def test_force_close_skips_verify_and_sign(rebar_repo: Path, monkeypatch) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)  # must not be called
    tid = _make(rebar_repo)
    _t.transition_compute(
        _rid(tid, rebar_repo), "in_progress", "closed", force_close="manual override",
        repo_root=str(rebar_repo),
    )
    assert _status(tid, rebar_repo) == "closed"
    # no completion attestation was signed (withheld) — unsigned is the durable signal
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "unsigned"


def test_bug_without_reason_rejected_before_verifier(rebar_repo: Path, monkeypatch) -> None:
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)  # the precheck must short-circuit
    tid = _make(rebar_repo, "bug")
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert "Fixed:" in ei.value.stderr or "bug" in ei.value.stderr.lower()
    assert _status(tid, rebar_repo) == "in_progress"


def test_unreadable_config_fails_gate_off_with_warning(rebar_repo: Path, monkeypatch, capsys) -> None:
    # An unreadable config must NOT block this (opt-in) gate or preempt other gates.
    (rebar_repo / "rebar.toml").write_text("this is = = not valid toml [[[\n")
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)  # gate-off => never verifies
    tid = _make(rebar_repo)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_config_dotted_conf_enables_flag_but_ini_form_does_not(tmp_path: Path, monkeypatch) -> None:
    """BL-1 regression: the DOTTED legacy form loads the flag; the [section] INI form in a
    .conf is silently dropped (must NOT enable it)."""
    from rebar import config

    dotted = tmp_path / "dotted.conf"
    dotted.write_text("verify.require_completion_verification_for_close = true\n")
    monkeypatch.setenv("REBAR_CONFIG", str(dotted))
    assert config.load_config(None).verify.require_completion_verification_for_close is True

    ini = tmp_path / "ini.conf"
    ini.write_text("[verify]\nrequire_completion_verification_for_close = true\n")
    monkeypatch.setenv("REBAR_CONFIG", str(ini))
    assert config.load_config(None).verify.require_completion_verification_for_close is False


def test_config_rebar_toml_enables_flag_and_default_off(tmp_path: Path) -> None:
    """The gate flag enables from a dotted ``rebar.toml`` ``[verify]`` table; an empty repo
    defaults the flag OFF (the opt-in contract — the gate never auto-enables)."""
    from rebar import config

    config.reset_config_cache()
    on = tmp_path / "on"
    on.mkdir()
    (on / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")
    assert config.load_config(str(on)).verify.require_completion_verification_for_close is True

    config.reset_config_cache()
    off = tmp_path / "off"
    off.mkdir()
    assert config.load_config(str(off)).verify.require_completion_verification_for_close is False


# ── the gate runs for every WORK type (task already covered above) ─────────────
@pytest.mark.parametrize("ttype", ["story", "epic"])
def test_gate_runs_for_story_and_epic(rebar_repo: Path, monkeypatch, ttype: str) -> None:
    """The completion gate is not task-only: a FAIL verdict blocks closing a story/epic too,
    keeping it in_progress with no signature (behavioral). NOTE: the completion gate runs
    BEFORE the write lock; the story/epic SIGNATURE gate (a separate, default-off gate) is not
    enabled here, so this isolates the completion gate's effect on these types."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", FAIL)
    tid = _make(rebar_repo, ttype)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    assert _status(tid, rebar_repo) == "in_progress"
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "unsigned"


def test_gate_runs_for_bug_with_valid_reason(rebar_repo: Path, monkeypatch) -> None:
    """With a VALID ``--reason``, a bug close reaches the verifier; a FAIL still blocks it
    (the bug-reason precheck is a gate IN FRONT of the verifier, not a bypass of it).
    Driven through ``transition_compute`` (the library ``transition`` has no ``reason`` arg),
    so the raw :class:`CommandError` surfaces here rather than the wrapped RebarError."""
    from rebar._commands._seam import CommandError

    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", FAIL)
    tid = _make(rebar_repo, "bug")
    rid = _rid(tid, rebar_repo)
    with pytest.raises(CommandError) as ei:
        _t.transition_compute(
            rid, "in_progress", "closed", reason="Fixed: patched it", repo_root=str(rebar_repo)
        )
    assert ei.value.returncode == 1
    assert "AC1" in ei.value.message  # the verifier DID run (its finding is surfaced)
    assert _status(tid, rebar_repo) == "in_progress"


# ── ConcurrencyMismatch: a raced close leaves NO orphan signature ──────────────
def test_concurrency_mismatch_leaves_no_orphan_signature(rebar_repo: Path, monkeypatch) -> None:
    """If the locked write (``transition_core``) rejects with a ConcurrencyMismatch AFTER a PASS
    precheck, the post-close signing step is never reached — so a raced/failed close never
    leaves an orphan 'certified' signature on a still-in_progress ticket (the verify→close→sign
    ordering's whole point)."""
    from rebar._commands.txn import ConcurrencyMismatch

    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    tid = _make(rebar_repo)
    rid = _rid(tid, rebar_repo)

    def _raise(*a, **k):
        raise ConcurrencyMismatch('Error: current status is "open", not "in_progress".')

    monkeypatch.setattr(_t.txn, "transition_core", _raise)
    with pytest.raises(ConcurrencyMismatch):
        _t.transition_compute(rid, "in_progress", "closed", repo_root=str(rebar_repo))
    # The ticket never closed and carries NO signature (no orphan attestation).
    assert _status(tid, rebar_repo) == "in_progress"
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "unsigned"


# ── session_log is lifecycle-exempt: a close is refused, no signature written ───
def test_session_log_close_is_refused_no_signature(rebar_repo: Path, monkeypatch) -> None:
    """A session_log cannot be transitioned/closed (lifecycle-exempt): the close is REFUSED, the
    log never closes or gains a completion signature, AND the gate must NOT fire a (billable)
    verifier call for a doomed close — the gate skips session_log before running the verifier."""
    from rebar._commands._seam import CommandError

    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)  # gate must skip session_log
    log = rebar.create_ticket("session_log", "L", description="verbose body", repo_root=str(rebar_repo))
    rid = _rid(log, rebar_repo)
    with pytest.raises(CommandError):
        _t.transition_compute(rid, "open", "closed", repo_root=str(rebar_repo))
    assert _status(log, rebar_repo) == "open"  # never closed (lifecycle guard refuses)
    assert rebar.verify_signature(log, repo_root=str(rebar_repo))["verdict"] == "unsigned"
