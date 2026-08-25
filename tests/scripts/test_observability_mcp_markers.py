"""The mcp blue-green markers are published as their own metrics (panicky-sylphish-foxterrier).

`autodeploy.sh`'s `mcp` target emits two countable journal markers — ``AUTODEPLOY_MCP_RETIRE_CAP``
(the blue/green port pool is exhausted / too many releases still draining) and
``AUTODEPLOY_MCP_MEM_ABORT`` (the 8 GiB box was below the memory floor, so the 2x overlap was
refused). `observability.sh` counts each into its own ``rebar/host`` metric
(``mcp_retire_cap`` / ``mcp_mem_abort``) via a record-anchored ERE so the alarms in
``monitoring_foxterrier.tf`` can page on them.

These tests drive the REAL ``observability.sh`` over a synthetic unit journal and assert:
- each marker is counted into its OWN metric with the correct delta offset,
- the two tokens do NOT cross-contaminate each other's counter,
- the record anchor (`^<TOKEN> {`) means prose merely NAMING a marker mid-line is never counted
  (the bug-8c2f record-anchor contract), and
- the counters are deltas (a second run counts only markers that arrived since the first).

This is the coverage the LLM-Review gate flagged as missing on patch set 1 of change 2212.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40

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
    "MCP_RETIRE_CAP_OFFSET_FILE",
    "MCP_MEM_ABORT_OFFSET_FILE",
    "G2P_OFFSET_FILE",
)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _marker(token: str, reason: str, detail: str) -> str:
    """Reproduce the EXACT line autodeploy.sh `marker()` writes (ts/reason/detail JSON)."""
    payload = json.dumps({"ts": 1787645217, "reason": reason, "detail": detail})
    return f"{token} {payload}"


def _retire_cap_marker() -> str:
    return _marker(
        "AUTODEPLOY_MCP_RETIRE_CAP",
        "over-cap",
        "managed mcp containers=4 > cap=3; NOT forcing a kill (containers still draining)",
    )


def _mem_abort_marker() -> str:
    return _marker(
        "AUTODEPLOY_MCP_MEM_ABORT",
        "low-memory",
        "MemAvailable=512MB < min 1024MB on the 8GiB box; refusing the blue-green 2x overlap",
    )


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
        # An explicit 0 in every offset: an ABSENT offset file is a cold start (seeds to the
        # journal total and publishes 0 — bug e2a6, asserted separately below).
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


def test_each_mcp_marker_counts_into_its_own_metric(tmp_path: Path) -> None:
    """Both tokens are counted, each into its OWN metric — no cross-contamination."""
    lines = [
        _retire_cap_marker(),
        _mem_abort_marker(),
        _retire_cap_marker(),
    ]
    env, aws_log, paths = _environment(tmp_path, lines)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, "mcp_retire_cap") == [2]
    assert _values(aws_log, "mcp_mem_abort") == [1]
    assert paths["MCP_RETIRE_CAP_OFFSET_FILE"].read_text().strip() == "2"
    assert paths["MCP_MEM_ABORT_OFFSET_FILE"].read_text().strip() == "1"


def test_prose_naming_a_marker_is_not_counted(tmp_path: Path) -> None:
    """Record-anchored (`^<TOKEN> {`): a log sentence mentioning the marker name mid-line must
    never inflate the counter (bug 8c2f)."""
    lines = [
        _retire_cap_marker(),
        "note: an AUTODEPLOY_MCP_RETIRE_CAP was seen earlier; investigating",
        "AUTODEPLOY_MCP_MEM_ABORT happened during the last incident (prose, no JSON)",
    ]
    env, aws_log, _paths = _environment(tmp_path, lines)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, "mcp_retire_cap") == [1]
    assert _values(aws_log, "mcp_mem_abort") == [0]


def test_cold_start_seeds_offsets_and_publishes_zero(tmp_path: Path) -> None:
    """No offset file yet -> publish 0, never the inherited journal count (bug e2a6)."""
    env, aws_log, paths = _environment(
        tmp_path, [_retire_cap_marker()] * 3 + [_mem_abort_marker()] * 2, seed_offsets=False
    )

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, "mcp_retire_cap") == [0]
    assert _values(aws_log, "mcp_mem_abort") == [0]
    assert paths["MCP_RETIRE_CAP_OFFSET_FILE"].read_text().strip() == "3"
    assert paths["MCP_MEM_ABORT_OFFSET_FILE"].read_text().strip() == "2"


def test_second_run_publishes_only_new_markers(tmp_path: Path) -> None:
    """The counters are DELTAS: a second run counts only markers that arrived since the first."""
    env, aws_log, paths = _environment(tmp_path, [_retire_cap_marker(), _mem_abort_marker()])

    first = _run(env)
    journal = Path(env["JOURNAL_FILE"])
    journal.write_text(journal.read_text() + _mem_abort_marker() + "\n")
    second = _run(env)

    assert (first.returncode, second.returncode) == (0, 0)
    assert _values(aws_log, "mcp_retire_cap") == [1, 0]
    assert _values(aws_log, "mcp_mem_abort") == [1, 1]
    assert paths["MCP_RETIRE_CAP_OFFSET_FILE"].read_text().strip() == "1"
    assert paths["MCP_MEM_ABORT_OFFSET_FILE"].read_text().strip() == "2"
