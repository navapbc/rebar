"""CloudWatch counter offsets acknowledge only successful publication (bug 6a65)."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40
_TARGETS = (
    ("replication_errors", "REPL_OFFSET_FILE"),
    ("voter_errors", "VOTER_OFFSET_FILE"),
    ("review_bot_merge_change_errors", "MERGE_OFFSET_FILE"),
    ("deploy_errors", "DEPLOY_OFFSET_FILE"),
    ("deploy_deferrals", "DEFER_OFFSET_FILE"),
    ("review_interrupts", "INTERRUPT_OFFSET_FILE"),
    ("g2p_dispatch_errors", "G2P_OFFSET_FILE"),
)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _environment(tmp_path: Path, target: str) -> tuple[dict[str, str], dict[str, Path]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"
    failed_marker = tmp_path / "failed"
    _stub(
        bin_dir,
        "curl",
        f"""
        for a in "$@"; do
          case "$a" in
            *projects/rebar/branches/main*)
              printf ")]}}'\\n"; printf '{{"revision": "{_SHA}"}}\\n'; exit 0 ;;
          esac
        done
        case "$*" in *http_code*) printf '200'; exit 0 ;; esac
        printf 'dummy-token'; exit 0
        """,
    )
    _stub(bin_dir, "git", f'printf "{_SHA}\\trefs/heads/main\\n"; exit 0')
    _stub(bin_dir, "logger", "exit 0")
    _stub(
        bin_dir,
        "journalctl",
        """
        for _ in {1..7}; do
          printf '%s\\n' 'VOTER_ERROR {"ts": 1}' 'MERGE_CHANGE_ERROR {"ts": 1}'
          printf '%s\\n' 'AUTODEPLOY_ERROR {"ts": 1}' 'AUTODEPLOY_DEFERRED {"ts": 1}'
          printf '%s\\n' 'AUTODEPLOY_REVIEW_INTERRUPT {"ts": 1}'
          printf '%s\\n' 'gerrit_to_platform error'
        done
        """,
    )
    _stub(
        bin_dir,
        "aws",
        """
        printf '%s\\n' "$*" >> "$AWS_LOG"
        case "$*" in
          *"--metric-name $FAIL_METRIC "*)
            if [ ! -e "$FAILED_MARKER" ]; then
              : > "$FAILED_MARKER"
              exit 23
            fi
            ;;
        esac
        exit 0
        """,
    )

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    paths: dict[str, Path] = {}
    for _metric, variable in _TARGETS:
        path = offsets / variable.lower()
        path.write_bytes(b"2\n")
        paths[variable] = path
    repl_log = tmp_path / "replication.log"
    repl_log.write_text("[ERROR]\n" * 7)

    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "FAILED_MARKER": str(failed_marker),
            "FAIL_METRIC": target,
            "REPL_LOG": str(repl_log),
            **{name: str(path) for name, path in paths.items()},
        }
    )
    return env, {"aws_log": aws_log, "failed": failed_marker, **paths}


def _values(log: Path, target: str) -> list[int]:
    values: list[int] = []
    for line in log.read_text().splitlines():
        parts = line.split()
        if "--metric-name" not in parts or target not in parts:
            continue
        values.append(int(parts[parts.index("--value") + 1]))
    return values


def _run_twice(
    env: dict[str, str],
) -> tuple[subprocess.CompletedProcess, subprocess.CompletedProcess]:
    first = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)
    second = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)
    return first, second


@pytest.mark.parametrize(("target", "offset_variable"), _TARGETS)
def test_failed_publish_retains_offset_and_retries_accumulated_delta(
    tmp_path: Path, target: str, offset_variable: str
) -> None:
    env, paths = _environment(tmp_path, target)

    first = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)
    offset_after_failure = paths[offset_variable].read_bytes()
    second = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)

    assert first.returncode == second.returncode == 0
    assert paths["failed"].exists()
    assert (offset_after_failure, paths[offset_variable].read_bytes()) == (b"2\n", b"7\n")
    assert _values(paths["aws_log"], target) == [5, 5]


def test_shrunken_journal_holds_the_offset_across_a_failed_publish(tmp_path: Path) -> None:
    """A lost-history reset still obeys publish-then-persist, and publishes 0 (bug 2dc7).

    The offset contract under test here is unchanged: a failed publish must NOT advance the
    offset. The published VALUE is 0 rather than the surviving total — republishing the
    survivors would double-count markers already counted under the larger offset. See
    ``test_observability_rotation_2dc7.py`` for that behaviour's own coverage.
    """
    env, paths = _environment(tmp_path, "voter_errors")
    paths["VOTER_OFFSET_FILE"].write_bytes(b"10\n")

    first = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)
    offset_after_failure = paths["VOTER_OFFSET_FILE"].read_bytes()
    second = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)

    assert first.returncode == second.returncode == 0
    assert (offset_after_failure, paths["VOTER_OFFSET_FILE"].read_bytes()) == (b"10\n", b"7\n")
    assert _values(paths["aws_log"], "voter_errors") == [0, 0]


def test_zero_delta_remains_compatible(tmp_path: Path) -> None:
    env, paths = _environment(tmp_path, "voter_errors")
    paths["VOTER_OFFSET_FILE"].write_bytes(b"7\n")

    first, second = _run_twice(env)

    assert first.returncode == second.returncode == 0
    assert paths["VOTER_OFFSET_FILE"].read_bytes() == b"7\n"
    assert _values(paths["aws_log"], "voter_errors") == [0, 0]
