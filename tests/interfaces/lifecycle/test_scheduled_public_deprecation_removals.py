from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar

_DESC = (
    "A sufficiently detailed plan body.\n\n## Approach\nDo the thing carefully.\n\n"
    "## Scope\nsrc/x.py\n\n## Testing\n`pytest -q`\n\n## Acceptance Criteria\n"
    "- [ ] the thing works (checked: `pytest -q`)\n"
)


def _commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _make(repo: Path) -> str:
    return rebar.create_ticket(
        "task", "removed transition force", description=_DESC, repo_root=str(repo)
    )


def test_removed_force_close_keyword_is_rejected_and_names_replacement(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    tid = _make(rebar_repo)
    rebar.claim(tid, repo_root=str(rebar_repo))

    with pytest.raises(TypeError) as excinfo:
        rebar.transition(
            tid, "in_progress", "closed", force_close="legacy", repo_root=str(rebar_repo)
        )

    message = str(excinfo.value)
    assert "force_close" in message
    assert 'force="<explicit reason>"' in message
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "in_progress"


def test_removed_boolean_force_is_rejected_and_names_replacement(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    tid = _make(rebar_repo)

    with pytest.raises(TypeError) as excinfo:
        rebar.transition(tid, "open", "in_progress", force=True, repo_root=str(rebar_repo))

    message = str(excinfo.value)
    assert "force=True" in message
    assert 'force="<explicit reason>"' in message
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"
