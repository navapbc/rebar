"""Check cold-start journal counters without saved offsets.

Existing markers predate monitoring and must publish 0 while the cursor is
seeded. Markers arriving after initialization then publish normally. The
log-file counter keeps its separate bare-total state.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
from _journald_stub import JOURNALCTL_EMULATOR, TIMEOUT_STUB
from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40
_MARKERS_IN_JOURNAL = 7
# (metric published to CloudWatch, env var naming its persisted offset file, markers the stub
# journal emits per iteration — the rolled-up review_interrupts matches BOTH reason-scoped lines)
_TARGETS = (
    ("replication_errors", "REPL_OFFSET_FILE", 1),
    ("voter_errors", "VOTER_OFFSET_FILE", 1),
    ("review_bot_merge_change_errors", "MERGE_OFFSET_FILE", 1),
    ("deploy_errors", "DEPLOY_OFFSET_FILE", 1),
    ("deploy_deferrals", "DEFER_OFFSET_FILE", 1),
    ("review_interrupts", "INTERRUPT_OFFSET_FILE", 2),
    ("review_interrupts_bound_exceeded", "INTERRUPT_BOUND_OFFSET_FILE", 1),
    ("review_interrupts_signal_unavailable", "INTERRUPT_SIGNAL_OFFSET_FILE", 1),
    ("g2p_dispatch_errors", "G2P_OFFSET_FILE", 1),
)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _environment(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    """Stub the box out, populate the journal, and leave every offset file ABSENT."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"
    # The journal's marker count is read from a file so a test can grow it between runs.
    markers_file = tmp_path / "markers"
    markers_file.write_text(f"{_MARKERS_IN_JOURNAL}\n")
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
    # Serve marker-file records through the cursor emulator's bounded read path.
    _stub(
        bin_dir,
        "journalctl",
        """
        n=$(cat "$MARKERS_FILE")
        : > "$JOURNAL_FILE"
        for _ in $(seq 1 "$n"); do
          printf '%s\\n' 'VOTER_ERROR {"ts": 1}' 'MERGE_CHANGE_ERROR {"ts": 1}' \\
            'AUTODEPLOY_ERROR {"ts": 1}' 'AUTODEPLOY_DEFERRED {"ts": 1}' \\
            'gerrit_to_platform error' \\
            'AUTODEPLOY_REVIEW_INTERRUPT {"ts": 1, "reason": "bound-exceeded"}' \\
            'AUTODEPLOY_REVIEW_INTERRUPT {"ts": 1, "reason": "signal-unavailable"}' \\
            >> "$JOURNAL_FILE"
        done
        """
        + JOURNALCTL_EMULATOR,
    )
    _stub(bin_dir, "timeout", TIMEOUT_STUB)
    _stub(bin_dir, "aws", 'printf \'%s\\n\' "$*" >> "$AWS_LOG"; exit 0')

    # The directory exists (the script mkdir -p's it anyway); the offset FILES do not.
    offsets = tmp_path / "offsets"
    offsets.mkdir()
    paths = {variable: offsets / variable.lower() for _metric, variable, _n in _TARGETS}
    assert not any(path.exists() for path in paths.values())
    repl_log = tmp_path / "replication.log"
    repl_log.write_text("[ERROR]\n" * _MARKERS_IN_JOURNAL)

    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "MARKERS_FILE": str(markers_file),
            "JOURNAL_FILE": str(tmp_path / "journal.txt"),
            "REPL_LOG": str(repl_log),
            **{name: str(path) for name, path in paths.items()},
        }
    )
    return env, {"aws_log": aws_log, "markers": markers_file, "repl_log": repl_log, **paths}


def _values(log: Path, target: str) -> list[int]:
    values: list[int] = []
    for line in log.read_text().splitlines():
        parts = line.split()
        if "--metric-name" not in parts or target not in parts:
            continue
        values.append(int(parts[parts.index("--value") + 1]))
    return values


def _state(path: Path) -> tuple[int, str]:
    """Read journal total and cursor; log-file totals have no cursor."""
    fields = path.read_text().split()
    return int(fields[0]), (fields[1] if len(fields) > 1 else "")


@pytest.mark.parametrize(("target", "offset_variable", "per_interval"), _TARGETS)
def test_cold_start_publishes_zero_and_seeds_the_offset(
    tmp_path: Path, target: str, offset_variable: str, per_interval: int
) -> None:
    """A counter's first run must announce 0, not the journal it inherited."""
    env, paths = _environment(tmp_path)

    result = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)

    assert result.returncode == 0
    # 0, NOT the journal's marker count — everything there predates this counter.
    assert _values(paths["aws_log"], target) == [0]
    # Seed a tail cursor so later reads skip inherited journal entries.
    # The replication-log counter keeps a bare total instead.
    total, cursor = _state(paths[offset_variable])
    if offset_variable == "REPL_OFFSET_FILE":
        assert (total, cursor) == (_MARKERS_IN_JOURNAL, "")
    else:
        assert total == 0
        assert cursor, "no cursor seeded: the next run would re-read the inherited journal"


def test_markers_after_initialisation_still_publish(tmp_path: Path) -> None:
    """Seeding must not be sticky: the next genuinely-new marker publishes 1."""
    env, paths = _environment(tmp_path)

    subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)
    # One more marker of every kind lands after the counters initialised.
    paths["markers"].write_text(f"{_MARKERS_IN_JOURNAL + 1}\n")
    paths["repl_log"].write_text("[ERROR]\n" * (_MARKERS_IN_JOURNAL + 1))
    second = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)

    assert second.returncode == 0
    for target, offset_variable, per_interval in _TARGETS:
        assert _values(paths["aws_log"], target) == [0, per_interval], target
        total, _cursor = _state(paths[offset_variable])
        expected = (
            (_MARKERS_IN_JOURNAL + 1) * per_interval
            if offset_variable == "REPL_OFFSET_FILE"
            else per_interval
        )
        assert total == expected, target
