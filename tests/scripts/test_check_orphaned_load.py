"""Contract for the orphaned-CPU-load detector (bug f3d9-ecd8-df72-4fa1).

On 2026-08-22 three agent sessions spawned ``python -c "while True: pass"`` CPU
saturation workers to reproduce a timing-dependent failure. Nothing reaped them:
38 orphaned to PID 1 and ran for four days at ~1341% combined CPU. This module
covers the on-demand detector that would have surfaced them.

**This test never spawns a real CPU burner.** Doing so would reproduce the very
defect the ticket tracks, so the process-inspection seam is injected and every
process record here is synthetic. The one exception is a READ-ONLY smoke
assertion against the real adapter, which covers the live ``ps``-parsing path
the injected seam bypasses.

API contract (scripts/check_orphaned_load.py):
  - DEFAULT_MIN_CPU_SECONDS: float                       # 3600
  - ORPHAN_PPID: int                                     # 1
  - ProcessRecord(pid, ppid, cpu_seconds, command)
  - parse_cpu_time(value: str) -> float                  # ps TIME -> seconds
  - list_processes() -> list[ProcessRecord]              # real adapter (read-only)
  - find_orphaned_load(records, min_cpu_seconds) -> list[ProcessRecord]
  - main(argv=None, *, lister=None) -> int               # 0 clean, 1 findings
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.scripts

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_orphaned_load.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_orphaned_load", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves its own class's module through
    # sys.modules, so a path-loaded module that skips this fails at import.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _leaked_decoy(gate: ModuleType) -> object:
    """A synthetic stand-in for one of the 38 leaked workers: PPID 1, days of CPU."""
    return gate.ProcessRecord(
        pid=4242,
        ppid=1,
        cpu_seconds=138_000.0,
        command='python -c "while True: pass"',
    )


def test_default_threshold_is_3600_cpu_seconds(gate: ModuleType) -> None:
    assert gate.DEFAULT_MIN_CPU_SECONDS == 3600
    assert gate.ORPHAN_PPID == 1


def test_flags_orphaned_high_cpu_process(gate: ModuleType) -> None:
    found = gate.find_orphaned_load([_leaked_decoy(gate)], gate.DEFAULT_MIN_CPU_SECONDS)
    assert [record.pid for record in found] == [4242]


def test_ignores_normally_parented_process_however_hot(gate: ModuleType) -> None:
    """A long-running process with a live parent is somebody's business, not a leak."""
    parented = gate.ProcessRecord(
        pid=4243,
        ppid=99_001,
        cpu_seconds=138_000.0,
        command='python -c "while True: pass"',
    )
    assert gate.find_orphaned_load([parented], gate.DEFAULT_MIN_CPU_SECONDS) == []


def test_ignores_orphan_below_the_cpu_threshold(gate: ModuleType) -> None:
    """Plenty of legitimate daemons are reparented to PID 1; CPU time is the signal."""
    quiet_orphan = gate.ProcessRecord(
        pid=4244, ppid=1, cpu_seconds=12.5, command="/opt/homebrew/bin/some-helper"
    )
    assert gate.find_orphaned_load([quiet_orphan], gate.DEFAULT_MIN_CPU_SECONDS) == []


def test_threshold_is_exclusive_at_the_boundary(gate: ModuleType) -> None:
    at_threshold = gate.ProcessRecord(pid=4245, ppid=1, cpu_seconds=3600.0, command="borderline")
    just_over = gate.ProcessRecord(pid=4246, ppid=1, cpu_seconds=3600.5, command="borderline")
    found = gate.find_orphaned_load([at_threshold, just_over], gate.DEFAULT_MIN_CPU_SECONDS)
    assert [record.pid for record in found] == [4246]


def test_honours_a_lowered_threshold(gate: ModuleType) -> None:
    quiet_orphan = gate.ProcessRecord(
        pid=4244, ppid=1, cpu_seconds=12.5, command="/opt/homebrew/bin/some-helper"
    )
    assert [r.pid for r in gate.find_orphaned_load([quiet_orphan], 10.0)] == [4244]


def test_findings_sort_hottest_first(gate: ModuleType) -> None:
    hot = gate.ProcessRecord(pid=1, ppid=1, cpu_seconds=138_000.0, command="hot")
    warm = gate.ProcessRecord(pid=2, ppid=1, cpu_seconds=7_200.0, command="warm")
    found = gate.find_orphaned_load([warm, hot], gate.DEFAULT_MIN_CPU_SECONDS)
    assert [record.command for record in found] == ["hot", "warm"]


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("0:00.00", 0.0),
        ("12:34.56", 754.56),
        ("1:02:03", 3723.0),
        ("2-03:04:05", 183_845.0),
        ("2298:04.00", 137_884.0),
    ],
)
def test_parse_cpu_time_handles_every_ps_shape(
    gate: ModuleType, field: str, expected: float
) -> None:
    assert gate.parse_cpu_time(field) == pytest.approx(expected)


def test_parse_cpu_time_rejects_garbage(gate: ModuleType) -> None:
    with pytest.raises(ValueError):
        gate.parse_cpu_time("not-a-time")


# ---------------------------------------------------------------------------
# main() over the injected seam
# ---------------------------------------------------------------------------


def test_main_exits_nonzero_and_names_the_leak(
    gate: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code = gate.main([], lister=lambda: [_leaked_decoy(gate)])
    out = capsys.readouterr().out
    assert code == 1
    assert "4242" in out
    assert "while True: pass" in out


def test_main_exits_zero_when_only_parented_load_exists(
    gate: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    parented = gate.ProcessRecord(pid=7, ppid=500, cpu_seconds=138_000.0, command="busy-but-owned")
    assert gate.main([], lister=lambda: [parented]) == 0
    assert "busy-but-owned" not in capsys.readouterr().out


def test_main_honours_min_cpu_seconds_argument(gate: ModuleType) -> None:
    quiet_orphan = gate.ProcessRecord(pid=8, ppid=1, cpu_seconds=12.5, command="quiet")
    assert gate.main([], lister=lambda: [quiet_orphan]) == 0
    assert gate.main(["--min-cpu-seconds", "10"], lister=lambda: [quiet_orphan]) == 1


def test_help_exits_zero(gate: ModuleType) -> None:
    with pytest.raises(SystemExit) as excinfo:
        gate.main(["--help"])
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# The one live-system assertion: READ-ONLY, no process is spawned or signalled
# ---------------------------------------------------------------------------


def test_real_adapter_reads_live_process_table_without_raising(gate: ModuleType) -> None:
    """Covers the real ``ps`` parsing path the injected seam bypasses.

    Read-only by construction: it lists processes and asserts shape. It never
    spawns a load generator and never signals anything.
    """
    records = gate.list_processes()
    assert isinstance(records, list)
    assert records, "the live process table is never empty"
    assert all(isinstance(record, gate.ProcessRecord) for record in records)
    assert all(record.cpu_seconds >= 0 for record in records)
    assert os.getpid() not in {record.pid for record in records}


# ---------------------------------------------------------------------------
# System-owned exclusion (measured: 33 flagged -> 29 launchd daemons -> 4 real)
# ---------------------------------------------------------------------------


def _system_daemon(gate: ModuleType) -> object:
    """WindowServer: the loudest of the 29 launchd daemons measured on the host."""
    return gate.ProcessRecord(
        pid=608,
        ppid=1,
        cpu_seconds=401_613.6,
        command=(
            "/System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer -daemon"
        ),
    )


def _orphaned_agent_job(gate: ModuleType) -> object:
    """A real hit of the target class, buried among the daemons on this host."""
    return gate.ProcessRecord(
        pid=26898,
        ppid=1,
        cpu_seconds=10_958.4,
        command="/bin/bash /Users/joeoakhart/.claude/jobs/09e12a48/tmp/watch-tracker.sh",
    )


@pytest.mark.parametrize(
    "command",
    [
        "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder",
        "/usr/libexec/opendirectoryd",
        "/usr/sbin/cfprefsd daemon",
        "/usr/bin/some-system-tool",
        "/Library/Apple/System/Library/somebinary",
        "/Library/Application Support/JAMF/Jamf.app/Contents/MacOS/JamfDaemon",
    ],
)
def test_is_system_owned_recognises_measured_offenders(gate: ModuleType, command: str) -> None:
    assert gate.is_system_owned(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "/bin/bash /Users/joeoakhart/.claude/jobs/09e12a48/tmp/watch-tracker.sh",
        '/opt/homebrew/bin/python -c "while True: pass"',
        "/usr/local/logscale-collector/logscale-collector --cfg config.yaml",
        "/Applications/Caffeine.app/Contents/MacOS/Caffeine",
        "python -c 'while True: pass'",
    ],
)
def test_is_system_owned_leaves_user_space_alone(gate: ModuleType, command: str) -> None:
    assert gate.is_system_owned(command) is False


def test_system_daemon_only_matches_at_the_start_of_the_command_line(gate: ModuleType) -> None:
    """A path mentioned as an ARGUMENT must not launder a user-space process."""
    assert gate.is_system_owned("/bin/bash /System/Library/whatever.sh") is False


def test_system_daemons_are_excluded_by_default(gate: ModuleType) -> None:
    records = [_system_daemon(gate), _orphaned_agent_job(gate)]
    found = gate.find_orphaned_load(records, gate.DEFAULT_MIN_CPU_SECONDS)
    assert [record.pid for record in found] == [26898]


def test_include_system_reports_the_daemons_too(gate: ModuleType) -> None:
    records = [_system_daemon(gate), _orphaned_agent_job(gate)]
    found = gate.find_orphaned_load(records, gate.DEFAULT_MIN_CPU_SECONDS, include_system=True)
    assert [record.pid for record in found] == [608, 26898]


def test_main_suppresses_system_daemons_and_says_how_many(
    gate: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    records = [_system_daemon(gate), _orphaned_agent_job(gate)]
    code = gate.main([], lister=lambda: list(records))
    out = capsys.readouterr().out
    assert code == 1
    assert "26898" in out
    assert "WindowServer" not in out
    assert "1 system-owned" in out
    assert "docs/orphaned-processes.md" in out


def test_main_include_system_flag_reports_the_daemons(
    gate: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    records = [_system_daemon(gate), _orphaned_agent_job(gate)]
    assert gate.main(["--include-system"], lister=lambda: list(records)) == 1
    out = capsys.readouterr().out
    assert "WindowServer" in out
    assert "system-owned" not in out


def test_main_exits_zero_when_every_orphan_is_a_system_daemon(
    gate: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 29-daemon case: nothing actionable, so the check must stay quiet."""
    assert gate.main([], lister=lambda: [_system_daemon(gate)]) == 0
    assert capsys.readouterr().out == ""
