"""AC-checkbox completeness precheck at close (ticket 433c) — happy paths.

A deterministic, pre-LLM close precheck: when the completion-verification close gate
would dispatch the verifier, a ticket whose ``## Acceptance Criteria`` section still
contains unchecked ``- [ ]`` items fails the close BEFORE any LLM call — except items
whose text begins with the ``[operator-attested]`` tag (their done-evidence legitimately
lives outside the snapshot). Same harness as test_completion_gate.py: monkeypatch
``rebar.llm.verify_completion`` (the gate calls it by module attribute) — no model, no
network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm


def _enable(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


def _commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _make(repo: Path, description: str, ttype: str = "task") -> str:
    tid = rebar.create_ticket(
        ttype, f"ac gate {ttype}", description=description, repo_root=str(repo)
    )
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    return tid


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def PASS(ticket_id, **kw):
    return {"verdict": "PASS", "findings": [], "runner": "fake", "model": "m"}


def _never(ticket_id, **kw):  # must NOT be called
    raise AssertionError("verify_completion was called when it must not be")


def test_all_boxes_checked_close_proceeds_to_verifier(rebar_repo: Path, monkeypatch) -> None:
    """Every AC checkbox checked → the precheck passes and the close reaches the verifier."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    desc = "Body.\n\n## Acceptance Criteria\n- [x] first done\n- [x] second done\n"
    tid = _make(rebar_repo, desc)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_unchecked_box_blocks_before_any_llm_call(rebar_repo: Path, monkeypatch) -> None:
    """An unchecked, non-attested '- [ ]' AC item fails the close deterministically
    BEFORE the (billable) verifier runs, with remediation guidance."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)  # precheck must fire first
    desc = "Body.\n\n## Acceptance Criteria\n- [x] shipped\n- [ ] docs updated\n"
    tid = _make(rebar_repo, desc)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    # Load-bearing tokens, not exact prose: the offending item and the two remediations.
    assert "docs updated" in ei.value.stderr
    assert "operator-attested" in ei.value.stderr.lower()
    assert _status(tid, rebar_repo) == "in_progress"


def test_unchecked_operator_attested_item_is_exempt(rebar_repo: Path, monkeypatch) -> None:
    """An unchecked item whose text begins with the [operator-attested] tag is NOT a
    violation — the close proceeds to the verifier."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    desc = (
        "Body.\n\n## Acceptance Criteria\n"
        "- [x] code merged\n"
        "- [ ] [operator-attested] prod deploy verified by operator\n"
        "      provenance: environment=production; principal=release-operator; "
        "privilege_posture=production-equivalent; instrument=live-call — console shows green\n"
    )
    tid = _make(rebar_repo, desc)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"
