"""Held-out oracle for the ``rebar bridge projects`` CLI (story c927).

The cases a happy-path implementer does not see: the required ``--repos`` flag and its
explicit-empty form, replace-not-merge semantics, the unknown-key failure, and the
collateral invariant that a mapping write never touches the sibling bridge state.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout
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


def _bridge_state(repo: Path) -> Path:
    return repo / ".tickets-tracker" / ".bridge_state"


def _projects_file(repo: Path) -> Path:
    return _bridge_state(repo) / "projects.json"


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = bridge_cli(list(argv))
    return rc, out.getvalue(), err.getvalue()


def _stored_repos(repo: Path, key: str) -> list[str]:
    record = json.loads(_projects_file(repo).read_text(encoding="utf-8"))
    return record["projects"][key]["repos"]


def test_set_without_repos_is_an_argument_error(repo: Path) -> None:
    """``--repos`` is required: omitting it exits non-zero and writes no record."""
    rc, _out, _err = _run("projects", "set", "REB")

    assert rc != 0
    assert not _projects_file(repo).exists()


def test_set_with_explicit_empty_repos_stores_empty_list(repo: Path) -> None:
    """``--repos ""`` is the deliberate way to store an empty repo list."""
    rc, _out, _err = _run("projects", "set", "REB", "--repos", "")

    assert rc == 0
    assert _stored_repos(repo, "REB") == []


def test_set_on_existing_key_replaces_rather_than_merges(repo: Path) -> None:
    """A second ``set`` replaces the repos list; it does not union with the first."""
    assert _run("projects", "set", "REB", "--repos", "a,b")[0] == 0
    assert _run("projects", "set", "REB", "--repos", "a")[0] == 0

    assert _stored_repos(repo, "REB") == ["a"]


def test_remove_unknown_key_exits_nonzero_and_mutates_nothing(repo: Path) -> None:
    """Removing a key that is not present fails, names the key, and leaves the record intact."""
    assert _run("projects", "set", "REB", "--repos", "rebar")[0] == 0
    before = _projects_file(repo).read_bytes()

    rc, _out, err = _run("projects", "remove", "NOPE")

    assert rc != 0
    assert "NOPE" in err
    assert _projects_file(repo).read_bytes() == before


def test_mapping_write_leaves_sibling_bridge_state_byte_identical(repo: Path) -> None:
    """A projects write must not rewrite bindings.json or prev_snapshot.json."""
    bs = _bridge_state(repo)
    bs.mkdir(parents=True, exist_ok=True)
    (bs / "bindings.json").write_text('{"version": 1, "bindings": {}}\n', encoding="utf-8")
    (bs / "prev_snapshot.json").write_text('{"snapshot": "x"}\n', encoding="utf-8")

    def _h(name: str) -> str:
        return hashlib.sha256((bs / name).read_bytes()).hexdigest()

    before = (_h("bindings.json"), _h("prev_snapshot.json"))

    assert _run("projects", "set", "REB", "--repos", "rebar")[0] == 0

    assert (_h("bindings.json"), _h("prev_snapshot.json")) == before
