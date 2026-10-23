"""Check that mirror and replication probes publish a heartbeat on every path.

A failed Gerrit or GitHub comparison publishes ``mirror_out_of_sync=1``. An
absent replication log publishes ``replication_errors=0``; mirror status carries
the stopped-replication signal. Assertions inspect CloudWatch command arguments
without network, AWS, or real Git.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_GERRIT_SHA = "a" * 40
_GITHUB_SHA = "b" * 40
_OFFSET_VARIABLES = (
    "REPL_OFFSET_FILE",
    "VOTER_OFFSET_FILE",
    "MERGE_OFFSET_FILE",
    "DEPLOY_OFFSET_FILE",
    "DEFER_OFFSET_FILE",
    "INTERRUPT_OFFSET_FILE",
    "G2P_OFFSET_FILE",
)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _curl_stub(*, gerrit_sha: str | None) -> str:
    """Answer the Gerrit branch REST read with ``gerrit_sha``, or fail like ``curl -f`` does."""
    if gerrit_sha is None:
        branch_case = "exit 22"  # `curl -f` on an HTTP error: no stdout, non-zero status
    else:
        # Gerrit prefixes its JSON with the )]}' XSSI guard, which the script strips.
        body = ")]}'\n" + json.dumps({"revision": gerrit_sha}) + "\n"
        branch_case = f"printf {shlex.quote(body)}; exit 0"
    return f"""
        for a in "$@"; do
          case "$a" in
            *projects/rebar/branches/main*) {branch_case} ;;
          esac
        done
        case "$*" in *http_code*) printf '200'; exit 0 ;; esac
        printf 'dummy-token'; exit 0
        """


def _environment(
    tmp_path: Path,
    *,
    gerrit_sha: str | None = _GERRIT_SHA,
    github_sha: str | None = _GERRIT_SHA,
    repl_log_lines: str | None = "",
) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"

    _stub(bin_dir, "curl", _curl_stub(gerrit_sha=gerrit_sha))
    if github_sha is None:
        # `git ls-remote` against an unreachable remote: no stdout, non-zero status.
        _stub(bin_dir, "git", "exit 128")
    else:
        _stub(bin_dir, "git", f'printf "{github_sha}\\trefs/heads/main\\n"; exit 0')
    _stub(bin_dir, "logger", "exit 0")
    _stub(bin_dir, "journalctl", "exit 0")
    _stub(bin_dir, "aws", 'printf \'%s\\n\' "$*" >> "$AWS_LOG"; exit 0')

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    repl_log = tmp_path / "replication.log"
    if repl_log_lines is not None:
        repl_log.write_text(repl_log_lines)

    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "REPL_LOG": str(repl_log),
            **{name: str(offsets / name.lower()) for name in _OFFSET_VARIABLES},
        }
    )
    return env, aws_log


def _values(log: Path, metric: str) -> list[int]:
    values: list[int] = []
    for line in log.read_text().splitlines():
        parts = line.split()
        if "--metric-name" not in parts or metric not in parts:
            continue
        values.append(int(parts[parts.index("--value") + 1]))
    return values


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)
    assert result.returncode == 0
    return result


def test_matching_shas_publish_zero(tmp_path: Path) -> None:
    env, aws_log = _environment(tmp_path, gerrit_sha=_GERRIT_SHA, github_sha=_GERRIT_SHA)

    _run(env)

    assert _values(aws_log, "mirror_out_of_sync") == [0]


def test_diverged_shas_publish_one(tmp_path: Path) -> None:
    env, aws_log = _environment(tmp_path, gerrit_sha=_GERRIT_SHA, github_sha=_GITHUB_SHA)

    _run(env)

    assert _values(aws_log, "mirror_out_of_sync") == [1]


@pytest.mark.parametrize(
    ("gerrit_sha", "github_sha", "failed_side"),
    [
        (None, _GERRIT_SHA, "the Gerrit REST read"),
        (_GERRIT_SHA, None, "the GitHub git ls-remote"),
        (None, None, "both reads"),
    ],
)
def test_a_failed_fetch_publishes_one_rather_than_nothing(
    tmp_path: Path, gerrit_sha: str | None, github_sha: str | None, failed_side: str
) -> None:
    """Publish ``1`` when either mirror read fails instead of claiming sync.

    The alarm treats missing data as breaching; a failed comparison must still
    publish. Its 300/3/2 window absorbs an isolated failure.
    """
    env, aws_log = _environment(tmp_path, gerrit_sha=gerrit_sha, github_sha=github_sha)

    _run(env)

    assert _values(aws_log, "mirror_out_of_sync") == [1], (
        f"{failed_side} failed and the probe published "
        f"{_values(aws_log, 'mirror_out_of_sync')} for mirror_out_of_sync; it must publish "
        "exactly [1]. Publishing nothing leaves the alarm with no datapoint, which is the "
        "fail-open ticket bff5-9163-cddd-4158 removed."
    )


def test_replication_errors_publishes_a_zero_heartbeat_without_a_log(tmp_path: Path) -> None:
    """Publish ``replication_errors=0`` when the log is absent.

    The alarm treats missing datapoints as breaching, so a new host still needs
    this zero heartbeat.
    """
    env, aws_log = _environment(tmp_path, repl_log_lines=None)

    _run(env)

    assert _values(aws_log, "replication_errors") == [0]


def test_replication_errors_still_counts_new_failures(tmp_path: Path) -> None:
    """ANTI-VACUITY for the heartbeat above: a present log still publishes its delta."""
    env, aws_log = _environment(tmp_path, repl_log_lines="[ERROR] push failed\n" * 3)

    _run(env)

    assert _values(aws_log, "replication_errors") == [0]  # cold start seeds, publishes 0

    _run(env)

    assert _values(aws_log, "replication_errors") == [0, 0]

    Path(env["REPL_LOG"]).write_text("[ERROR] push failed\n" * 5)
    _run(env)

    assert _values(aws_log, "replication_errors") == [0, 0, 2]
