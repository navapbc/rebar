"""Close-gate coverage for the completion-verification gate (epic c7c5).

The gate (rebar._commands.transition_close._completion_precheck, wired into transition_compute) is
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
from rebar import config as _config
from rebar._commands import transition as _t
from rebar._commands import transition_close as _tc
from rebar._engine_support.resolver import resolve_ticket_id


def _enable(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


def _commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _make(repo: Path, ttype: str = "task") -> str:
    desc = (
        "Body.\n\n## Acceptance Criteria\n- [ ] done\n\n"
        "## Success Criteria\n- [ ] x\n\n## Context\nc\n"
    )
    tid = rebar.create_ticket(ttype, f"gate {ttype}", description=desc, repo_root=str(repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    return tid


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def _rid(tid: str, repo: Path) -> str:
    return resolve_ticket_id(tid, str(_config.tracker_dir(str(repo))))


def PASS(ticket_id, **kw):
    return {"verdict": "PASS", "findings": [], "runner": "fake", "model": "m"}


def PASS_uncertifiable(ticket_id, **kw):
    # The parent's OWN criteria PASS, but a closed-but-uncertified (force-closed) descendant
    # withholds certification: certifiable=False. The parent may close but not certify.
    return {
        "verdict": "PASS",
        "findings": [],
        "runner": "fake",
        "model": "m",
        "certifiable": False,
    }


def FAIL(ticket_id, **kw):
    return {
        "verdict": "FAIL",
        "runner": "fake",
        "model": "m",
        "findings": [
            {"criterion": "AC1", "detail": "missing", "severity": "high", "dimension": "completion"}
        ],
    }


def _never(ticket_id, **kw):  # must NOT be called
    raise AssertionError("verify_completion was called when it must not be")


def _boom(ticket_id, **kw):  # simulate missing extra/key / any verifier failure
    from rebar.llm.errors import LLMConfigError

    raise LLMConfigError("the agent runner needs the 'agents' extra")


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


def test_gate_pass_uncertifiable_closes_without_signature(rebar_repo: Path, monkeypatch) -> None:
    """A PASS verdict with ``certifiable=False`` (an uncertified/force-closed descendant): the
    parent's OWN criteria passed so it CLOSES — NOT blocked, no ``--force-close`` needed — but the
    close is NOT certified (certification propagates: an unattested descendant leaves the subtree
    unattested). No completion signature is written; the closed-without-signature ticket is the
    durable 'not fully certified' signal."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS_uncertifiable)
    tid = _make(rebar_repo)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"  # CLOSED (not blocked)
    v = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert v.get("verdict") != "certified", v  # NOT certified — no completion signature written


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
        capture_output=True,
        text=True,
        check=True,
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
        _rid(tid, rebar_repo),
        "in_progress",
        "closed",
        force_close="manual override",
        repo_root=str(rebar_repo),
    )
    assert _status(tid, rebar_repo) == "closed"
    # no completion attestation was signed (withheld) — unsigned is the durable signal
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "unsigned"


def test_library_force_close_matches_cli(rebar_repo: Path, monkeypatch) -> None:
    """clay-cake-act: the library ``rebar.transition(..., force_close=...)`` reaches the SAME
    completion-gate-bypass seam as the CLI ``--force-close`` — both close the ticket WITHOUT
    running the verifier and leave it closed-WITHOUT-signature, identically. (Before the fix
    the library wrapper exposed only ``force``, so a library consumer had no in-process bypass
    and had to shell out to the CLI — the parity gap.)"""
    import sys

    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)  # neither path may verify

    # Library path: the new force_close= parameter.
    tid_lib = _make(rebar_repo)
    out = rebar.transition(
        tid_lib, "in_progress", "closed", force_close="lib override", repo_root=str(rebar_repo)
    )
    assert out["to"] == "closed"

    # CLI path: the --force-close flag the wrapper threads to.
    tid_cli = _make(rebar_repo)
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "rebar.cli",
            "transition",
            tid_cli,
            "in_progress",
            "closed",
            "--force-close=cli override",
        ],
        cwd=str(rebar_repo),
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr

    # PARITY: both closed, and both closed-without-signature (the durable "unverified" signal).
    assert _status(tid_lib, rebar_repo) == "closed"
    assert _status(tid_cli, rebar_repo) == "closed"
    assert (
        rebar.verify_signature(tid_lib, repo_root=str(rebar_repo))["verdict"]
        == rebar.verify_signature(tid_cli, repo_root=str(rebar_repo))["verdict"]
        == "unsigned"
    )


def test_bug_without_reason_rejected_before_verifier(rebar_repo: Path, monkeypatch) -> None:
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)  # the precheck must short-circuit
    tid = _make(rebar_repo, "bug")
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert "Fixed:" in ei.value.stderr or "bug" in ei.value.stderr.lower()
    assert _status(tid, rebar_repo) == "in_progress"


def test_unreadable_config_fails_gate_off_with_warning(
    rebar_repo: Path, monkeypatch, capsys
) -> None:
    # An unreadable config must NOT block this (opt-in) gate or preempt other gates.
    (rebar_repo / "rebar.toml").write_text("this is = = not valid toml [[[\n")
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)  # gate-off => never verifies
    tid = _make(rebar_repo)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def _set_impact(tid: str, repo: Path) -> None:
    rebar.set_file_impact(tid, [{"path": "src/x.py", "reason": "touched"}], repo_root=str(repo))


def _commit_ref(repo: Path, ref: str) -> None:
    """Empty commit whose message carries a ``rebar-ticket: <ref>`` trailer."""
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", f"work\n\nrebar-ticket: {ref}"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def test_file_impact_without_referencing_commit_blocks(rebar_repo: Path, monkeypatch) -> None:
    """A ticket that records file_impact but has NO commit referencing it (rebar-ticket
    trailer) is blocked BEFORE the billable verifier runs (fail-fast, deterministic) — the
    implementation has not landed, so completion cannot be confirmed."""
    _commit(rebar_repo)  # a HEAD commit exists, but it does NOT reference the ticket
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)  # must NOT reach the LLM
    tid = _make(rebar_repo)
    _set_impact(tid, rebar_repo)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    # The message names the reason (file_impact) and the remediation (a referencing commit).
    assert "file_impact" in ei.value.stderr
    assert "commit" in ei.value.stderr.lower()
    assert _status(tid, rebar_repo) == "in_progress"
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "unsigned"


def test_file_impact_with_referencing_commit_closes(rebar_repo: Path, monkeypatch) -> None:
    """The same ticket closes once a commit references it via a rebar-ticket trailer — the
    deterministic precheck passes and the (mocked PASS) verifier runs and signs."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    tid = _make(rebar_repo)
    _set_impact(tid, rebar_repo)
    _commit_ref(rebar_repo, tid)  # commit references the ticket by id
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "certified"


def test_file_impact_commit_check_resolves_root_when_repo_root_is_none(
    rebar_repo: Path, monkeypatch
) -> None:
    """Regression: the CLI passes ``repo_root=None`` to the close path. The commit check must
    still find the referencing commit by deriving the code root from the (resolved) tracker —
    NOT by running ``git -C None`` (which fails and would spuriously block a legitimate close)."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    monkeypatch.chdir(rebar_repo)  # so repo_root=None resolves via cwd, mirroring the CLI
    tid = _make(rebar_repo)  # note: no repo_root passed
    rebar.set_file_impact(tid, [{"path": "src/x.py", "reason": "touched"}])
    _commit_ref(rebar_repo, tid)
    rebar.transition(tid, "in_progress", "closed")  # no repo_root — the None path
    assert _status(tid, rebar_repo) == "closed"


def test_no_file_impact_skips_commit_check(rebar_repo: Path, monkeypatch) -> None:
    """The precheck applies only when file_impact is recorded — a ticket with no file_impact
    closes even though no commit references it."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    tid = _make(rebar_repo)  # no file_impact recorded
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_force_close_skips_file_impact_check(rebar_repo: Path, monkeypatch) -> None:
    """--force-close bypasses the deterministic file_impact/commit precheck too (it withholds
    the signature but still closes)."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)  # must not verify
    tid = _make(rebar_repo)
    _set_impact(tid, rebar_repo)
    _t.transition_compute(
        _rid(tid, rebar_repo),
        "in_progress",
        "closed",
        force_close="manual override",
        repo_root=str(rebar_repo),
    )
    assert _status(tid, rebar_repo) == "closed"
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "unsigned"


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

    monkeypatch.setattr(_tc.txn, "transition_core", _raise)
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
    log = rebar.create_ticket(
        "session_log", "L", description="verbose body", repo_root=str(rebar_repo)
    )
    rid = _rid(log, rebar_repo)
    with pytest.raises(CommandError):
        _t.transition_compute(rid, "open", "closed", repo_root=str(rebar_repo))
    assert _status(log, rebar_repo) == "open"  # never closed (lifecycle guard refuses)
    assert rebar.verify_signature(log, repo_root=str(rebar_repo))["verdict"] == "unsigned"


# ── session-provenance precedence (unified resolver, epic crust-fetch-stump / 6014) ──
# The FORCE_CLOSE audit-comment session id now delegates to the shared resolver
# (REBAR_SESSION_ID > CLAUDE_CODE_SESSION_ID > SESSION_ID), then keeps this call site's
# LOCAL cosmetic fallback to short git HEAD, then "unknown". Delegating fixed a latent bug:
# the old local chain OMITTED CLAUDE_CODE_SESSION_ID, so a real Claude Code session fell
# through to git HEAD. Additive "support both" — no deprecation warning.
def test_resolve_session_prefers_rebar_session_id(monkeypatch) -> None:
    """(a) REBAR_SESSION_ID wins even when the other session vars are also set."""
    monkeypatch.setenv("REBAR_SESSION_ID", "explicit-rebar")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude")
    monkeypatch.setenv("SESSION_ID", "ambient")
    assert _tc._resolve_session("ignored") == "explicit-rebar"


def test_resolve_session_captures_claude_code_session_id(monkeypatch) -> None:
    """(a2) BUG FIX: with only CLAUDE_CODE_SESSION_ID set, it is captured (not git HEAD)."""
    monkeypatch.delenv("REBAR_SESSION_ID", raising=False)
    monkeypatch.delenv("SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-sess")
    monkeypatch.setattr(_tc, "_short_head", lambda _tracker: "abc1234")
    assert _tc._resolve_session("ignored") == "claude-sess"


def test_resolve_session_falls_back_to_ambient_session_id(monkeypatch) -> None:
    """(b) With only ambient SESSION_ID set, behavior is exactly as before (back-compat)."""
    monkeypatch.delenv("REBAR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("SESSION_ID", "ambient")
    assert _tc._resolve_session("ignored") == "ambient"


def test_resolve_session_falls_back_to_short_head(monkeypatch) -> None:
    """(c) No session env var set → short git HEAD is used (local cosmetic fallback)."""
    monkeypatch.delenv("REBAR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("SESSION_ID", raising=False)
    monkeypatch.setattr(_tc, "_short_head", lambda _tracker: "abc1234")
    assert _tc._resolve_session("ignored") == "abc1234"


def test_resolve_session_falls_back_to_unknown(monkeypatch) -> None:
    """(d) No session env var set and no HEAD available → 'unknown'."""
    monkeypatch.delenv("REBAR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("SESSION_ID", raising=False)
    monkeypatch.setattr(_tc, "_short_head", lambda _tracker: "")
    assert _tc._resolve_session("ignored") == "unknown"


# ── 24ec: a FAIL close leaves a durable, queryable verdict record ───────────────
def test_fail_close_persists_durable_verdict(rebar_repo: Path, monkeypatch) -> None:
    """A blocked FAIL close must leave a durable, queryable sidecar record (mirroring the
    plan-review REVIEW_RESULT sidecar) carrying the schema tag, the failing criteria, and
    the remediation guidance — so completion FAILs are recoverable offline, not vanished."""
    import json

    from rebar.llm import completion_sidecar as cs

    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", FAIL)
    tid = _make(rebar_repo)
    with pytest.raises(rebar.RebarError):
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    rec = cs.latest_fail_verdict(tid, repo_root=str(rebar_repo))
    assert rec is not None, "no durable FAIL record was persisted"
    assert rec["schema"] == "completion_verifier_fail_v1"
    blob = json.dumps(rec)
    assert "AC1" in blob  # the failing criterion is captured
    assert rec.get("remediation")  # remediation guidance captured (reconcile sets it on FAIL)


# ── 24ec held-out edge coverage (persistence-failure fail-closed; PASS leaves no record) ──


def test_persistence_failure_does_not_mask_the_fail(rebar_repo: Path, monkeypatch) -> None:
    # If the durable-persist step raises, the close MUST still block with the FAIL unchanged
    # (fail-closed preserved; persistence is best-effort observability, never load-bearing).
    from rebar.llm import completion_sidecar as cs

    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", FAIL)

    def _boom_emit(*a, **k):
        raise RuntimeError("sidecar write blew up")

    monkeypatch.setattr(cs, "emit", _boom_emit)
    tid = _make(rebar_repo)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    assert "AC1" in ei.value.stderr
    assert _status(tid, rebar_repo) == "in_progress"


def test_pass_close_leaves_no_fail_record(rebar_repo: Path, monkeypatch) -> None:
    from rebar.llm import completion_sidecar as cs

    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    tid = _make(rebar_repo)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"
    assert cs.latest_fail_verdict(tid, repo_root=str(rebar_repo)) is None
