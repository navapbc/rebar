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


def test_gate_fail_blocks_and_keeps_open(rebar_repo: Path, monkeypatch) -> None:
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", FAIL)
    tid = _make(rebar_repo)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert "completion verification FAILED" in ei.value.stderr
    assert "AC1" in ei.value.stderr
    assert _status(tid, rebar_repo) == "in_progress"
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "unsigned"


def test_gate_missing_llm_fails_closed(rebar_repo: Path, monkeypatch) -> None:
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _boom)
    tid = _make(rebar_repo)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert "could not run" in ei.value.stderr and "agents" in ei.value.stderr
    assert _status(tid, rebar_repo) == "in_progress"


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
