"""CLI-surface oracle for the cross-session warning (story 5d55).

Real ``python -m rebar`` subprocess tests: a single-ticket CLI command run by a session
that is NOT the holder prints a holder-naming ``another session`` line to STDERR (stdout and
exit code unchanged), while same-session runs, bulk commands, and an unset acting id stay
silent. The config toggle silences it end-to-end, ``link`` warns for the PRIMARY endpoint
only, and the emit is best-effort (a detector error never breaks the command).

Asserts observable behavior only — stderr text, stdout payload, exit code, and persisted
mutation — never internal structure. The holder-naming substring under test is
``another session`` (the detector's message); the pre-existing unrelated ``signing SKIPPED``
stderr line is deliberately not matched.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar

pytestmark = pytest.mark.unit

_SESSION_VARS = ("REBAR_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID", "SESSION_ID")
_HOLDER_MSG = "another session"
_ID_RE = re.compile(r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}")


@pytest.fixture
def cli_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in (*_SESSION_VARS, "AI_AGENT"):
        monkeypatch.delenv(var, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _run(repo: Path, args: list[str], *, session: str | None = None) -> subprocess.CompletedProcess:
    env = subprocess_env()
    for var in _SESSION_VARS:
        env.pop(var, None)
    env["REBAR_ROOT"] = str(repo)
    if session is not None:
        env["REBAR_SESSION_ID"] = session
    return subprocess.run(
        [sys.executable, "-m", "rebar", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _make_ticket(repo: Path, title: str = "t") -> str:
    tid = rebar.create_ticket("task", title, repo_root=str(repo))
    return _ID_RE.search(tid).group(0) if _ID_RE.search(tid) else tid


def _make_claimed(repo: Path, *, holder: str = "sess-A", title: str = "t") -> str:
    tid = _make_ticket(repo, title)
    claimed = _run(repo, ["claim", tid], session=holder)
    assert claimed.returncode == 0, claimed.stderr
    return tid


# --------------------------------------------------------------- happy path (implementer sees)
def test_show_warns_for_other_session(cli_repo: Path) -> None:
    """``show`` as a DIFFERENT session prints a holder-naming WARN to stderr; stdout payload
    and exit code are unchanged."""
    tid = _make_claimed(cli_repo, holder="sess-A")
    res = _run(cli_repo, ["show", tid], session="sess-B")
    assert res.returncode == 0
    assert _HOLDER_MSG in res.stderr
    assert "sess-A" in res.stderr
    # stdout is the normal show payload, unpolluted by the warning.
    assert '"claimed_session": "sess-A"' in res.stdout
    assert _HOLDER_MSG not in res.stdout


def test_show_same_session_no_warn(cli_repo: Path) -> None:
    """Negative control: the HOLDER running ``show`` gets no cross-session warning."""
    tid = _make_claimed(cli_repo, holder="sess-A")
    res = _run(cli_repo, ["show", tid], session="sess-A")
    assert res.returncode == 0
    assert _HOLDER_MSG not in res.stderr


# --------------------------------------------------------------- edge / E2E
def test_comment_warns_and_still_mutates(cli_repo: Path) -> None:
    """A write command (``comment``) as another session warns AND still performs the write —
    the warning is advisory, never a gate."""
    tid = _make_claimed(cli_repo, holder="sess-A")
    res = _run(cli_repo, ["comment", tid, "hello-from-B"], session="sess-B")
    assert res.returncode == 0
    assert _HOLDER_MSG in res.stderr
    assert "sess-A" in res.stderr
    # The mutation landed despite the warning.
    shown = _run(cli_repo, ["show", tid], session="sess-A")
    assert "hello-from-B" in shown.stdout


def test_bulk_commands_never_warn(cli_repo: Path) -> None:
    """Bulk commands (``list``, ``ready``) are excluded from the warning even when the store
    holds another session's in_progress ticket."""
    _make_claimed(cli_repo, holder="sess-A")
    for cmd in (["list"], ["ready"]):
        res = _run(cli_repo, cmd, session="sess-B")
        assert res.returncode == 0, res.stderr
        assert _HOLDER_MSG not in res.stderr, f"{cmd} must not warn"


def test_unset_acting_session_no_warn(cli_repo: Path) -> None:
    """When the acting session id is unknown, stay silent — we cannot prove we are a
    *different* session."""
    tid = _make_claimed(cli_repo, holder="sess-A")
    res = _run(cli_repo, ["show", tid], session=None)
    assert res.returncode == 0
    assert _HOLDER_MSG not in res.stderr


def test_config_toggle_off_silences_cli(cli_repo: Path) -> None:
    """``[warnings] cross_session=false`` in rebar.toml silences the CLI warning end-to-end."""
    tid = _make_claimed(cli_repo, holder="sess-A")
    (cli_repo / "rebar.toml").write_text("[warnings]\ncross_session = false\n", encoding="utf-8")
    res = _run(cli_repo, ["show", tid], session="sess-B")
    assert res.returncode == 0
    assert _HOLDER_MSG not in res.stderr


def test_link_warns_for_primary_endpoint_only(cli_repo: Path) -> None:
    """``link <id1> <id2> <rel>`` warns for the PRIMARY (source ``id1``) holder only — a
    single warning naming id1's holder, not id2's."""
    id1 = _make_claimed(cli_repo, holder="sess-A", title="src")
    id2 = _make_ticket(cli_repo, "dst")  # unheld
    res = _run(cli_repo, ["link", id1, id2, "relates_to"], session="sess-B")
    assert res.returncode == 0, res.stderr
    assert res.stderr.count(_HOLDER_MSG) == 1
    assert "sess-A" in res.stderr


def test_link_ignores_target_holder(cli_repo: Path) -> None:
    """When only the TARGET (``id2``) is held, ``link`` does not warn — the warning is
    computed for the primary ``id1`` only, so a held target is not a cross-session hit."""
    id1 = _make_ticket(cli_repo, "src")  # unheld primary
    id2 = _make_claimed(cli_repo, holder="sess-A", title="dst")
    res = _run(cli_repo, ["link", id1, id2, "relates_to"], session="sess-B")
    assert res.returncode == 0, res.stderr
    assert _HOLDER_MSG not in res.stderr


def test_warning_is_best_effort_on_detector_error(
    cli_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A detector failure (corrupt/absent/locked state) never breaks the command: the write
    still succeeds, exit code is 0, and no warning is emitted."""
    from rebar import _cli
    from rebar._commands import cross_session

    tid = _make_claimed(cli_repo, holder="sess-A")

    def _boom(*_a: object, **_k: object) -> str | None:
        raise RuntimeError("state read blew up")

    monkeypatch.setattr(cross_session, "cross_session_warning_for", _boom)
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-B")

    rc = _cli.main(["comment", tid, "still-works"])
    captured = capsys.readouterr()
    assert rc == 0
    assert _HOLDER_MSG not in captured.err
    # The mutation landed even though the warning path raised.
    shown = _run(cli_repo, ["show", tid], session="sess-A")
    assert "still-works" in shown.stdout
