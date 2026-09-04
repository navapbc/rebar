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
        "task", "canonical transition force", description=_DESC, repo_root=str(repo)
    )


def _audit(tid: str, repo: Path) -> str:
    return " ".join(
        c.get("body", "") for c in rebar.show_ticket(tid, repo_root=str(repo)).get("comments", [])
    )


def test_canonical_transition_force_string_still_bypasses_start_work_gate(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    (rebar_repo / "rebar.toml").write_text("[verify]\nrequire_plan_review_for_claim = true\n")
    tid = _make(rebar_repo)

    out = rebar.transition(
        tid, "open", "in_progress", force="approved reason", repo_root=str(rebar_repo)
    )

    assert out["to"] == "in_progress"
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "in_progress"
    assert "FORCE_CLAIM" in _audit(tid, rebar_repo)
    assert "approved reason" in _audit(tid, rebar_repo)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"force": True}, "force=True"),
        ({"force_close": "approved reason"}, "force_close"),
    ],
)
def test_removed_transition_force_aliases_fail_at_the_library_boundary(
    rebar_repo: Path, kwargs: dict[str, object], expected: str
) -> None:
    _commit(rebar_repo)
    (rebar_repo / "rebar.toml").write_text("[verify]\nrequire_plan_review_for_claim = true\n")
    tid = _make(rebar_repo)

    with pytest.raises(TypeError) as excinfo:
        rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo), **kwargs)

    message = str(excinfo.value)
    assert expected in message
    assert 'force="<explicit reason>"' in message
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"
