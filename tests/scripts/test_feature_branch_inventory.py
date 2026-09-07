from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "gerrit" / "feature-branch-inventory.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_rebased_content_equivalent_branch_reports_merged(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    calls = tmp_path / "git-calls"
    _write_executable(
        fakebin / "git",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {calls}
case "$1" in
  credential)
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
""",
    )
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
    env = os.environ | {
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "GERRIT_HOST": "example.test",
        "GERRIT_USER": "tester",
        "PROJECT": "rebar",
        "NOW_EPOCH": "1788652800",
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, check=False
    )

    assert re.findall(r"^(feature/\S+)\s+(\S+)", result.stdout, re.MULTILINE) == [
        ("feature/rebased", "MERGED-REBASED"),
        ("feature/direct", "MERGED-BACK"),
        ("feature/unmerged", "NOT-IN-MAIN"),
    ]
