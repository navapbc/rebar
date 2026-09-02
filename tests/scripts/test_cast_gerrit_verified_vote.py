"""Contract tests for the Gerrit Verified vote helper (ticket adab)."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.scripts

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "cast_gerrit_verified_vote.py"


def _write_fake_ssh(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        """#!/bin/sh
printf '%s\\n' "$@" > "$SSH_ARGV_FILE"
if stat -c %a "$2" >/dev/null 2>&1; then
  stat -c %a "$2" > "$SSH_KEY_MODE_FILE"
else
  stat -f %Lp "$2" > "$SSH_KEY_MODE_FILE"
fi
for arg in "$@"; do
  case "$arg" in
    UserKnownHostsFile=*)
      known_hosts_path=${arg#UserKnownHostsFile=}
      if stat -c %a "$known_hosts_path" >/dev/null 2>&1; then
        stat -c %a "$known_hosts_path" > "$SSH_KNOWN_HOSTS_MODE_FILE"
      else
        stat -f %Lp "$known_hosts_path" > "$SSH_KNOWN_HOSTS_MODE_FILE"
      fi
      ;;
  esac
done
case "$FAKE_SSH_MODE" in
  closed)
    echo "error: fatal: change is closed" >&2
    echo "fatal: one or more reviews failed; review output above" >&2
    exit 1
    ;;
  fail)
    echo "permission denied" >&2
    exit 23
    ;;
  *)
    exit 0
    ;;
esac
""",
        encoding="utf-8",
    )
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _run(
    tmp_path: Path,
    mode: str = "success",
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    bin_dir = _write_fake_ssh(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "SSH_ARGV_FILE": str(tmp_path / "ssh-argv.txt"),
        "SSH_KEY_MODE_FILE": str(tmp_path / "ssh-key-mode.txt"),
        "SSH_KNOWN_HOSTS_MODE_FILE": str(tmp_path / "ssh-known-hosts-mode.txt"),
        "FAKE_SSH_MODE": mode,
        "GERRIT_SSH_PRIVKEY": "private-key",
        "GERRIT_KNOWN_HOSTS": "host-key",
        "GERRIT_SERVER": "rebar.example",
        "GERRIT_SSH_USER": "ci-bot",
        "CHANGE_NUMBER": "2483",
        "PATCHSET_NUMBER": "7",
        "VOTE_TYPE": "success",
        "RUN_URL": "https://github.example/repo/actions/runs/33580445347",
        **overrides,
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_closed_change_is_a_successful_noop(tmp_path: Path) -> None:
    result = _run(tmp_path, mode="closed")

    assert result.returncode == 0
    assert "change is already closed" in result.stdout
    assert "fatal: change is closed" in result.stderr


def test_other_vote_failures_still_fail(tmp_path: Path) -> None:
    result = _run(tmp_path, mode="fail")

    assert result.returncode == 23
    assert "permission denied" in result.stderr


def test_success_vote_uses_the_verified_label_and_run_url(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.returncode == 0
    argv = (tmp_path / "ssh-argv.txt").read_text(encoding="utf-8")
    assert "-p\n29418" in argv
    assert "UserKnownHostsFile=" in argv
    assert "gerrit review 2483,7" in argv
    assert "--label Verified=1" in argv
    assert "SUCCESS: https://github.example/repo/actions/runs/33580445347" in argv


def test_failure_vote_uses_verified_minus_one(tmp_path: Path) -> None:
    result = _run(tmp_path, VOTE_TYPE="failure")

    assert result.returncode == 0
    argv = (tmp_path / "ssh-argv.txt").read_text(encoding="utf-8")
    assert "--label Verified=-1" in argv
    assert "FAILURE: https://github.example/repo/actions/runs/33580445347" in argv


def test_cancelled_vote_uses_verified_minus_one(tmp_path: Path) -> None:
    result = _run(tmp_path, VOTE_TYPE="cancelled")

    assert result.returncode == 0
    argv = (tmp_path / "ssh-argv.txt").read_text(encoding="utf-8")
    assert "--label Verified=-1" in argv
    assert "--label Code-Review=-1" in argv
    assert "CANCELLED: https://github.example/repo/actions/runs/33580445347" in argv


def test_invalid_vote_type_fails_before_ssh(tmp_path: Path) -> None:
    result = _run(tmp_path, VOTE_TYPE="skipped")

    assert result.returncode == 1
    assert "Unknown vote type" in result.stderr
    assert not (tmp_path / "ssh-argv.txt").exists()


def test_invalid_change_and_patchset_numbers_fail_before_ssh(tmp_path: Path) -> None:
    for override in ({"CHANGE_NUMBER": "0"}, {"PATCHSET_NUMBER": "bad"}):
        case_dir = tmp_path / next(iter(override))
        case_dir.mkdir()
        result = _run(case_dir, **override)

        assert result.returncode == 1
        assert "must be a positive integer" in result.stderr
        assert not (case_dir / "ssh-argv.txt").exists()


def test_ssh_secret_files_are_chmod_600_and_removed_after_vote(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.returncode == 0
    assert (tmp_path / "ssh-key-mode.txt").read_text(encoding="utf-8").strip() == "600"
    assert (tmp_path / "ssh-known-hosts-mode.txt").read_text(encoding="utf-8").strip() == "600"
    assert not (tmp_path / "home" / ".ssh" / "gerrit_verified_key").exists()
    assert not (tmp_path / "home" / ".ssh" / "gerrit_verified_known_hosts").exists()
