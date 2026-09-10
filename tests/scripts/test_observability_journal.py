"""journald usage metrics (story e956-b1c3-45b9-4016).

``observability.sh`` §2g measures exactly the journal tree governed by ``SystemMaxUse``,
publishes its size and unclamped percent, and reports whether the running daemon read the
drop-in. Size and cap readings fail independently. Measurement failure stays silent rather
than becoming zero, so breaching missing-data alarms page; the heartbeat still publishes
every tick, including zero, reserving absence for probe failure.

Tests run the real scripts through PATH and fake-``/proc`` stubs.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40

GIB = 1024**3
DEFAULT_CAP = 3 * GIB

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
    "DISK_PRESSURE_PERSIST_OFFSET_FILE",
    "G2P_OFFSET_FILE",
)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _environment(
    tmp_path: Path,
    *,
    journal_bytes: int | None = GIB + GIB // 2,
    cap: int = DEFAULT_CAP,
    dropin_installed: bool = True,
    journald_postdates_dropin: bool = True,
    main_pid: str = "4242",
) -> tuple[dict[str, str], Path, Path]:
    """Returns ``(env, aws_log, du_log)``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"
    du_log = tmp_path / "du.log"
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()

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
    _stub(bin_dir, "journalctl", "exit 0")
    _stub(bin_dir, "docker", "exit 1")
    # Model macOS without `timeout` by executing the wrapped command.
    _stub(bin_dir, "timeout", 'shift\nexec "$@"')
    _stub(
        bin_dir,
        "systemctl",
        f"""
        case "$*" in
          *"--property=MainPID"*) printf '%s\\n' "{main_pid}"; exit 0 ;;
          *is-active*)            exit 0 ;;
        esac
        exit 0
        """,
    )

    # Record each `du` path so tests pin measurement to the governed tree.
    journal_body = (
        "exit 1" if journal_bytes is None else f'printf "{journal_bytes}\\t$1\\n"; exit 0'
    )
    _stub(
        bin_dir,
        "du",
        f"""
        for a in "$@"; do
          case "$a" in -*) ;; *) printf '%s\\n' "$a" >> "$DU_LOG" ;; esac
        done
        case "$*" in
          *journal*) {journal_body} ;;
        esac
        exit 1
        """,
    )

    dropin = tmp_path / "journald.conf.d" / "99-rebar-disk-ceiling.conf"
    if dropin_installed:
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(f"[Journal]\nSystemMaxUse={cap}\n")
        os.utime(dropin, (1_000_000_000, 1_000_000_000))
        started = 1_000_000_060 if journald_postdates_dropin else 999_999_940
        entry = proc_dir / main_pid
        entry.mkdir()
        os.utime(entry, (started, started))

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "DU_LOG": str(du_log),
            "REPL_LOG": str(tmp_path / "replication.log"),
            "JOURNAL_MAX_USE_BYTES": str(cap),
            "JOURNAL_DIR": str(journal_dir),
            "JOURNALD_DROPIN": str(dropin),
            "JOURNALD_PROC_DIR": str(proc_dir),
            **{name: str(offsets / name.lower()) for name in _OFFSET_VARIABLES},
        }
    )
    for name in _OFFSET_VARIABLES:
        (offsets / name.lower()).write_text("0\n")
    (tmp_path / "replication.log").write_text("")
    return env, aws_log, du_log


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT)], env=env, timeout=120, check=False)


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
        values.append(int(float(parts[parts.index("--value") + 1])))
    return values


def _one(log: Path, metric: str) -> int:
    values = _values(log, metric)
    assert len(values) == 1, f"expected exactly one {metric} datapoint, got {values}"
    return values[0]


# Governed-tree reading


def test_the_journal_size_and_its_percent_of_the_ceiling_are_published(tmp_path: Path) -> None:
    env, aws_log, _ = _environment(tmp_path, journal_bytes=GIB + GIB // 2, cap=3 * GIB)
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_bytes") == GIB + GIB // 2
    assert _one(aws_log, "journal_used_percent") == 50


def test_the_percent_is_measured_over_the_tree_the_ceiling_governs(tmp_path: Path) -> None:
    """Measure only ``/var/log/journal``, which ``SystemMaxUse`` governs; wider ``/var/log``
    would invalidate the ratio."""
    env, _, du_log = _environment(tmp_path)
    assert _run(env).returncode == 0
    measured = du_log.read_text().split()
    assert env["JOURNAL_DIR"] in measured
    assert "/var/log" not in measured


def test_a_ceiling_overrun_publishes_its_true_ratio(tmp_path: Path) -> None:
    """Publish the unclamped ratio because ``SystemMaxUse`` is best-effort and real overruns
    must differ from exactly 100% (bug ``b380-3dfc-99fc-4a0e``)."""
    env, aws_log, _ = _environment(tmp_path, journal_bytes=6 * GIB, cap=3 * GIB)
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_used_percent") == 200
    assert _one(aws_log, "journal_bytes") == 6 * GIB


# Measurement silence


def test_an_unmeasurable_journal_publishes_nothing_rather_than_zero(tmp_path: Path) -> None:
    """Publish silence, not false zero, when sizing fails; breaching missing-data alarms page."""
    env, aws_log, _ = _environment(tmp_path, journal_bytes=None)
    assert _run(env).returncode == 0
    assert _values(aws_log, "journal_bytes") == []
    assert _values(aws_log, "journal_used_percent") == []


def test_the_size_is_still_published_when_the_ceiling_is_unreadable(tmp_path: Path) -> None:
    """Size and cap fail independently; an unreadable cap must not suppress the magnitude gauge."""
    env, aws_log, _ = _environment(tmp_path)
    env["JOURNAL_MAX_USE_BYTES"] = "0"
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_bytes") > 0
    assert _values(aws_log, "journal_used_percent") == []


# Cap heartbeat


def test_the_heartbeat_is_one_when_the_running_journald_postdates_the_dropin(
    tmp_path: Path,
) -> None:
    env, aws_log, _ = _environment(tmp_path, journald_postdates_dropin=True)
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_cap_in_effect") == 1


def test_the_heartbeat_is_zero_when_the_daemon_predates_the_dropin(tmp_path: Path) -> None:
    """Report zero when journald predates the drop-in; otherwise the percentage quietly uses a
    cap not in force."""
    env, aws_log, _ = _environment(tmp_path, journald_postdates_dropin=False)
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_cap_in_effect") == 0


def test_the_heartbeat_is_published_even_with_no_dropin_installed(tmp_path: Path) -> None:
    """Publish zero without a drop-in so absence remains reserved for probe, timer, or host
    failure (bug bff5)."""
    env, aws_log, _ = _environment(tmp_path, dropin_installed=False)
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_cap_in_effect") == 0


def test_the_heartbeat_survives_an_unmeasurable_journal(tmp_path: Path) -> None:
    """Heartbeat and size fail independently; unreadable journal size must not suppress cap
    state."""
    env, aws_log, _ = _environment(tmp_path, journal_bytes=None)
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_cap_in_effect") == 1
    assert _values(aws_log, "journal_bytes") == []
