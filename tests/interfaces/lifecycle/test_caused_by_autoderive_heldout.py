"""Held-out caused_by coverage: the library close facade writes the link, and the user guide
advises a blame hunt. The companion file owns the single-culprit and ambiguous-blame cases."""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.interface


def _caused_by_targets(tid: str, repo: str) -> list[str]:
    deps = rebar.show_ticket(tid, repo_root=repo)["deps"]
    return [d["target_id"] for d in deps if d["relation"] == "caused_by"]


def test_library_facade_threads_caused_by(rebar_repo) -> None:
    repo = str(rebar_repo)
    culprit = rebar.create_ticket("task", "culprit", repo_root=repo)
    bug = rebar.create_ticket("bug", "bug", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)

    # The library facade must accept and thread caused_by (parallel to ed13's close_class).
    rebar.transition(
        bug, "in_progress", "closed", close_class="regression", caused_by=culprit, repo_root=repo
    )
    assert culprit in _caused_by_targets(bug, repo)


def test_user_guide_has_blame_hunt_advisory() -> None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        ug = parent / "docs" / "user-guide.md"
        if ug.exists():
            text = ug.read_text(encoding="utf-8").lower()
            assert "caused-by" in text or "caused_by" in text, (
                "user-guide must mention the caused-by advisory"
            )
            return
    raise AssertionError("could not locate docs/user-guide.md")
