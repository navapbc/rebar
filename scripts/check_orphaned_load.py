#!/usr/bin/env python3
"""Report orphaned high-CPU helpers without modifying the process table.

Two signals together identify that shape, and neither alone is sufficient:

* ``PPID == 1`` shows that the spawner exited without reaping the child.
* Accumulated CPU above 3,600 seconds separates the observed leaked workers
  (about 138,000 CPU-seconds over four days) from ordinary idle daemons.

``--min-cpu-seconds`` adjusts the threshold for hosts with legitimate long-lived compute.

Because launchd/init also parents legitimate daemons to PID 1, system-owned paths and macOS
``.app/Contents/MacOS`` executables are suppressed by default. The measured unfiltered set
contained 29 system daemons among 33 candidates. ``--include-system`` restores all suppressed
records, and the report always states the suppressed count.

The output is a triage list: inspect each command line before acting.

It is deliberately on demand, not a gate, because live host state is not a property of the
reviewed tree. It has no CI-provider dependency::

    python scripts/check_orphaned_load.py
    python scripts/check_orphaned_load.py --min-cpu-seconds 600

Exit status is zero when nothing is flagged and one otherwise. The command is read-only: it
never signals, kills, or spawns. Teardown remains an operator decision documented in
``docs/orphaned-processes.md``.

Tests inject ``list_processes`` through ``main(lister=...)`` rather than spawning load.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: Parent PID of a process whose spawner exited without reaping it.
ORPHAN_PPID = 1

#: One CPU-hour separates the observed leaked workers from ordinary reparented daemons.
DEFAULT_MIN_CPU_SECONDS = 3600

#: OS and managed-endpoint prefixes whose processes launchd/init legitimately reparents.
#: Home, Homebrew, and local paths remain visible; GUI bundles are classified separately.
_SYSTEM_PATH_PREFIXES = (
    "/System/",
    "/usr/libexec/",
    "/usr/sbin/",
    "/usr/bin/",
    "/sbin/",
    "/usr/lib/systemd/",
    "/Library/Apple/",
    "/Library/Application Support/JAMF/",
)

#: macOS GUI bundles are launchd-owned even when installed by the operator.
_GUI_APP_PREFIXES = ("/Applications/",)

#: ``ps`` output spec. Field order matches ``_parse_ps_line``; ``command=`` is last
#: because it is the only field that can contain spaces.
_PS_FORMAT = "pid=,ppid=,time=,command="

_PS_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class ProcessRecord:
    """One row of the process table, with CPU time already normalised to seconds."""

    pid: int
    ppid: int
    cpu_seconds: float
    command: str


#: The injectable process-inspection seam.
ProcessLister = Callable[[], list[ProcessRecord]]


def parse_cpu_time(value: str) -> float:
    """Convert a ``ps`` TIME field to seconds.

    Handles every shape ``ps`` emits across platforms: ``MM:SS.ss`` (macOS, where
    minutes run unbounded — ``2298:04.00``), ``HH:MM:SS`` (Linux), and the
    ``DD-HH:MM:SS`` form Linux uses past a day. Raises ``ValueError`` on anything
    else rather than silently scoring a malformed row as zero.
    """
    text = value.strip()
    days = 0
    if "-" in text:
        day_text, _, text = text.partition("-")
        days = int(day_text)
    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"unrecognised ps TIME field: {value!r}")
    seconds = float(parts[-1])
    if len(parts) >= 2:
        seconds += int(parts[-2]) * 60
    if len(parts) == 3:
        seconds += int(parts[-3]) * 3600
    return days * 86400 + seconds


def _parse_ps_line(line: str) -> ProcessRecord | None:
    """Parse one ``ps`` row, or return ``None`` if it is not a usable row."""
    fields = line.split(maxsplit=3)
    if len(fields) < 4:
        return None
    try:
        return ProcessRecord(
            pid=int(fields[0]),
            ppid=int(fields[1]),
            cpu_seconds=parse_cpu_time(fields[2]),
            command=fields[3].strip(),
        )
    except ValueError:
        return None


def list_processes() -> list[ProcessRecord]:
    """Read the live process table via ``ps``. Read-only; never signals anything.

    The checker's own process is omitted — it is by definition not a leaked
    orphan, and reporting it would be noise on every run.
    """
    proc = subprocess.run(
        ["ps", "-A", "-o", _PS_FORMAT],
        capture_output=True,
        text=True,
        check=False,
        timeout=_PS_TIMEOUT_SECONDS,
    )
    self_pid = os.getpid()
    records = []
    for line in proc.stdout.splitlines():
        record = _parse_ps_line(line)
        if record is not None and record.pid != self_pid:
            records.append(record)
    return records


def is_system_owned(command: str) -> bool:
    """Is this command line an OS-vendor or endpoint-management executable?

    Matched as a prefix of the whole command line, which anchors it to the
    executable path: a system path appearing later, as an ARGUMENT, must not
    launder a user-space process (``/bin/bash /System/Library/x.sh`` is a user
    process running a system script). Prefix-on-the-whole-string also survives
    the spaces in ``/Library/Application Support/JAMF/…``, which splitting on
    whitespace would not.
    """
    return command.startswith(_SYSTEM_PATH_PREFIXES)


def is_user_gui_app(command: str) -> bool:
    """Is this command line a macOS GUI app bundle executable?"""
    return command.startswith(_GUI_APP_PREFIXES) and ".app/Contents/MacOS/" in command


def is_default_suppressed(command: str) -> bool:
    """Is this process launchd/init-owned noise rather than an agent leak?"""
    return is_system_owned(command) or is_user_gui_app(command)


def find_orphaned_load(
    records: Sequence[ProcessRecord],
    min_cpu_seconds: float,
    *,
    include_system: bool = False,
) -> list[ProcessRecord]:
    """Return the orphaned, CPU-hot subset of ``records``, hottest first.

    Both PPID and CPU conditions are required: a reparented process that has
    burned no CPU is an ordinary daemon, and a CPU-hot process with a live parent
    belongs to whoever started it. Known launchd/init-owned noise is dropped as
    well unless ``include_system`` — see the module docstring for the measurement
    that makes that the default.
    """
    flagged = [
        record
        for record in records
        if record.ppid == ORPHAN_PPID
        and record.cpu_seconds > min_cpu_seconds
        and (include_system or not is_default_suppressed(record.command))
    ]
    return sorted(flagged, key=lambda record: record.cpu_seconds, reverse=True)


def _report(flagged: Sequence[ProcessRecord], min_cpu_seconds: float, suppressed: int) -> None:
    note = f" ({suppressed} launchd/init-owned suppressed)" if suppressed else ""
    print(
        f"check_orphaned_load: {len(flagged)} orphaned process(es) with PPID "
        f"{ORPHAN_PPID} above {min_cpu_seconds:g} CPU-seconds{note}:"
    )
    for record in flagged:
        hours = record.cpu_seconds / 3600
        print(f"  pid={record.pid:<8} cpu={record.cpu_seconds:>12.1f}s ({hours:.1f}h)")
        print(f"    {record.command}")
    print(
        "\nEach of these was spawned by something that has since exited. Confirm "
        "the owning investigation is over, then terminate only the specific pids "
        "you have verified with `ps -o pid=,ppid=,command= -p <pid>`. Bounding "
        "helpers at spawn time and reaping the recorded pid prevents the leak — see "
        "docs/orphaned-processes.md."
    )
    if suppressed:
        print(
            f"\n{suppressed} further orphan(s) were suppressed as launchd/init-owned "
            "processes parented to PID 1 by design. Re-run with "
            "--include-system to see them."
        )


def main(argv: list[str] | None = None, *, lister: ProcessLister | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_orphaned_load",
        description=(
            "Report compute processes whose parent is PID 1 and whose accumulated "
            "CPU time exceeds a threshold. Read-only: nothing is signalled."
        ),
    )
    parser.add_argument(
        "--min-cpu-seconds",
        type=float,
        default=DEFAULT_MIN_CPU_SECONDS,
        help="CPU-seconds an orphan must exceed to be reported (default: %(default)s)",
    )
    parser.add_argument(
        "--include-system",
        action="store_true",
        help=(
            "also report OS-vendor and endpoint-management executables, which "
            "launchd/init parents to PID 1 by design, plus macOS GUI app bundles "
            "(29 of 33 on the host that motivated this check)"
        ),
    )
    args = parser.parse_args(argv)

    records = (lister or list_processes)()
    flagged = find_orphaned_load(records, args.min_cpu_seconds, include_system=args.include_system)
    everything = find_orphaned_load(records, args.min_cpu_seconds, include_system=True)
    suppressed = len(everything) - len(flagged)
    if not flagged:
        return 0
    _report(flagged, args.min_cpu_seconds, suppressed)
    return 1


if __name__ == "__main__":
    sys.exit(main())
