"""AC-checkbox completeness precheck at close (ticket 433c) — happy paths.

A deterministic, pre-LLM close precheck: when the completion-verification close gate
would dispatch the verifier, a ticket whose ``## Acceptance Criteria`` section still
contains unchecked ``- [ ]`` items fails the close BEFORE any LLM call — except items
whose text begins with the ``[non-codebase]`` tag (their done-evidence legitimately
lives outside the snapshot). Same harness as test_completion_gate.py: monkeypatch
``rebar.llm.verify_completion`` (the gate calls it by module attribute) — no model, no
network.

Held-out oracles merged from test_ac_checkbox_precheck_heldout: AC-checkbox precheck (ticket
433c) — HELD-OUT edge/adversarial suite.
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
    assert "non-codebase" in ei.value.stderr.lower()
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


# ── held-out oracles (merged from test_ac_checkbox_precheck_heldout) ──


def _make_heldout(repo: Path, description: str, ttype: str = "task") -> str:
    tid = rebar.create_ticket(ttype, f"ac hx {ttype}", description=description, repo_root=str(repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    return tid


def _blocked(repo: Path, tid: str) -> str:
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))
    assert ei.value.returncode == 1
    assert _status(tid, repo) == "in_progress"
    return ei.value.stderr


def test_mixed_case_tag_is_exempt(rebar_repo: Path, monkeypatch) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    desc = (
        "## Acceptance Criteria\n- [x] a\n- [ ] [Operator-Attested] restore drill run\n"
        "      provenance: environment=production; principal=ops-oncall; "
        "privilege_posture=production-equivalent; instrument=live-call — drill log reviewed\n"
    )
    tid = _make_heldout(rebar_repo, desc)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_underscore_near_miss_tag_is_not_exempt(rebar_repo: Path, monkeypatch) -> None:
    """[operator_attested] (underscore) is NOT the tag — exact hyphenated token only."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = "## Acceptance Criteria\n- [ ] [operator_attested] deploy applied\n"
    tid = _make_heldout(rebar_repo, desc)
    err = _blocked(rebar_repo, tid)
    assert "deploy applied" in err


def test_tag_not_at_item_start_is_not_exempt(rebar_repo: Path, monkeypatch) -> None:
    """The tag must BEGIN the item text; a mid-line mention does not exempt."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = "## Acceptance Criteria\n- [ ] verify the [operator-attested] runbook step\n"
    tid = _make_heldout(rebar_repo, desc)
    err = _blocked(rebar_repo, tid)
    assert "runbook step" in err


def test_checked_attested_and_checked_plain_close_fine(rebar_repo: Path, monkeypatch) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    desc = (
        "## Acceptance Criteria\n- [X] big case done\n- [x] [operator-attested] drill done\n"
        "      provenance: environment=production; principal=ops-oncall; "
        "privilege_posture=production-equivalent; instrument=live-call — drill log reviewed\n"
    )
    tid = _make_heldout(rebar_repo, desc)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_prose_only_ac_passes_precheck(rebar_repo: Path, monkeypatch) -> None:
    """Prose AC (no checkbox syntax) remains legal at close."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    desc = "## Acceptance Criteria\nThe exporter emits valid JSON on every run.\n"
    tid = _make_heldout(rebar_repo, desc)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_no_ac_section_passes_precheck(rebar_repo: Path, monkeypatch) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    tid = _make_heldout(
        rebar_repo, "Just a body, no AC heading.\n- [ ] stray outside any section\n"
    )
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_unchecked_box_outside_ac_section_does_not_block(rebar_repo: Path, monkeypatch) -> None:
    """A '- [ ]' under ## Testing (or any non-AC section) is not an AC item."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    desc = "## Acceptance Criteria\n- [x] done\n\n## Testing\n- [ ] follow-up manual pass\n"
    tid = _make_heldout(rebar_repo, desc)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_checkbox_inside_fenced_code_block_is_not_an_item(rebar_repo: Path, monkeypatch) -> None:
    """A '- [ ]' line inside a ``` fence within the AC section is literal text, not an item
    (same fence handling as the shared plan-clarity floor)."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    desc = (
        "## Acceptance Criteria\n"
        "- [x] template committed\n"
        "```\n"
        "- [ ] this is example template text\n"
        "```\n"
    )
    tid = _make_heldout(rebar_repo, desc)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_gate_off_unchecked_box_closes(rebar_repo: Path, monkeypatch) -> None:
    """Gate off (default) → no precheck, no verifier: close succeeds untouched."""
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = "## Acceptance Criteria\n- [ ] not done yet\n"
    tid = _make_heldout(rebar_repo, desc)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_force_close_bypasses_precheck(rebar_repo: Path, monkeypatch) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = "## Acceptance Criteria\n- [ ] not done yet\n"
    tid = _make_heldout(rebar_repo, desc)
    rebar.transition(
        tid, "in_progress", "closed", force="operator override", repo_root=str(rebar_repo)
    )
    assert _status(tid, rebar_repo) == "closed"


def test_duplicate_bug_with_live_replacement_bypasses(rebar_repo: Path, monkeypatch) -> None:
    """A duplicate-class bug close with a live replacement link skips the verifier today —
    the AC-checkbox precheck must not fire there either."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    canonical = rebar.create_ticket("task", "canonical", repo_root=str(rebar_repo))
    desc = "## Acceptance Criteria\n- [ ] never satisfied on the dup\n"
    bug = _make_heldout(rebar_repo, desc, ttype="bug")
    rebar.link(bug, canonical, "duplicates", repo_root=str(rebar_repo))
    rebar.transition(
        bug, "in_progress", "closed", close_class="duplicate", repo_root=str(rebar_repo)
    )
    assert _status(bug, rebar_repo) == "closed"


def test_ac_precheck_fires_before_referencing_commit_check(rebar_repo: Path, monkeypatch) -> None:
    """Ticket with BOTH violations (unchecked box AND file_impact without a referencing
    commit): the AC-checkbox message wins — it is the earlier, purely-local precheck."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = "## Acceptance Criteria\n- [ ] wire the adapter\n"
    tid = _make_heldout(rebar_repo, desc)
    rebar.set_file_impact(
        tid, [{"path": "src/x.py", "reason": "touched"}], repo_root=str(rebar_repo)
    )
    err = _blocked(rebar_repo, tid)
    assert "wire the adapter" in err
    assert "rebar-ticket:" not in err


def test_block_message_teaches_remediation(rebar_repo: Path, monkeypatch) -> None:
    """The teaching message carries the load-bearing remediation tokens: check the box,
    the [non-codebase] escape for externally-evidenced items, and --force."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = "## Acceptance Criteria\n- [x] a\n- [ ] publish the doc\n- [ ] tell the team\n"
    tid = _make_heldout(rebar_repo, desc)
    err = _blocked(rebar_repo, tid)
    low = err.lower()
    assert "publish the doc" in err
    assert "tell the team" in err
    assert "non-codebase" in low
    assert "--force" in err
