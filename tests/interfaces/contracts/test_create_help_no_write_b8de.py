"""A ``--help`` request must never be stored as ticket content (bug b8de).

``rebar create task --help`` (a request to view the create usage) used to create a
placeholder ticket titled ``--help``: the dispatcher only honoured ``--help``/``-h`` when
it was the FIRST token after the subcommand (``rest[0]``), so with ``rest == ["task",
"--help"]`` the flag fell through to ``composer.create_cli`` and was consumed as the title,
performing a CREATE write.

The repaired contract: ``--help``/``-h`` appearing anywhere before a ``--`` terminator is a
help request for a routable subcommand — it prints the pinned usage to stdout, exits 0, and
performs NO write. ``--`` remains the escape hatch, and the commands that own their own
help (and the unknown-subcommand error) are unchanged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import _cli

pytestmark = pytest.mark.interface


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init",),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _ticket_count(repo: Path) -> int:
    return len(rebar.list_tickets(repo_root=str(repo)))


def test_create_task_help_shows_usage_and_writes_nothing(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reported repro: ``create task --help`` prints usage, exits 0, creates NO ticket."""
    assert _ticket_count(rebar_repo) == 0

    rc = _cli.main(["create", "task", "--help"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "create" in out.lower()
    assert _ticket_count(rebar_repo) == 0, "a --help request must not create a ticket"


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "task", "--help"],
        ["create", "task", "-h"],
        ["create", "task", "a title", "--help"],
        ["edit", "some-id", "--help"],
    ],
)
def test_help_anywhere_before_terminator_shows_usage_no_write(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """``--help``/``-h`` in any pre-``--`` position triggers usage and no write."""
    before = _ticket_count(rebar_repo)

    rc = _cli.main(argv)

    assert rc == 0
    assert capsys.readouterr().out.strip(), "expected usage text on stdout"
    assert _ticket_count(rebar_repo) == before, "help must not mutate the store"


def test_ordinary_create_still_writes(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A real title with no help flag creates exactly one ticket (no over-eager intercept)."""
    rc = _cli.main(["create", "task", "a real title"])
    capsys.readouterr()

    assert rc == 0
    assert _ticket_count(rebar_repo) == 1


@pytest.mark.parametrize(
    ("rest", "expected"),
    [
        (["--help"], True),
        (["-h"], True),
        (["task", "--help"], True),
        (["task", "a title", "-h"], True),
        ([], False),
        (["task", "a real title"], False),
        (["--", "--help"], False),
        (["task", "--", "-h"], False),
        (["--help", "--"], True),
    ],
)
def test_wants_help_scans_before_the_terminator(rest: list[str], expected: bool) -> None:
    """The dispatcher help scan honours a flag in any position up to a ``--`` terminator."""
    assert _cli._wants_help(rest) is expected
