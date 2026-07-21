from __future__ import annotations

from pathlib import Path

import pytest
from test_completion_gate import FAIL, _enable, _make, _status

import rebar
import rebar.llm


@pytest.mark.parametrize(
    ("shape", "close_class", "should_skip"),
    [
        ("replacement_supersedes_bug", "not_a_bug", True),
        ("bug_duplicates_canonical", "duplicate", True),
        ("no_relation", "duplicate", False),
        ("bug_supersedes_replacement", "not_a_bug", False),
        ("archived_duplicate_target", "duplicate", False),
        ("duplicate_link_wrong_class", "regression", False),
    ],
)
def test_completion_gate_skips_only_valid_noncompletion_link_shapes(
    rebar_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    close_class: str,
    should_skip: bool,
) -> None:
    _enable(rebar_repo)
    calls: list[str] = []

    def counted_fail(ticket_id: str, **kwargs):
        calls.append(ticket_id)
        return FAIL(ticket_id, **kwargs)

    monkeypatch.setattr(rebar.llm, "verify_completion", counted_fail)
    bug = _make(rebar_repo, "bug")
    other = rebar.create_ticket("task", "replacement", repo_root=str(rebar_repo))
    if shape == "replacement_supersedes_bug":
        rebar.link(other, bug, "supersedes", repo_root=str(rebar_repo))
    elif shape == "bug_duplicates_canonical":
        rebar.link(bug, other, "duplicates", repo_root=str(rebar_repo))
    elif shape == "bug_supersedes_replacement":
        rebar.link(bug, other, "supersedes", repo_root=str(rebar_repo))
    elif shape == "archived_duplicate_target":
        rebar.link(bug, other, "duplicates", repo_root=str(rebar_repo))
        rebar.archive(other, repo_root=str(rebar_repo))
    elif shape == "duplicate_link_wrong_class":
        rebar.link(bug, other, "duplicates", repo_root=str(rebar_repo))

    if should_skip:
        rebar.transition(
            bug,
            "in_progress",
            "closed",
            close_class=close_class,
            repo_root=str(rebar_repo),
        )
        assert calls == []
        assert _status(bug, rebar_repo) == "closed"
    else:
        with pytest.raises(rebar.RebarError):
            rebar.transition(
                bug,
                "in_progress",
                "closed",
                close_class=close_class,
                repo_root=str(rebar_repo),
            )
        assert calls == [bug]
        assert _status(bug, rebar_repo) == "in_progress"
