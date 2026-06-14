"""Tier E E5: purge-bridge — in-process vs dispatcher byte-parity.

purge-bridge mutates (rm -rf jira-* dirs + commit), and its output carries NO
ticket ids (only counts + the --keep project name), so mutating scenarios run the
SAME fixture set through each impl on a fresh store and compare output directly;
the non-mutating arg-guards run on a shared store.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from rebar._cli import main
from rebar._engine import dispatcher, engine_env

# The bash arm runs `git commit` WITHOUT -q, so it emits git's plumbing summary
# (`[tickets <hash>] …`, ` N files changed`, ` delete mode …`) — nondeterministic
# (the commit hash varies) and impossible to reproduce byte-for-byte. Like the E3
# delete port, the in-process impl suppresses that chatter (cleaner stdout); the
# deterministic human narration is what must match. Strip the git summary lines
# from the bash output before the byte comparison.
_GIT_CHATTER = re.compile(
    r"^(\[tickets [0-9a-f]+\] .*| \d+ files? changed.*| (delete|create|rename) mode .*)$"
)


def _strip_git_chatter(out: str) -> str:
    return "".join(
        line + "\n" for line in out.splitlines() if not _GIT_CHATTER.match(line)
    )


def _bash(argv: list[str], repo: Path) -> tuple[str, str, int]:
    env = engine_env(str(repo))
    env["_TICKET_TEST_NO_SYNC"] = "1"
    cp = subprocess.run(
        ["bash", str(dispatcher()), *argv],
        env=env, cwd=str(repo), capture_output=True, text=True,
    )
    return cp.stdout, cp.stderr, cp.returncode


def _inproc(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[str, str, int]:
    capsys.readouterr()
    code = main(argv)
    cap = capsys.readouterr()
    return cap.out, cap.err, code


@pytest.fixture(autouse=True)
def _no_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_TICKET_TEST_NO_SYNC", "1")


# (dir name, jira_key) — the standard mixed fixture set.
_FIXTURES = [
    ("jira-keep1", "DIG-1"),
    ("jira-keep2", "DIG-2"),
    ("jira-del1", "OTHER-5"),
    ("jira-del2", "FOO-9"),
    ("jira-skip1", ""),        # empty jira_key → no project key
    ("jira-skip2", None),      # no jira_key field → no project key
    ("native-xyz", "DIG-3"),   # non-jira- prefix → never scanned
]


def _seed(repo: Path) -> None:
    tracker = repo / ".tickets-tracker"
    for name, key in _FIXTURES:
        d = tracker / name
        d.mkdir(parents=True, exist_ok=True)
        data: dict = {} if key is None else {"jira_key": key}
        (d / "100-aaaa-CREATE.json").write_text(
            json.dumps({"event_type": "CREATE", "data": data}), encoding="utf-8"
        )
    subprocess.run(["git", "-C", str(tracker), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tracker), "commit", "-q", "--no-verify", "-m", "fixtures"],
        check=True, capture_output=True,
    )


def test_arg_guard_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (
        ["purge-bridge"],                       # missing --keep
        ["purge-bridge", "--keep="],            # empty --keep
        ["purge-bridge", "--bogus"],            # unknown arg
        ["purge-bridge", "--keep=DIG", "extra"],  # unexpected positional
    ):
        b_out, b_err, b_code = _bash(argv, rebar_repo)
        i_out, i_err, i_code = _inproc(argv, capsys)
        assert i_out == b_out, f"{argv}: stdout {i_out!r} vs {b_out!r}"
        assert i_err == b_err, f"{argv}: stderr {i_err!r} vs {b_err!r}"
        assert i_code == b_code, f"{argv}: exit {i_code} vs {b_code}"


def test_empty_store_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # No jira-* dirs at all → "Nothing to delete." on a clean store.
    b_out, b_err, b_code = _bash(["purge-bridge", "--keep=DIG"], rebar_repo)
    i_out, i_err, i_code = _inproc(["purge-bridge", "--keep=DIG"], capsys)
    assert i_out == b_out and i_err == b_err and i_code == b_code == 0


def test_dry_run_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(rebar_repo)
    b_out, b_err, b_code = _bash(["purge-bridge", "--keep=DIG", "--dry-run"], rebar_repo)
    # dry-run is non-mutating, so the same store is safe for both impls.
    i_out, i_err, i_code = _inproc(["purge-bridge", "--keep=DIG", "--dry-run"], capsys)
    assert i_out == b_out, f"stdout {i_out!r} vs {b_out!r}"
    assert i_err == b_err and i_code == b_code == 0


def test_delete_and_commit_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tracker = rebar_repo / ".tickets-tracker"

    # In-process run on the seeded store.
    _seed(rebar_repo)
    i_out, i_err, i_code = _inproc(["purge-bridge", "--keep=DIG"], capsys)
    i_remaining = sorted(p.name for p in tracker.glob("jira-*"))
    i_commit = subprocess.run(
        ["git", "-C", str(tracker), "log", "-1", "--format=%s"],
        capture_output=True, text=True,
    ).stdout.strip()

    # Reset the jira-* dirs and run bash over an identically seeded store.
    for p in tracker.glob("jira-*"):
        subprocess.run(["rm", "-rf", str(p)], check=True)
    _seed(rebar_repo)
    b_out, b_err, b_code = _bash(["purge-bridge", "--keep=DIG"], rebar_repo)
    b_remaining = sorted(p.name for p in tracker.glob("jira-*"))
    b_commit = subprocess.run(
        ["git", "-C", str(tracker), "log", "-1", "--format=%s"],
        capture_output=True, text=True,
    ).stdout.strip()

    assert i_out == _strip_git_chatter(b_out), f"stdout {i_out!r} vs {b_out!r}"
    assert i_code == b_code == 0
    # The two non-DIG jira tickets were deleted; keeps + skips + native survive.
    assert i_remaining == b_remaining == ["jira-keep1", "jira-keep2", "jira-skip1", "jira-skip2"]
    assert i_commit == b_commit == "purge: remove 2 non-DIG Jira-sourced (jira-*) tickets"
