"""The AUTODEPLOY_DISK_PRESSURE marker is published as a metric (task 9d15-d576-e0ca-4596).

`autodeploy.sh` emits a countable ``AUTODEPLOY_DISK_PRESSURE`` journal marker whenever the
pressure-triggered reclaim fires (story 28f9), but story 28f9 intentionally scoped the metric
out, so whether the reclaim gate ran at all was readable ONLY from the host journal. During
the 2026-08-17 root-disk incident that made a first-order question — "did the reclaim gate
run?" — unanswerable from CloudWatch. These tests drive the REAL ``observability.sh`` over a
synthetic unit journal and assert the marker is counted into ``rebar/host:disk_pressure_prunes``
with a record-anchored pattern and its own delta offset, and that a cold start seeds to 0
rather than publishing the inherited journal count.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40

METRIC = "disk_pressure_prunes"

_OFFSET_VARIABLES = (
    "REPL_OFFSET_FILE",
    "VOTER_OFFSET_FILE",
    "MERGE_OFFSET_FILE",
    "DEPLOY_OFFSET_FILE",
    "DEFER_OFFSET_FILE",
    "INTERRUPT_OFFSET_FILE",
    "INTERRUPT_BOUND_OFFSET_FILE",
    "INTERRUPT_SIGNAL_OFFSET_FILE",
    "DISK_PRESSURE_OFFSET_FILE",
    "G2P_OFFSET_FILE",
)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _marker() -> str:
    """Reproduce the exact line autodeploy.sh `marker()` writes for a pressure prune."""
    payload = json.dumps(
        {"ts": 1787175217, "reason": "pressure-prune", "detail": "root disk at 92% (threshold 90%)"}
    )
    return f"AUTODEPLOY_DISK_PRESSURE {payload}"


def _environment(
    tmp_path: Path, journal_lines: list[str], *, seed_offsets: bool = True
) -> tuple[dict[str, str], Path, dict[str, Path]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"

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
    _stub(bin_dir, "aws", 'printf \'%s\\n\' "$*" >> "$AWS_LOG"; exit 0')

    journal = tmp_path / "journal.txt"
    journal.write_text("".join(f"{line}\n" for line in journal_lines))
    _stub(bin_dir, "journalctl", 'cat "$JOURNAL_FILE"; exit 0')

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    paths = {name: offsets / name.lower() for name in _OFFSET_VARIABLES}
    if seed_offsets:
        # An explicit 0 in every offset: these tests are about the disk-pressure counter, and
        # an absent offset file is a cold start (seeds to the journal total and publishes 0 —
        # bug e2a6, asserted separately below).
        for path in paths.values():
            path.write_text("0\n")
    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "JOURNAL_FILE": str(journal),
            "REPL_LOG": str(tmp_path / "replication.log"),
            **{name: str(path) for name, path in paths.items()},
        }
    )
    (tmp_path / "replication.log").write_text("")
    return env, aws_log, paths


def _values(log: Path, metric: str) -> list[int]:
    values: list[int] = []
    if not log.exists():
        return values
    for line in log.read_text().splitlines():
        parts = line.split()
        if "--metric-name" not in parts:
            continue
        if parts[parts.index("--metric-name") + 1] != metric:
            continue
        values.append(int(parts[parts.index("--value") + 1]))
    return values


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)


def test_disk_pressure_markers_are_counted_into_their_own_metric(tmp_path: Path) -> None:
    """Marker lines are counted into disk_pressure_prunes; prose naming the marker is not.

    The prose line is the record-anchor regression (bug 8c2f): only journal records that BEGIN
    with `AUTODEPLOY_DISK_PRESSURE {` may count, so a log sentence that mentions the marker
    name mid-line never inflates the counter.
    """
    lines = [_marker(), "saw an AUTODEPLOY_DISK_PRESSURE marker earlier today", _marker()]
    env, aws_log, paths = _environment(tmp_path, lines)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, METRIC) == [2]
    assert paths["DISK_PRESSURE_OFFSET_FILE"].read_text().strip() == "2"


def test_cold_start_seeds_the_offset_and_publishes_zero(tmp_path: Path) -> None:
    """No offset file yet -> publish 0, never the inherited journal count (bug e2a6)."""
    env, aws_log, paths = _environment(tmp_path, [_marker()] * 3, seed_offsets=False)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, METRIC) == [0]
    assert paths["DISK_PRESSURE_OFFSET_FILE"].read_text().strip() == "3"


def test_second_run_publishes_only_the_new_markers(tmp_path: Path) -> None:
    """The counter is a DELTA: a second run counts only markers that arrived since the first."""
    env, aws_log, paths = _environment(tmp_path, [_marker()] * 2)

    first = _run(env)
    journal = Path(env["JOURNAL_FILE"])
    journal.write_text(journal.read_text() + _marker() + "\n")
    second = _run(env)

    assert (first.returncode, second.returncode) == (0, 0)
    assert _values(aws_log, METRIC) == [2, 1]
    assert paths["DISK_PRESSURE_OFFSET_FILE"].read_text().strip() == "3"
