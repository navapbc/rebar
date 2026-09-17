from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "gerrit" / "feature-branch-inventory.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _git_stub(calls: Path) -> str:
    """Fake `git` used by the inventory script under test."""
    return f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {calls}
case "$1" in
  credential)
    # Real `git credential fill` reads the request before answering. Draining it
    # keeps this arm alive until the writer closes, so the caller's
    # `printf ... | git credential fill` under `set -o pipefail` cannot lose the
    # race and take SIGPIPE.
    cat >/dev/null
    printf 'password=secret\\n'
    ;;
  ls-remote)
    printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\trefs/heads/main\\n'
    ;;
  merge-base)
    [ "$3" = "cccccccccccccccccccccccccccccccccccccccc" ] && exit 0
    exit 1
    ;;
  diff)
    if [ "$2" = "--name-only" ]; then
      printf 'src/rebar/example.py\\n'
    elif [ "$2" = "--quiet" ] && [ "$4" = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" ]; then
      exit 0
    else
      exit 1
    fi
    ;;
  *)
    exit 2
    ;;
esac
"""


def test_rebased_content_equivalent_branch_reports_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    calls = tmp_path / "git-calls"
    _write_executable(fakebin / "git", _git_stub(calls))
    _write_executable(
        fakebin / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
url="${@: -1}"
case "$url" in
  *'/branches/')
    printf '['
    printf '%s' '{"ref":"refs/heads/feature/rebased",'
    printf '%s' '"revision":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},'
    printf '%s' '{"ref":"refs/heads/feature/direct",'
    printf '%s' '"revision":"cccccccccccccccccccccccccccccccccccccccc"},'
    printf '%s' '{"ref":"refs/heads/feature/unmerged",'
    printf '%s' '"revision":"dddddddddddddddddddddddddddddddddddddddd"}'
    printf ']'
    ;;
  *'/commits/'*)
    printf '{"committer":{"date":"2026-09-01 00:00:00.000000000"}}'
    ;;
  *)
    exit 2
    ;;
esac
""",
    )
    bash_env = tmp_path / "bash-env"
    bash_env.write_text("exit 0\n")
    monkeypatch.setenv("BASH_ENV", str(bash_env))
    env = os.environ | {
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "GERRIT_HOST": "example.test",
        "GERRIT_USER": "tester",
        "PROJECT": "rebar",
        "NOW_EPOCH": "1788652800",
    }
    env.pop("BASH_ENV", None)
    env.pop("ENV", None)

    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, check=False
    )

    rows = re.findall(r"^(feature/\S+)\s+(\S+)", result.stdout, re.MULTILINE)
    assert rows == [
        ("feature/rebased", "MERGED-REBASED"),
        ("feature/direct", "MERGED-BACK"),
        ("feature/unmerged", "NOT-IN-MAIN"),
    ], (
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}\n"
        f"git calls={calls.read_text() if calls.exists() else '<none>'!r}"
    )


def test_credential_stub_drains_stdin_so_writer_cannot_take_sigpipe(
    tmp_path: Path,
) -> None:
    """Regression oracle for the pipefail/SIGPIPE class that made this file flaky.

    ``feature-branch-inventory.sh`` runs ``printf ... | git credential fill``
    under ``set -o pipefail``. A stub that answers without consuming stdin can
    exit before the writer's write is issued; the writer then takes SIGPIPE,
    pipefail fails the pipeline, and the script aborts with ``credential fill
    failed`` (exit 2) for reasons unrelated to the behaviour under test.

    Delaying the writer past the point where a non-draining stub would already
    have exited makes that race deterministic rather than scheduling-dependent:
    this fails every run against a stub that does not drain, and passes every
    run against one that does.
    """
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    calls = tmp_path / "git-calls"
    git = fakebin / "git"
    _write_executable(git, _git_stub(calls))

    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -o pipefail; { sleep 0.5; printf "protocol=https\\nhost=example.test\\n\\n"; } '
            '| "$1" credential fill',
            "_",
            str(git),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, (
        "credential stub exited before draining stdin, so the writer took SIGPIPE "
        f"under pipefail: returncode={result.returncode} stderr={result.stderr!r}"
    )
    assert "password=" in result.stdout
