#!/usr/bin/env python3
"""On-demand detector for orphaned high-CPU compute processes [rebar:f3d9-ecd8-df72-4fa1].

Agents legitimately spawn CPU saturation workers to reproduce timing-dependent
failures. The defect this check exists for is that the workers outlive the
investigation: on 2026-08-22 three sessions spawned ``python -c "while True:
pass"`` load generators, none of which were reaped. Thirty-eight of them
reparented to PID 1 and ran for **four days** at roughly 1341% combined CPU on a
six-performance-core host, driving load average to 58 until applications could no
longer launch.

Two signals together identify that shape, and neither alone is sufficient:

* **PPID == 1** — the spawning harness exited without reaping the child, so
  nothing associates the process with the finished investigation. Plenty of
  legitimate daemons are also reparented to PID 1, which is why this is only half
  the test.
* **accumulated CPU time > 3600 seconds** — the leaked batch had burned roughly
  138,000 CPU-seconds (38 processes x ~40% CPU x 4 days). An idle reparented
  daemon rarely passes an hour of *CPU* time in its whole life, so an hour
  separates the two populations by more than an order of magnitude at both ends.
  ``--min-cpu-seconds`` moves the line for hosts that run legitimate long-lived
  compute under PID 1.

On a desktop OS the report is a triage list, not a verdict: system daemons are
launched by ``launchd``/``init`` and legitimately accumulate days of CPU, so they
appear alongside anything leaked. What separates a leak is its command line — an
ad-hoc ``python -c``, a helper from a finished investigation — so read the
commands, not just the count.

**Deliberately an on-demand command, not a gate.** It reports on live host state,
which is not a property of the tree under review, so wiring it into ``make lint``
or CI would make the build depend on whatever else the workstation happens to be
running. It is a plain command with no CI-provider dependency
(``project.portability``)::

    python scripts/check_orphaned_load.py
    python scripts/check_orphaned_load.py --min-cpu-seconds 600

Exit status is 0 when nothing is flagged and 1 when at least one process is. It
is strictly READ-ONLY: it inspects the process table and never signals, kills, or
spawns anything. Reclaiming a flagged batch is an operator decision — see
``AGENTS.md`` for the teardown guidance and the ``pkill`` recipe.

The process-inspection seam (``list_processes``) is injectable via ``main``'s
``lister`` argument precisely so tests can feed synthetic records: a test that
spawned a real unbounded CPU burner to have something to detect would reproduce
the defect this check exists to catch.
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

#: CPU-seconds an orphan must exceed to be reported. See the module docstring for
#: the arithmetic: the incident's workers were ~2 orders of magnitude above this,
#: and a legitimate reparented daemon is well below it.
DEFAULT_MIN_CPU_SECONDS = 3600

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


def find_orphaned_load(
    records: Sequence[ProcessRecord], min_cpu_seconds: float
) -> list[ProcessRecord]:
    """Return the orphaned, CPU-hot subset of ``records``, hottest first.

    Both conditions are required: a reparented process that has burned no CPU is
    an ordinary daemon, and a CPU-hot process with a live parent belongs to
    whoever started it.
    """
    flagged = [
        record
        for record in records
        if record.ppid == ORPHAN_PPID and record.cpu_seconds > min_cpu_seconds
    ]
    return sorted(flagged, key=lambda record: record.cpu_seconds, reverse=True)


def _report(flagged: Sequence[ProcessRecord], min_cpu_seconds: float) -> None:
    print(
        f"check_orphaned_load: {len(flagged)} orphaned process(es) with PPID "
        f"{ORPHAN_PPID} above {min_cpu_seconds:g} CPU-seconds:"
    )
    for record in flagged:
        hours = record.cpu_seconds / 3600
        print(f"  pid={record.pid:<8} cpu={record.cpu_seconds:>12.1f}s ({hours:.1f}h)")
        print(f"    {record.command}")
    print(
        "\nEach of these was spawned by something that has since exited. Confirm "
        "the owning investigation is over, then terminate them by matching their "
        "command line, e.g. `pkill -f 'while True: pass'`. Bounding helpers at "
        "spawn time (`timeout 120 ... &`) and tearing down the process group "
        "(`trap 'kill 0' EXIT INT TERM`) prevents the leak — see AGENTS.md."
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
    args = parser.parse_args(argv)

    records = (lister or list_processes)()
    flagged = find_orphaned_load(records, args.min_cpu_seconds)
    if not flagged:
        return 0
    _report(flagged, args.min_cpu_seconds)
    return 1


if __name__ == "__main__":
    sys.exit(main())
