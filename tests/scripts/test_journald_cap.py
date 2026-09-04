"""The journald disk ceiling and its honestly-observed activation (story e956-b1c3-45b9-4016).

``/var/log`` was 1.8G of the 28G root working set at the 2026-09-02 outage, and 1.7G of that
was the journal — every compose service logs to the host journal, so journald is a genuine
root-volume generator that ``rebar-root-disk-pressure`` can only report as "root disk high".

``infra/scripts/journald-cap.sh`` installs an explicit ``SystemMaxUse=`` ceiling. Two properties
carry this file, and both are lessons paid for by story 9183:

**Activation is OBSERVED, never inferred from an exit status.** journald reads its config at
STARTUP and ``systemd-journald.service`` implements no ``ExecReload``, so no command's zero exit
proves the ceiling is in force. 9183 shipped a ``systemctl reload docker`` whose zero exit was
the only evidence behind an "in effect" claim that was not true; the tests below pin that no
``reload`` is used at all, and that a SUCCESSFUL ``systemctl restart`` whose result cannot be
confirmed is still reported as NOT in effect.

**Everything fails closed.** An unreadable PID, an unreadable mtime, an absent drop-in, or a
daemon that predates the file all report NOT in effect. A ceiling that reports itself installed
while not in force is worse than no ceiling, because the loud half manufactures confidence.

The tests drive the REAL script over PATH stubs and a fake ``/proc``: no systemd, no journald,
no root, and no CI provider.
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

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "journald-cap.sh"

GIB = 1024**3
DEFAULT_CAP = 3 * GIB


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _environment(
    tmp_path: Path,
    *,
    main_pid: str = "4242",
    active: bool = True,
    restart_rc: int = 0,
    have_systemctl: bool = True,
    fd_store_max: str = "4224",
    cap: int | None = None,
) -> tuple[dict[str, str], Path, Path]:
    """A worktree with a stubbed ``systemctl``, a fake ``/proc`` and a writable drop-in path.

    Returns ``(env, dropin, systemctl_log)``. The log records every ``systemctl`` invocation, so
    a test can assert what the script did — and, more to the point, what it did NOT do.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl_log = tmp_path / "systemctl.log"
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()

    if have_systemctl:
        _stub(
            bin_dir,
            "systemctl",
            f"""
            printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
            case "$*" in
              *"--property=MainPID"*)              printf '%s\\n' "{main_pid}"; exit 0 ;;
              *"--property=FileDescriptorStoreMax"*) printf '%s\\n' "{fd_store_max}"; exit 0 ;;
              *is-active*)                         exit {0 if active else 3} ;;
              restart*)                            exit {restart_rc} ;;
            esac
            exit 0
            """,
        )

    dropin = tmp_path / "journald.conf.d" / "99-rebar-disk-ceiling.conf"
    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "SYSTEMCTL_LOG": str(systemctl_log),
            "JOURNALD_DROPIN": str(dropin),
            "JOURNALD_PROC_DIR": str(proc_dir),
            "JOURNAL_DIR": str(tmp_path / "journal"),
        }
    )
    if cap is not None:
        env["JOURNAL_MAX_USE_BYTES"] = str(cap)
    return env, dropin, systemctl_log


def _proc_entry(env: dict[str, str], pid: str, mtime: float) -> Path:
    """Create ``<proc>/<pid>`` with a controlled mtime — the daemon's start time."""
    entry = Path(env["JOURNALD_PROC_DIR"]) / pid
    entry.mkdir(parents=True, exist_ok=True)
    os.utime(entry, (mtime, mtime))
    return entry


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# --------------------------------------------------------------------------------------
# Rendering: side-effect-free, explicit, and env-settable
# --------------------------------------------------------------------------------------


def test_print_conf_sets_an_explicit_system_max_use(tmp_path: Path) -> None:
    """The whole point of the story: the ceiling is STATED, not left to journald's default.

    Unset, ``SystemMaxUse=`` is 10% of the filesystem capped at 4G — a number that exists only
    inside journald and that no operator can read off the box during an incident.
    """
    env, dropin, _ = _environment(tmp_path)
    result = _run(env, "--print-conf")
    assert result.returncode == 0, result.stderr
    assert "[Journal]" in result.stdout
    assert f"SystemMaxUse={DEFAULT_CAP}" in result.stdout
    # Side-effect-free, the --print-json / --print-volumes precedent.
    assert not dropin.exists()


def test_the_ceiling_is_env_settable_rather_than_a_frozen_constant(tmp_path: Path) -> None:
    """ADR 0112 decision 6: a measured default is a starting point, never a constant."""
    env, _, _ = _environment(tmp_path, cap=7 * GIB)
    result = _run(env, "--print-conf")
    assert result.returncode == 0, result.stderr
    assert f"SystemMaxUse={7 * GIB}" in result.stdout


def test_print_env_is_the_single_source_of_truth_observability_reads(tmp_path: Path) -> None:
    """``observability.sh`` evals this, so the published percent-of-cap and the installed cap
    are the same number by construction and cannot drift apart."""
    env, dropin, _ = _environment(tmp_path)
    result = _run(env, "--print-env")
    assert result.returncode == 0, result.stderr
    assert f"JOURNAL_MAX_USE_BYTES={DEFAULT_CAP}" in result.stdout
    assert f"JOURNAL_DIR={env['JOURNAL_DIR']}" in result.stdout
    assert f"JOURNALD_DROPIN={dropin}" in result.stdout


def test_a_non_integer_ceiling_is_refused(tmp_path: Path) -> None:
    """A typo'd cap must fail loudly rather than render a line journald silently ignores."""
    env, _, _ = _environment(tmp_path)
    env["JOURNAL_MAX_USE_BYTES"] = "3GiB"
    result = _run(env, "--print-conf")
    assert result.returncode != 0
    assert "integer" in result.stderr


# --------------------------------------------------------------------------------------
# Install: idempotent, and it restarts ONLY when the file actually changed
# --------------------------------------------------------------------------------------


def test_install_writes_the_dropin_and_restarts_journald(tmp_path: Path) -> None:
    env, dropin, systemctl_log = _environment(tmp_path)
    result = _run(env, "--install")
    assert result.returncode == 0, result.stderr
    assert f"SystemMaxUse={DEFAULT_CAP}" in dropin.read_text()
    assert "restart systemd-journald" in systemctl_log.read_text()


def test_a_second_install_writes_nothing_and_does_not_restart(tmp_path: Path) -> None:
    """Re-running the boot orchestrator must not bounce the logger to install bytes already
    there — compose-up.sh runs on every deploy tick."""
    env, dropin, systemctl_log = _environment(tmp_path)
    assert _run(env, "--install").returncode == 0
    before = dropin.stat().st_mtime_ns
    systemctl_log.write_text("")

    result = _run(env, "--install")
    assert result.returncode == 0, result.stderr
    assert dropin.stat().st_mtime_ns == before
    assert "restart" not in systemctl_log.read_text()


def test_a_reload_is_never_used_as_evidence_that_the_ceiling_took(tmp_path: Path) -> None:
    """``systemd-journald.service`` implements no ``ExecReload``, so a reload proves nothing.

    Story 9183 shipped ``systemctl reload docker`` whose zero exit was the only evidence behind
    an "in effect" claim that was false. Absence of execution reported as success is the defect
    class; the fix is to never issue the command whose result would have to be trusted.
    """
    env, _, systemctl_log = _environment(tmp_path)
    assert _run(env, "--install").returncode == 0
    assert "reload" not in systemctl_log.read_text()


def test_a_successful_restart_is_not_itself_reported_as_activation(tmp_path: Path) -> None:
    """THE crux. ``systemctl restart`` exits 0 here, and the observation still says NOT in
    effect because the fake ``/proc`` entry predates the drop-in.

    If the script ever regressed to reading the restart's exit status as proof, this test would
    report a ceiling in force that is not — the exact failure 9183's reviewer caught.
    """
    env, dropin, systemctl_log = _environment(tmp_path, restart_rc=0)
    _proc_entry(env, "4242", mtime=1_000_000)  # long before the write

    result = _run(env, "--install")
    assert result.returncode == 0, result.stderr
    assert "restart systemd-journald" in systemctl_log.read_text()
    assert "NOT in effect" in result.stderr
    assert dropin.exists()


def test_a_failed_restart_reports_not_in_effect(tmp_path: Path) -> None:
    env, _, _ = _environment(tmp_path, restart_rc=1)
    _proc_entry(env, "4242", mtime=1_000_000)
    result = _run(env, "--install")
    assert result.returncode == 0, result.stderr
    assert "NOT in effect" in result.stderr


# --------------------------------------------------------------------------------------
# Activation: OBSERVED, and failing closed in every undeterminable case
# --------------------------------------------------------------------------------------


def test_the_ceiling_is_in_effect_when_journald_postdates_the_dropin(tmp_path: Path) -> None:
    """The only state that may be reported as in force: a daemon that started after the write,
    so it necessarily read this file."""
    env, dropin, _ = _environment(tmp_path)
    assert _run(env, "--install").returncode == 0
    _proc_entry(env, "4242", mtime=dropin.stat().st_mtime + 60)

    assert _run(env, "--check-active").stdout.strip() == "1"
    assert "IS in effect" in _run(env, "--install").stderr


def test_a_daemon_predating_the_dropin_is_not_in_effect(tmp_path: Path) -> None:
    env, dropin, _ = _environment(tmp_path)
    assert _run(env, "--install").returncode == 0
    _proc_entry(env, "4242", mtime=dropin.stat().st_mtime - 3600)

    assert _run(env, "--check-active").stdout.strip() == "0"


def test_an_unreadable_pid_fails_closed(tmp_path: Path) -> None:
    """``MainPID`` of 0 is what systemd reports for a unit it is not running."""
    env, _, _ = _environment(tmp_path, main_pid="0")
    assert _run(env, "--install").returncode == 0
    assert _run(env, "--check-active").stdout.strip() == "0"


def test_an_unreadable_start_time_fails_closed(tmp_path: Path) -> None:
    """The PID is reported but ``/proc/<pid>`` does not exist — a race with a restart, or a
    kernel that does not expose it. Undeterminable is NOT in effect."""
    env, _, _ = _environment(tmp_path, main_pid="9999")
    assert _run(env, "--install").returncode == 0
    # No _proc_entry for 9999.
    assert _run(env, "--check-active").stdout.strip() == "0"


def test_an_absent_dropin_is_not_in_effect(tmp_path: Path) -> None:
    """Nothing installed is the strongest possible "not in force", and observability publishes
    it as a 0 heartbeat rather than silence."""
    env, _, _ = _environment(tmp_path)
    _proc_entry(env, "4242", mtime=2_000_000_000)
    assert _run(env, "--check-active").stdout.strip() == "0"


def test_a_stopped_journald_is_reported_as_reading_the_ceiling_on_next_start(
    tmp_path: Path,
) -> None:
    """First boot: compose-up.sh may install before the logger is up, and the next start reads
    the file for free. That is neither a warning nor an in-force claim."""
    env, _, _ = _environment(tmp_path, active=False)
    result = _run(env, "--install")
    assert result.returncode == 0, result.stderr
    assert "next starts" in result.stderr
    assert "IS in effect" not in result.stderr


def test_the_restart_is_refused_when_journald_has_no_file_descriptor_store(
    tmp_path: Path,
) -> None:
    """The riskiest assumption behind restarting the logger, CHECKED rather than believed.

    Restarting journald is safe only because PID 1 holds its per-service stdout stream fds in
    the unit's file-descriptor store (``FileDescriptorStoreMax=4224`` in the shipped unit, whose
    upstream comment is "Ensure services using StandardOutput=journal do not break when journald
    is stopped") and hands them back on the way up. Without that store the restart would sever
    the log streams of every already-running service — which would quietly zero the
    marker-count metrics ``observability.sh`` derives from those journals: healthy-looking
    readings from a box that stopped reporting.

    So the store is probed and the restart REFUSED when it is absent. A dormant ceiling is a
    capacity problem the alarms announce; a severed log stream is a blind spot that looks like
    health.
    """
    env, dropin, systemctl_log = _environment(tmp_path, fd_store_max="0")
    result = _run(env, "--install")
    assert result.returncode == 0, result.stderr
    assert dropin.exists()
    assert "restart" not in systemctl_log.read_text()
    assert "no file-descriptor store" in result.stderr
    assert "NOT restarting" in result.stderr


# --------------------------------------------------------------------------------------
# compose-up.sh calls it, and a failure there does not stop the stack booting
# --------------------------------------------------------------------------------------
# These RUN the boot orchestrator over stubs rather than reading its source, following
# tests/scripts/test_docker_storage_cap.py: asserting a verbatim line passes for the wrong
# reason on a rename and fails for the wrong reason on a behaviour-preserving edit.

COMPOSE_UP = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "compose-up.sh"


def _boot_sandbox(tmp_path: Path, *, install_exit: int) -> tuple[Path, Path, Path]:
    """A copy of ``compose-up.sh`` beside stub sibling scripts, plus PATH stubs and a call log.

    ``compose-up.sh`` resolves its siblings through ``BASH_SOURCE``, which is what makes the
    installer substitutable. The run is expected to die somewhere further down the boot path;
    the assertions are about what had already happened by then.
    """
    scripts = tmp_path / "repo" / "infra" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "compose-up.sh").write_text(COMPOSE_UP.read_text())
    calls = tmp_path / "boot-calls.log"
    calls.write_text("")
    _stub(scripts, "journald-cap.sh", f'echo "journald-cap $*" >> "{calls}"\nexit {install_exit}\n')
    _stub(scripts, "docker-storage-cap.sh", f'echo "docker-storage-cap $*" >> "{calls}"\nexit 0\n')
    bindir = tmp_path / "bootbin"
    bindir.mkdir()
    for name in ("systemctl", "dnf", "curl", "docker", "aws", "logger"):
        _stub(bindir, name, f'echo "{name} $*" >> "{calls}"\nexit 0\n')
    return scripts / "compose-up.sh", bindir, calls


def _boot(script: Path, bindir: Path) -> subprocess.CompletedProcess[str]:
    env = subprocess_env()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def test_compose_up_installs_the_journal_ceiling(tmp_path: Path) -> None:
    """Without this call the drop-in only ever exists in the repository."""
    script, bindir, calls = _boot_sandbox(tmp_path, install_exit=0)
    _boot(script, bindir)
    assert "journald-cap --install" in calls.read_text()


def test_a_failed_ceiling_install_does_not_stop_the_stack_booting(tmp_path: Path) -> None:
    """An unbounded journal is a capacity problem the alarms announce, not a boot failure.

    It is also not invisible: ``rebar-journal-usage-high`` and
    ``rebar-journal-cap-not-in-effect`` watch this generator directly, which is what makes the
    non-fatal branch safe rather than merely convenient. The observable claim is that the next
    boot step still ran, under a script that is ``set -e`` throughout.
    """
    script, bindir, calls = _boot_sandbox(tmp_path, install_exit=1)
    result = _boot(script, bindir)
    log = calls.read_text()
    assert "journald-cap --install" in log
    assert "systemctl enable --now docker" in log, (
        f"a failed ceiling install aborted the boot instead of warning:\n{log}"
    )
    assert "WARN" in result.stderr, result.stderr
