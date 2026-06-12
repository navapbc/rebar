"""Tier B (REBAR_LEAF_WRITES) parity tests — docs/bash-migration.md §3/§4.

Two guarantees:

1. **Switch-resolution parity.** ``rebar._switch.resolve`` is the single source of
   truth for parsing ``REBAR_LEAF_WRITES``; the bash dispatcher's
   ``_leaf_writes_python`` helper must resolve every value to the same bash/python
   verdict (it uses the identical ``tr`` lower+strip idiom, the ``REBAR_PUSH``
   pattern). We pin them against a matrix including typos and mixed case.
2. **Library in-process Python path.** With ``REBAR_LEAF_WRITES=python`` the ported
   leaf writes (comment / set_file_impact / set_verify_commands) go through
   ``rebar._commands`` + the bash append seam and produce state identical to the
   bash path, read back via ``show_ticket``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import _switch

# The exact lower+strip pipeline the dispatcher's _leaf_writes_python runs. Kept
# here so the parity test pins the dispatcher's parse to _switch.resolve.
_BASH_RESOLVE = (
    r"""printf '%s' "${REBAR_LEAF_WRITES:-bash}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]'"""
)

_MATRIX = ["", "python", "PYTHON", " Python ", "bash", "BASH", "py", "bogus", "1", "true"]


@pytest.mark.parametrize("value", _MATRIX)
def test_switch_resolution_matches_bash_idiom(value: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REBAR_LEAF_WRITES", value)
    py_uses_python = _switch.resolve("REBAR_LEAF_WRITES") == "python"
    out = subprocess.run(
        ["bash", "-c", _BASH_RESOLVE],
        env={"REBAR_LEAF_WRITES": value, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    bash_uses_python = out == "python"
    assert py_uses_python == bash_uses_python


def test_switch_unset_defaults_bash(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REBAR_LEAF_WRITES", raising=False)
    assert _switch.resolve("REBAR_LEAF_WRITES") == "bash"
    assert _switch.leaf_writes_python() is False


def _new_ticket(repo: Path) -> str:
    return rebar.create_ticket("task", "leaf parity ticket", repo_root=str(repo))


def test_library_comment_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    rebar.comment(tid, "a python-path note", repo_root=str(rebar_repo))
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    bodies = [c["body"] for c in state["comments"]]
    assert "a python-path note" in bodies


def test_library_comment_parity_bash_vs_python(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Same comment via bash and python paths → both land, identical body."""
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "bash")
    rebar.comment(tid, "shared note", repo_root=str(rebar_repo))
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    rebar.comment(tid, "shared note", repo_root=str(rebar_repo))
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    bodies = [c["body"] for c in state["comments"]]
    assert bodies.count("shared note") == 2


def test_library_file_impact_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    impact = [{"path": "src/x.py", "reason": "touched"}]
    rebar.set_file_impact(tid, impact, repo_root=str(rebar_repo))
    assert rebar.get_file_impact(tid, repo_root=str(rebar_repo)) == impact


def test_library_verify_commands_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    cmds = [{"dd_id": "DD1", "dd_text": "it builds", "command": "make"}]
    rebar.set_verify_commands(tid, cmds, repo_root=str(rebar_repo))
    assert rebar.get_verify_commands(tid, repo_root=str(rebar_repo)) == cmds


def test_library_python_path_rejects_bad_file_impact(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    with pytest.raises(rebar.RebarError):
        rebar.set_file_impact(tid, [{"path": "x"}], repo_root=str(rebar_repo))


def test_library_tag_roundtrip_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    rebar.tag(tid, "area:api", repo_root=str(rebar_repo))
    assert "area:api" in rebar.show_ticket(tid, repo_root=str(rebar_repo))["tags"]
    rebar.untag(tid, "area:api", repo_root=str(rebar_repo))
    assert "area:api" not in rebar.show_ticket(tid, repo_root=str(rebar_repo))["tags"]


def test_library_tag_idempotent_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    rebar.tag(tid, "dup:tag", repo_root=str(rebar_repo))
    rebar.tag(tid, "dup:tag", repo_root=str(rebar_repo))  # idempotent — no second tag
    tags = rebar.show_ticket(tid, repo_root=str(rebar_repo))["tags"]
    assert tags.count("dup:tag") == 1
    # untag of an absent tag is graceful (no raise)
    rebar.untag(tid, "never:applied", repo_root=str(rebar_repo))


def test_library_archive_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    rebar.archive(tid, repo_root=str(rebar_repo))
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["archived"] is True
    # idempotent: archiving again is a silent no-op
    rebar.archive(tid, repo_root=str(rebar_repo))


def test_library_archive_status_gate_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))  # open -> in_progress
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    with pytest.raises(rebar.RebarError):  # archive only works on open tickets
        rebar.archive(tid, repo_root=str(rebar_repo))
