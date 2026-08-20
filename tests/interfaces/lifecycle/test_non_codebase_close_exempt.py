"""HELD-OUT E2E: the close-gate exemption reaches `[non-codebase]` (story 3726, ADR 0101).

`ensure_ac_boxes_checked` (`src/rebar/_commands/txn.py`) reuses the shared tag matcher with
INVERTED semantics — a match REMOVES an unchecked criterion from the set that blocks a close.
It is the fifth consumer of that regex and the only one outside `plan_review/`, so widening the
matcher changes close-gate behavior. That change is intended (the exemption is the tag's whole
purpose) and is pinned here through the real `rebar.transition` entry point.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm

_PROVENANCE = (
    "      provenance: environment=production; principal=release-bot; "
    "privilege_posture=production-equivalent; instrument=live-call — console shows green\n"
)


def _enable(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


def _commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _make(repo: Path, description: str) -> str:
    tid = rebar.create_ticket("task", "close exempt", description=description, repo_root=str(repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    return tid


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def PASS(ticket_id, **kw):
    return {"verdict": "PASS", "findings": [], "runner": "fake", "model": "m"}


def _never(ticket_id, **kw):
    raise AssertionError("verify_completion was called when the precheck must have fired first")


@pytest.mark.parametrize("tag", ["[non-codebase]", "[operator-attested]"])
def test_unchecked_tagged_item_is_close_exempt(rebar_repo: Path, monkeypatch, tag: str) -> None:
    """BOTH spellings exempt an unchecked criterion from the close block, so the rename does
    not silently strip the exemption from either the new tag or the 827 live tickets on the
    legacy one."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    desc = (
        "Body.\n\n## Acceptance Criteria\n"
        "- [x] code merged\n"
        f"- [ ] {tag} prod deploy verified\n" + _PROVENANCE
    )
    tid = _make(rebar_repo, desc)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_unchecked_untagged_item_still_blocks(rebar_repo: Path, monkeypatch) -> None:
    """The exemption stays NARROW: widening the tag must not turn the close gate off. An
    untagged unchecked criterion still blocks deterministically, before any LLM call."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = "Body.\n\n## Acceptance Criteria\n- [x] shipped\n- [ ] docs updated\n"
    tid = _make(rebar_repo, desc)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    assert "docs updated" in ei.value.stderr
    assert _status(tid, rebar_repo) == "in_progress"


def test_near_miss_tag_does_not_buy_the_exemption(rebar_repo: Path, monkeypatch) -> None:
    """ADR 0043's fail-safe at the close gate: a near-miss spelling is NOT the tag, so it
    still blocks rather than silently earning the escape hatch."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = (
        "Body.\n\n## Acceptance Criteria\n"
        "- [x] code merged\n"
        "- [ ] [non_codebase] prod deploy verified\n"
    )
    tid = _make(rebar_repo, desc)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    assert _status(tid, rebar_repo) == "in_progress"
