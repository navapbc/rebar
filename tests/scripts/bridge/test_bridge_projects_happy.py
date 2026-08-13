"""Happy-path oracle for the ``rebar bridge projects`` CLI + library (story c927).

Well-formed set/list through the real CLI parser with a persisted-state assertion, and
the library facade round-trip. The argument-error, empty-list, replace-semantics,
unknown-key, and collateral-invariant cases live in the held-out oracle.
"""

from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import rebar
from rebar._cli._bridge_commands import bridge_cli

pytestmark = [pytest.mark.unit, pytest.mark.scripts]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=r, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    rebar.init_repo(repo_root=str(r))
    return r


def _projects_file(repo: Path) -> Path:
    return repo / ".tickets-tracker" / ".bridge_state" / "projects.json"


def _run(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = bridge_cli(list(argv))
    return rc, buf.getvalue()


def test_cli_set_then_list_and_persisted_record(repo: Path) -> None:
    """set writes the entry to projects.json; list prints the projects mapping as JSON."""
    rc_set, _ = _run("projects", "set", "REB", "--repos", "rebar")
    assert rc_set == 0

    record = json.loads(_projects_file(repo).read_text(encoding="utf-8"))
    assert record["projects"]["REB"] == {"repos": ["rebar"]}

    rc_list, out = _run("projects", "list")
    assert rc_list == 0
    assert json.loads(out) == {"REB": {"repos": ["rebar"]}}


def test_library_set_then_list_roundtrip(repo: Path) -> None:
    """The library facade writes and reads back the same entry."""
    rebar.bridge_projects_set("REB", ["rebar"], repo_root=str(repo))

    assert rebar.bridge_projects_list(repo_root=str(repo)) == {"REB": {"repos": ["rebar"]}}
