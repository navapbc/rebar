"""journald usage against its ceiling, published as metrics (story e956-b1c3-45b9-4016).

``/var/log`` was 1.8G of the 28G root working set on 2026-09-02, 1.7G of it the journal, and the
only signal was ``rebar-root-disk-pressure`` — "root disk high", which cannot name the
generator. ``observability.sh`` §2g publishes the journal's size, its percent of the configured
ceiling, and a heartbeat saying whether that ceiling is actually in force.

Two properties carry this file, both inherited from story 9183:

**Both sides span the same bytes.** 9183's patchset 1 differenced a ``du`` of ``overlay2``
against a ledger that also counted the build cache, and the residue was systematically wrong.
The same rule applies to a ratio: ``SystemMaxUse`` governs the journal files under
``/var/log/journal``, so the numerator must be a ``du`` of exactly that tree — not of
``/var/log``, which the ceiling does not bound.

**Silence, never a fabricated 0.** A probe that could not measure publishes NOTHING, and every
alarm is ``treat_missing_data = "breaching"`` (bug 3276 defect 2), so the silence pages. A 0
would read as an empty journal on a box that is filling. The heartbeat is the deliberate
exception: it is published on EVERY tick including the 0 path (bug bff5), so its ABSENCE means
the probe is dead rather than the ceiling being fine.

The tests drive the REAL ``observability.sh`` and the REAL ``journald-cap.sh`` over PATH stubs
and a fake ``/proc``: no systemd, no journald, no AWS, no CI provider.
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
    # `timeout` is absent on stock macOS: drop the duration and exec the wrapped command, the
    # same portability stub tests/unit/test_autodeploy_prune.py already uses.
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

    # `du` records EVERY path it is asked about, so a test can pin that the journal reading is
    # taken over the tree the ceiling governs and not over a wider one.
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


# --------------------------------------------------------------------------------------
# The reading, and the byte set it is taken over
# --------------------------------------------------------------------------------------


def test_the_journal_size_and_its_percent_of_the_ceiling_are_published(tmp_path: Path) -> None:
    env, aws_log, _ = _environment(tmp_path, journal_bytes=GIB + GIB // 2, cap=3 * GIB)
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_bytes") == GIB + GIB // 2
    assert _one(aws_log, "journal_used_percent") == 50


def test_the_percent_is_measured_over_the_tree_the_ceiling_governs(tmp_path: Path) -> None:
    """``SystemMaxUse`` bounds the journal files under ``/var/log/journal`` and nothing else.

    A numerator taken over ``/var/log`` would count rotated syslog, nginx access logs and every
    other consumer against a ceiling that does not bound them, and the ratio would be about no
    quantity at all — the same defect as 9183's mismatched minuend and subtrahend, in ratio
    form.
    """
    env, _, du_log = _environment(tmp_path)
    assert _run(env).returncode == 0
    measured = du_log.read_text().split()
    assert env["JOURNAL_DIR"] in measured
    assert "/var/log" not in measured


def test_a_ceiling_overrun_publishes_its_true_ratio(tmp_path: Path) -> None:
    """The percent is the ONLY reading that can say the ceiling did not hold, so it must be able
    to say 200 (bug ``b380-3dfc-99fc-4a0e``). It used to clamp to 100, which made "exactly at the
    ceiling" and "at twice the ceiling" the same datapoint and turned a breach into what an
    operator reads as a healthy pinned ceiling. ``SystemMaxUse`` is journald's own best-effort
    target, not a hard wall, so the overrun is a real state and not an impossible one."""
    env, aws_log, _ = _environment(tmp_path, journal_bytes=6 * GIB, cap=3 * GIB)
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_used_percent") == 200
    assert _one(aws_log, "journal_bytes") == 6 * GIB


# --------------------------------------------------------------------------------------
# Silence, never a fabricated 0
# --------------------------------------------------------------------------------------


def test_an_unmeasurable_journal_publishes_nothing_rather_than_zero(tmp_path: Path) -> None:
    """A 0 would read as an empty journal on a filling box. Both alarms are
    ``treat_missing_data = "breaching"``, so the silence pages instead."""
    env, aws_log, _ = _environment(tmp_path, journal_bytes=None)
    assert _run(env).returncode == 0
    assert _values(aws_log, "journal_bytes") == []
    assert _values(aws_log, "journal_used_percent") == []


def test_the_size_is_still_published_when_the_ceiling_is_unreadable(tmp_path: Path) -> None:
    """The two readings are independently gated: losing the cap must not also take the
    magnitude off the air, since ``journal_bytes`` is what an operator sizes the problem with."""
    env, aws_log, _ = _environment(tmp_path)
    env["JOURNAL_MAX_USE_BYTES"] = "0"
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_bytes") > 0
    assert _values(aws_log, "journal_used_percent") == []


# --------------------------------------------------------------------------------------
# The heartbeat: is the ceiling we measure against the one journald actually read?
# --------------------------------------------------------------------------------------


def test_the_heartbeat_is_one_when_the_running_journald_postdates_the_dropin(
    tmp_path: Path,
) -> None:
    env, aws_log, _ = _environment(tmp_path, journald_postdates_dropin=True)
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_cap_in_effect") == 1


def test_the_heartbeat_is_zero_when_the_daemon_predates_the_dropin(tmp_path: Path) -> None:
    """The state story 9183 shipped and could not see: the ceiling is on disk, the running
    daemon never read it, and every other reading looks perfectly healthy.

    ``journal_used_percent`` is computed against a cap that is NOT the one in force here, so
    without this heartbeat the whole ratio is quietly about the wrong denominator.
    """
    env, aws_log, _ = _environment(tmp_path, journald_postdates_dropin=False)
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_cap_in_effect") == 0


def test_the_heartbeat_is_published_even_with_no_dropin_installed(tmp_path: Path) -> None:
    """Bug bff5's rule: a value on EVERY tick, including the failing path, so that ABSENCE
    means the probe, the timer or the host is dead — not that the ceiling is fine."""
    env, aws_log, _ = _environment(tmp_path, dropin_installed=False)
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_cap_in_effect") == 0


def test_the_heartbeat_survives_an_unmeasurable_journal(tmp_path: Path) -> None:
    """The heartbeat and the size reading fail independently: a ``du`` that cannot run must not
    also silence the answer to "is the ceiling in force"."""
    env, aws_log, _ = _environment(tmp_path, journal_bytes=None)
    assert _run(env).returncode == 0
    assert _one(aws_log, "journal_cap_in_effect") == 1
    assert _values(aws_log, "journal_bytes") == []
