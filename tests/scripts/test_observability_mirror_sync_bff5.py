"""The mirror/replication probes publish on EVERY path, never silently (ticket bff5-9163-cddd-4158).

``rebar/host`` alarms treat missing data as *breaching*, which is only sound while the
publisher is a genuine heartbeat: a healthy run must still put a datapoint. Two sections
used to fall silent instead.

* §5 ``mirror_out_of_sync`` published nothing when the Gerrit REST read or the GitHub
  ``git ls-remote`` failed. That was a FAIL-OPEN on the old ``notBreaching`` alarm and is an
  ambiguous non-signal on the new one, so a failed comparison now publishes ``1``.
* §3 ``replication_errors`` published nothing when ``$REPL_LOG`` was absent (a rebuilt host,
  a site volume that has not mounted, a Gerrit that has never started). It now publishes a
  ``0`` heartbeat there; ``mirror_out_of_sync`` owns the "replication actually stopped" case.

Every assertion is on the ``aws cloudwatch put-metric-data`` argv the script emits — no
network, no AWS, no real ``git``.
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
    """The regression guard: an unmakeable comparison must not fall silent.

    Silence is the one value this section may not emit. On the old ``notBreaching`` alarm it
    read as health (the fail-open this ticket fixes); on the current ``breaching`` alarm it is
    indistinguishable from a dead probe. ``1`` is the honest report — ``0`` would assert the
    in-sync claim the failed comparison cannot make — and the alarm's 300/3/2 window
    (``monitoring_ws7.tf``) absorbs an isolated blip.
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
    """§3 must publish even when ``$REPL_LOG`` is absent, or its alarm is not a heartbeat.

    ``monitoring_s5.tf`` sets ``treat_missing_data = "breaching"`` on the stated ground that
    the probe publishes every interval. A host whose replication log has not been created yet
    would otherwise emit no datapoint at all and page continuously for a reason that has
    nothing to do with a replication failure.
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
