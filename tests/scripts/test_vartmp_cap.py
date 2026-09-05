"""The ``/var/tmp`` disk ceiling, and the honest gap between the two things enforcing it
(story ``2ba3-bf77-1303-4b2d``).

``/var/tmp`` was 3.6G of the 28G root working set at the 2026-09-02 outage. It is a plain
directory on the root **XFS** filesystem, and that is the whole difficulty: unlike journald's
``SystemMaxUse`` (story e956) there is no writer here that checks a ceiling as it extends a
file, and ``systemd-tmpfiles`` bounds **age**, never **bytes**.

So ``infra/scripts/vartmp-cap.sh`` ships **two** mechanisms of unequal strength, and the tests
below exist mostly to stop the weaker one being mistaken for the stronger:

**The hard ceiling is an XFS project quota, and it is operator-enabled.** XFS reads its quota
mount options at mount time and refuses to enable accounting on a remount, so on the ROOT
filesystem it needs ``rootflags=pquota`` on the kernel command line and a **reboot** — an
operator-scheduled outage, not something a deploy tick may do. The script therefore applies the
quota only when the kernel is already accounting, and reports which regime the box is in.

**The reaper is a MITIGATION with a fill-rate assumption.** It evicts oldest-first on a timer,
so between two runs ``/var/tmp`` is bounded only by the volume. The tests pin its behaviour, its
grace window, and — deliberately — the fact that it never claims to be a ceiling.

The tests drive the REAL script over PATH stubs and a temporary tree: no systemd, no root, no
XFS, no AWS, and no CI provider.
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

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "vartmp-cap.sh"

GIB = 1024**3
DEFAULT_CAP = 4 * GIB


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _environment(
    tmp_path: Path,
    *,
    cap: int | None = None,
    quota_accounting: bool = False,
    quota_enforced: bool = False,
    timer_active: bool = True,
    have_systemctl: bool = True,
    have_xfs_quota: bool = True,
) -> tuple[dict[str, str], Path]:
    """A worktree with a stubbed ``systemctl``/``xfs_quota`` and a writable ``/var/tmp``.

    Returns ``(env, var_tmp)``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    var_tmp = tmp_path / "var-tmp"
    var_tmp.mkdir()
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()

    if have_systemctl:
        _stub(
            bin_dir,
            "systemctl",
            f"""
            printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
            case "$*" in
              *is-active*)  exit {0 if timer_active else 3} ;;
              *is-enabled*) exit {0 if timer_active else 1} ;;
            esac
            exit 0
            """,
        )
    if have_xfs_quota:
        # `state -p` is how the script learns whether the KERNEL is accounting project
        # quota — the precondition it cannot create for itself.
        _stub(
            bin_dir,
            "xfs_quota",
            f"""
            printf '%s\\n' "$*" >> "$XFS_QUOTA_LOG"
            case "$*" in
              *"state -p"*)
                printf 'Project quota state on / (/dev/nvme0n1p1)\\n'
                printf '  Accounting: {"ON" if quota_accounting else "OFF"}\\n'
                printf '  Enforcement: {"ON" if quota_enforced else "OFF"}\\n'
                exit 0 ;;
            esac
            exit 0
            """,
        )

    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
            "XFS_QUOTA_LOG": str(tmp_path / "xfs_quota.log"),
            "VAR_TMP_DIR": str(var_tmp),
            "VAR_TMP_TMPFILES_CONF": str(tmp_path / "tmpfiles.d" / "99-rebar-var-tmp.conf"),
            "VAR_TMP_UNIT_DIR": str(unit_dir),
            "VAR_TMP_INSTALLED_PATH": str(tmp_path / "installed" / "rebar-vartmp-cap.sh"),
        }
    )
    if cap is not None:
        env["VAR_TMP_MAX_BYTES"] = str(cap)
    return env, var_tmp


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _write(path: Path, size: int, *, age_seconds: float) -> Path:
    """A file of ``size`` bytes whose mtime is ``age_seconds`` in the past."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    when = os.stat(path).st_mtime - age_seconds
    os.utime(path, (when, when))
    for parent in (path.parent,):
        if parent != path.parent.parent:
            os.utime(parent, (when, when))
    return path


def _env_value(out: str, key: str) -> str:
    for line in out.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{key} not printed by --print-env: {out!r}")


# --------------------------------------------------------------------------------------
# The ceiling, and the two mechanisms behind it
# --------------------------------------------------------------------------------------


def _shipped_defaults() -> dict[str, str]:
    """``--print-env`` with NO overrides — the values the box actually deploys with."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "--print-env"],
        env=subprocess_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in result.stdout.splitlines()
        if "=" in line
    }


def test_print_env_publishes_the_ceiling_and_the_tree_it_governs() -> None:
    """``observability.sh`` consumes this with ``eval``, so the published percent-of-cap and
    the ceiling the box is configured to enforce are the same number by construction."""
    defaults = _shipped_defaults()
    assert int(defaults["VAR_TMP_MAX_BYTES"]) == DEFAULT_CAP
    assert defaults["VAR_TMP_DIR"] == "/var/tmp"


def test_the_shipped_ceiling_alarms_below_the_size_var_tmp_reached_at_the_outage() -> None:
    """``/var/tmp`` held 3.6G on 2026-09-02 and nothing named it. The 85% alarm threshold has
    to trip BELOW that, or this configuration would have watched the same fill in silence.

    Read from the SCRIPT, not from a constant in this file: the whole claim is about the value
    the host deploys with, so a test that re-states the number here would pass while the box
    shipped any other one.
    """
    # `du -sh` reported 3.6G, and GNU `du -h` scales in powers of 1024.
    observed_at_outage = 3.6 * GIB
    shipped = int(_shipped_defaults()["VAR_TMP_MAX_BYTES"])
    assert shipped * 0.85 < observed_at_outage, (
        f"the shipped {shipped}B budget alarms at {shipped * 0.85:.0f}B, which is ABOVE the "
        f"{observed_at_outage:.0f}B /var/tmp reached at the outage — this configuration would "
        "have watched the same fill without paging"
    )
    # ...and above it there must still be headroom, or steady state alarms forever.
    assert shipped > observed_at_outage


def test_a_non_integer_ceiling_is_refused_rather_than_silently_ignored(tmp_path: Path) -> None:
    """A ceiling nothing can parse is a ceiling that does not exist. Refuse loudly."""
    env, _ = _environment(tmp_path)
    env["VAR_TMP_MAX_BYTES"] = "4GiB"
    result = _run(env, "--print-env")
    assert result.returncode != 0
    assert "integer byte count" in result.stderr


def test_the_tmpfiles_dropin_bounds_age_only_and_says_so(tmp_path: Path) -> None:
    """``systemd-tmpfiles`` has no size verb for an ordinary directory. The rendered file must
    not read as though it were a byte ceiling."""
    env, _ = _environment(tmp_path)
    result = _run(env, "--print-conf")
    assert result.returncode == 0, result.stderr
    conf = result.stdout
    assert env["VAR_TMP_DIR"] in conf
    # An age-cleanup entry for the tree, and nothing pretending to be a size.
    assert conf.lstrip().startswith("#")
    verbs = [line.split()[0] for line in conf.splitlines() if line and not line.startswith("#")]
    assert verbs and set(verbs) <= {"q", "d"}


# --------------------------------------------------------------------------------------
# The hard ceiling: an XFS project quota the operator must enable
# --------------------------------------------------------------------------------------


def test_the_quota_heartbeat_is_zero_when_the_kernel_is_not_accounting(tmp_path: Path) -> None:
    """XFS reads quota mount options at MOUNT time and refuses to enable accounting on a
    remount, so on the root filesystem this needs ``rootflags=pquota`` and a reboot. Until
    that happens there is no hard ceiling, and the box must say so rather than imply one."""
    env, _ = _environment(tmp_path, quota_accounting=False, quota_enforced=False)
    result = _run(env, "--check-quota")
    assert result.returncode == 0
    assert result.stdout.strip() == "0"


def test_the_quota_heartbeat_is_zero_when_accounting_is_on_but_enforcement_is_off(
    tmp_path: Path,
) -> None:
    """Accounting without enforcement MEASURES the tree and bounds nothing. Reporting that as
    a ceiling is the "bounded on paper" failure this epic exists to remove."""
    env, _ = _environment(tmp_path, quota_accounting=True, quota_enforced=False)
    result = _run(env, "--check-quota")
    assert result.returncode == 0
    assert result.stdout.strip() == "0"


def test_the_quota_heartbeat_is_one_only_when_enforcement_is_on(tmp_path: Path) -> None:
    env, _ = _environment(tmp_path, quota_accounting=True, quota_enforced=True)
    result = _run(env, "--check-quota")
    assert result.returncode == 0
    assert result.stdout.strip() == "1"


def test_the_quota_check_fails_closed_without_xfs_quota(tmp_path: Path) -> None:
    """An unreadable state is NOT a ceiling. The cost of over-claiming here is an unbounded
    ``/var/tmp`` that everybody believes is capped."""
    env, _ = _environment(tmp_path, have_xfs_quota=False)
    result = _run(env, "--check-quota")
    assert result.returncode == 0
    assert result.stdout.strip() == "0"


def test_install_names_the_exact_operator_steps_when_the_quota_cannot_be_applied(
    tmp_path: Path,
) -> None:
    """A warning that says "quota unavailable" and stops is a dead end. The one action that
    fixes it needs a reboot, so the message has to carry it."""
    env, _ = _environment(tmp_path, quota_accounting=False)
    result = _run(env, "--install")
    assert result.returncode == 0, result.stderr
    assert "rootflags=pquota" in result.stderr
    assert "reboot" in result.stderr.lower()


# --------------------------------------------------------------------------------------
# The reaper: a mitigation, and the tests that keep it honest about being one
# --------------------------------------------------------------------------------------


def test_the_reaper_does_nothing_while_the_tree_is_under_its_ceiling(tmp_path: Path) -> None:
    env, var_tmp = _environment(tmp_path, cap=4096)
    keep = _write(var_tmp / "small" / "f", 512, age_seconds=86400)
    assert _run(env, "--reap").returncode == 0
    assert keep.exists()


def test_the_reaper_evicts_oldest_first_until_the_tree_is_under_the_ceiling(
    tmp_path: Path,
) -> None:
    """Oldest-first is the only defensible order: the newest bytes are the ones a running job
    is most likely to still need."""
    env, var_tmp = _environment(tmp_path, cap=4096)
    oldest = _write(var_tmp / "a" / "f", 3000, age_seconds=100_000)
    middle = _write(var_tmp / "b" / "f", 3000, age_seconds=50_000)
    newest = _write(var_tmp / "c" / "f", 3000, age_seconds=10_000)

    assert _run(env, "--reap").returncode == 0

    assert not oldest.parent.exists(), "the oldest entry should have gone first"
    assert newest.parent.exists(), "the newest entry must survive"
    # 9000 bytes against a 4096 ceiling: two of the three have to go.
    assert not middle.parent.exists()


def test_the_reaper_never_evicts_inside_its_grace_window(tmp_path: Path) -> None:
    """The snapshot janitor's rule, for the same reason: a directory written seconds ago is
    almost certainly still being written INTO, and deleting it converts a capacity problem
    into a corrupted job."""
    env, var_tmp = _environment(tmp_path, cap=1024)
    env["VAR_TMP_MIN_AGE_SECONDS"] = "3600"
    fresh_a = _write(var_tmp / "a" / "f", 4000, age_seconds=5)
    fresh_b = _write(var_tmp / "b" / "f", 4000, age_seconds=5)

    result = _run(env, "--reap")

    assert result.returncode == 0
    assert fresh_a.parent.exists() and fresh_b.parent.exists()
    # And it must SAY it could not get under the ceiling, rather than exiting quietly as
    # though it had.
    assert "could not" in result.stderr.lower() or "still" in result.stderr.lower()


def test_evidence_directories_are_reaped_individually_not_as_one_tree(tmp_path: Path) -> None:
    """``/var/tmp/rebar-evidence/<ticket>-<stamp>/`` is the runbook's sanctioned scratch
    location (task 3e92). Treating ``rebar-evidence`` as ONE candidate would either evict
    every investigation's evidence at once or, once one child is fresh, protect all of it
    forever — the epic flagged this collision explicitly."""
    env, var_tmp = _environment(tmp_path, cap=4096)
    stale = _write(var_tmp / "rebar-evidence" / "old-ticket" / "dump", 3000, age_seconds=100_000)
    active = _write(var_tmp / "rebar-evidence" / "new-ticket" / "dump", 3000, age_seconds=50_000)

    assert _run(env, "--reap").returncode == 0

    assert not stale.parent.exists(), "the stale investigation's evidence should be reclaimed"
    assert active.parent.exists(), "a newer investigation's evidence must survive"


def test_the_reaper_leaves_lost_and_found_alone(tmp_path: Path) -> None:
    """It is the filesystem's, not ours, and removing it breaks ``xfs_repair``."""
    env, var_tmp = _environment(tmp_path, cap=16)
    lost = var_tmp / "lost+found"
    lost.mkdir()
    _write(lost / "f", 4000, age_seconds=100_000)
    assert _run(env, "--reap").returncode == 0
    assert lost.exists()


def test_the_reaper_reports_what_it_removed(tmp_path: Path) -> None:
    """A silent reclaim is indistinguishable from a job deleting its own output. Whatever this
    removes must be attributable afterwards, from the journal alone."""
    env, var_tmp = _environment(tmp_path, cap=1024)
    _write(var_tmp / "doomed" / "f", 8000, age_seconds=100_000)
    result = _run(env, "--reap")
    assert result.returncode == 0
    assert "doomed" in result.stderr


# --------------------------------------------------------------------------------------
# "The cleanup is running" is itself a thing that can stop being true
# --------------------------------------------------------------------------------------


def test_the_cleanup_heartbeat_is_one_when_the_config_and_the_timer_are_both_present(
    tmp_path: Path,
) -> None:
    env, _ = _environment(tmp_path, timer_active=True)
    assert _run(env, "--install").returncode == 0
    result = _run(env, "--check-active")
    assert result.returncode == 0
    assert result.stdout.strip() == "1"


def test_the_cleanup_heartbeat_is_zero_when_the_timer_is_not_running(tmp_path: Path) -> None:
    """An installed drop-in with a dead timer is age cleanup that never happens and a reaper
    that never reaps — the state most likely to be mistaken for a working ceiling."""
    env, _ = _environment(tmp_path, timer_active=True)
    assert _run(env, "--install").returncode == 0
    # Everything stays installed; only the timer stops running.
    _stub(tmp_path / "bin", "systemctl", 'case "$*" in *is-active*) exit 3 ;; esac\nexit 0\n')
    result = _run(env, "--check-active")
    assert result.returncode == 0
    assert result.stdout.strip() == "0"


def test_the_cleanup_heartbeat_is_zero_before_anything_is_installed(tmp_path: Path) -> None:
    env, _ = _environment(tmp_path)
    result = _run(env, "--check-active")
    assert result.returncode == 0
    assert result.stdout.strip() == "0"


# --------------------------------------------------------------------------------------
# Installing it
# --------------------------------------------------------------------------------------


def test_install_writes_the_tmpfiles_dropin_and_the_reaper_units(tmp_path: Path) -> None:
    env, _ = _environment(tmp_path)
    assert _run(env, "--install").returncode == 0, "install should succeed"
    assert Path(env["VAR_TMP_TMPFILES_CONF"]).is_file()
    units = Path(env["VAR_TMP_UNIT_DIR"])
    assert (units / "rebar-var-tmp-reaper.service").is_file()
    assert (units / "rebar-var-tmp-reaper.timer").is_file()


def test_the_reaper_service_is_bounded_below_its_own_timer_period(tmp_path: Path) -> None:
    """The ``install-observability.sh`` lesson (bug 1205): a ``Type=oneshot`` with no
    ``TimeoutStartSec`` gets ``TimeoutStartUSec=INFINITY``, and because ``OnUnitActiveSec`` is
    measured from the last COMPLETED activation, one run that never finishes does not delay
    the timer — it DELETES the next elapse. A reaper that latches off is a ceiling that
    silently stops existing."""
    env, _ = _environment(tmp_path)
    assert _run(env, "--install").returncode == 0
    units = Path(env["VAR_TMP_UNIT_DIR"])
    service = (units / "rebar-var-tmp-reaper.service").read_text()
    timer = (units / "rebar-var-tmp-reaper.timer").read_text()

    start_timeout = int(
        next(line for line in service.splitlines() if line.startswith("TimeoutStartSec=")).split(
            "="
        )[1]
    )
    period = next(line for line in timer.splitlines() if line.startswith("OnUnitActiveSec="))
    assert period.endswith("min")
    period_seconds = int(period.split("=")[1].removesuffix("min")) * 60
    assert 0 < start_timeout < period_seconds


def test_install_is_idempotent(tmp_path: Path) -> None:
    """``compose-up.sh`` runs on every deploy tick."""
    env, _ = _environment(tmp_path)
    first = _run(env, "--install")
    conf = Path(env["VAR_TMP_TMPFILES_CONF"]).read_text()
    second = _run(env, "--install")
    assert first.returncode == 0 and second.returncode == 0
    assert Path(env["VAR_TMP_TMPFILES_CONF"]).read_text() == conf


def test_an_unknown_argument_is_refused(tmp_path: Path) -> None:
    env, _ = _environment(tmp_path)
    result = _run(env, "--vacuum-everything")
    assert result.returncode != 0
    assert "unknown argument" in result.stderr
