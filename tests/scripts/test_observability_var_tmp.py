"""``/var/tmp`` usage against its budget, published as metrics (story ``2ba3-bf77-1303-4b2d``).

``/var/tmp`` was 3.6G of the 28G root working set on 2026-09-02, and the only signal was
``rebar-root-disk-pressure`` — "root disk high", which cannot name the generator.
``observability.sh`` §2h publishes the tree's size, its percent of the configured budget, and
**two** heartbeats — because unlike journald (§2g) this generator is held by two mechanisms of
very different strength and an operator needs to know which one the box actually has.

Three properties carry this file:

**Silence, never a fabricated 0.** A probe that could not measure publishes NOTHING, and
``rebar-var-tmp-usage-high`` is ``treat_missing_data = "breaching"`` (bug 3276 defect 2) so the
silence pages. A 0 would read as an empty ``/var/tmp`` on a box that is filling.

**The heartbeats publish on EVERY tick, including their 0 path** (bug bff5), so their ABSENCE
means the probe, the timer or the host is dead rather than the ceiling being fine.

**Every reading is BOUNDED.** Twelve unbounded journal rescans in this same probe took Gerrit
off the air for 41 minutes on 2026-09-04 (bug 1205). ``/var/tmp`` is by definition a tree nobody
planned the size of, so its ``du`` runs through the script's own ``bounded`` wrapper and a
timeout is reported as silence.

The tests drive the REAL ``observability.sh`` and the REAL ``vartmp-cap.sh`` over PATH stubs:
no systemd, no XFS, no AWS, no CI provider.
"""

from __future__ import annotations

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
DEFAULT_CAP = 4 * GIB

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
    var_tmp_bytes: int | None = GIB,
    cap: int = DEFAULT_CAP,
    cleanup_active: bool = True,
    quota_enforced: bool = False,
) -> tuple[dict[str, str], Path, Path]:
    """Returns ``(env, aws_log, du_log)``."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"
    du_log = tmp_path / "du.log"
    var_tmp = tmp_path / "var-tmp"
    var_tmp.mkdir()

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
    _stub(bin_dir, "timeout", 'shift\nexec "$@"')
    _stub(
        bin_dir,
        "systemctl",
        f'case "$*" in *is-active*) exit {0 if cleanup_active else 3} ;; esac\nexit 0',
    )
    _stub(
        bin_dir,
        "xfs_quota",
        f"""
        case "$*" in
          *"state -p"*)
            printf '  Accounting: {"ON" if quota_enforced else "OFF"}\\n'
            printf '  Enforcement: {"ON" if quota_enforced else "OFF"}\\n'
            exit 0 ;;
        esac
        exit 0
        """,
    )

    # `du` records EVERY path it is asked about, so a test can pin which tree the reading is
    # taken over.
    var_tmp_body = (
        "exit 1" if var_tmp_bytes is None else f'printf "{var_tmp_bytes}\\t$1\\n"; exit 0'
    )
    _stub(
        bin_dir,
        "du",
        f"""
        for a in "$@"; do
          case "$a" in -*) ;; *) printf '%s\\n' "$a" >> "$DU_LOG" ;; esac
        done
        case "$*" in
          *var-tmp*) {var_tmp_body} ;;
        esac
        exit 1
        """,
    )

    tmpfiles_conf = tmp_path / "tmpfiles.d" / "99-rebar-var-tmp.conf"
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    if cleanup_active:
        # `--check-active` compares the installed drop-in against what the cap script renders,
        # so the fixture renders it rather than hand-writing a copy that could drift.
        rendered = subprocess.run(
            [
                "bash",
                str(Path(SCRIPT).parent / "vartmp-cap.sh"),
                "--print-conf",
            ],
            env=subprocess_env({"VAR_TMP_DIR": str(var_tmp), "VAR_TMP_MAX_BYTES": str(cap)}),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        tmpfiles_conf.parent.mkdir(parents=True, exist_ok=True)
        tmpfiles_conf.write_text(rendered)
        (unit_dir / "rebar-var-tmp-reaper.timer").write_text("[Timer]\n")

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "DU_LOG": str(du_log),
            "REPL_LOG": str(tmp_path / "replication.log"),
            "VAR_TMP_DIR": str(var_tmp),
            "VAR_TMP_MAX_BYTES": str(cap),
            "VAR_TMP_TMPFILES_CONF": str(tmpfiles_conf),
            "VAR_TMP_UNIT_DIR": str(unit_dir),
            **{name: str(offsets / name.lower()) for name in _OFFSET_VARIABLES},
        }
    )
    for name in _OFFSET_VARIABLES:
        (offsets / name.lower()).write_text("0\n")
    (tmp_path / "replication.log").write_text("")
    return env, aws_log, du_log


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT)], env=env, timeout=180, check=False)


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
# The reading
# --------------------------------------------------------------------------------------


def test_the_var_tmp_size_and_its_percent_of_the_budget_are_published(tmp_path: Path) -> None:
    env, aws_log, _ = _environment(tmp_path, var_tmp_bytes=GIB, cap=4 * GIB)
    assert _run(env).returncode == 0
    assert _one(aws_log, "var_tmp_bytes") == GIB
    assert _one(aws_log, "var_tmp_used_percent") == 25


def test_the_percent_is_measured_over_the_tree_the_budget_governs(tmp_path: Path) -> None:
    """The budget is about ``/var/tmp``. A numerator taken over ``/var`` would count the Gerrit
    site tree and the whole Docker root against a ceiling that does not bound them, and the
    ratio would be about no quantity at all — story 9183's mismatched minuend and subtrahend,
    in ratio form."""
    env, _, du_log = _environment(tmp_path)
    assert _run(env).returncode == 0
    measured = du_log.read_text().split()
    assert env["VAR_TMP_DIR"] in measured
    assert "/var" not in measured


def test_a_budget_overrun_publishes_its_true_ratio(tmp_path: Path) -> None:
    """``/var/tmp`` has no writer-enforced ceiling on this box — the budget is held by a
    5-minute reaper that a fast writer outruns — so being over it is the expected failure, not
    an impossible state. The percent used to clamp to 100 (bug ``b380-3dfc-99fc-4a0e``), which
    made the one reading deployed to detect that failure incapable of reporting it."""
    env, aws_log, _ = _environment(tmp_path, var_tmp_bytes=9 * GIB, cap=4 * GIB)
    assert _run(env).returncode == 0
    assert _one(aws_log, "var_tmp_used_percent") == 225
    assert _one(aws_log, "var_tmp_bytes") == 9 * GIB


# --------------------------------------------------------------------------------------
# Silence, never a fabricated 0
# --------------------------------------------------------------------------------------


def test_an_unmeasurable_var_tmp_publishes_nothing_rather_than_zero(tmp_path: Path) -> None:
    """A 0 would read as an empty ``/var/tmp`` on a filling box, and
    ``rebar-var-tmp-usage-high`` is ``treat_missing_data = "breaching"`` so the silence pages
    instead."""
    env, aws_log, _ = _environment(tmp_path, var_tmp_bytes=None)
    assert _run(env).returncode == 0
    assert _values(aws_log, "var_tmp_bytes") == []
    assert _values(aws_log, "var_tmp_used_percent") == []


def test_the_size_is_still_published_when_the_budget_is_unreadable(tmp_path: Path) -> None:
    """Independently gated: losing the budget must not also take the MAGNITUDE off the air,
    since ``var_tmp_bytes`` is what an operator sizes the problem with."""
    env, aws_log, _ = _environment(tmp_path)
    env["VAR_TMP_MAX_BYTES"] = "0"
    assert _run(env).returncode == 0
    assert _one(aws_log, "var_tmp_bytes") > 0
    assert _values(aws_log, "var_tmp_used_percent") == []


# --------------------------------------------------------------------------------------
# The two heartbeats: WHICH mechanism is holding the line?
# --------------------------------------------------------------------------------------


def test_the_cleanup_heartbeat_is_published_on_every_tick_including_its_zero_path(
    tmp_path: Path,
) -> None:
    """The heartbeat rule (bug bff5). If the 0 path published nothing, "the cleanup stopped"
    and "the probe stopped" would be the same signal — and only one of them is survivable."""
    active, aws_active, _ = _environment(tmp_path / "on", cleanup_active=True)
    assert _run(active).returncode == 0
    assert _one(aws_active, "var_tmp_cleanup_active") == 1

    dead, aws_dead, _ = _environment(tmp_path / "off", cleanup_active=False)
    assert _run(dead).returncode == 0
    assert _one(aws_dead, "var_tmp_cleanup_active") == 0


def test_the_hard_quota_heartbeat_reports_the_regime_the_box_is_actually_in(
    tmp_path: Path,
) -> None:
    """This is the metric that keeps the whole story honest. The reaper is a mitigation with a
    fill-rate assumption; only an ENFORCED XFS project quota is a ceiling. Publishing which one
    is live means nobody has to take a runbook's word for it."""
    without, aws_without, _ = _environment(tmp_path / "no-quota", quota_enforced=False)
    assert _run(without).returncode == 0
    assert _one(aws_without, "var_tmp_hard_quota_in_effect") == 0

    with_quota, aws_with, _ = _environment(tmp_path / "quota", quota_enforced=True)
    assert _run(with_quota).returncode == 0
    assert _one(aws_with, "var_tmp_hard_quota_in_effect") == 1


def test_the_heartbeats_survive_a_var_tmp_that_cannot_be_measured(tmp_path: Path) -> None:
    """The size and the heartbeats fail independently: a ``du`` that times out must not also
    take "is anything bounding this tree" off the air."""
    env, aws_log, _ = _environment(tmp_path, var_tmp_bytes=None, cleanup_active=True)
    assert _run(env).returncode == 0
    assert _values(aws_log, "var_tmp_bytes") == []
    assert _one(aws_log, "var_tmp_cleanup_active") == 1
    assert _one(aws_log, "var_tmp_hard_quota_in_effect") == 0


# --------------------------------------------------------------------------------------
# Bounding — the 2026-09-04 lesson (bug 1205)
# --------------------------------------------------------------------------------------


def test_the_var_tmp_walk_is_wall_clock_bounded(tmp_path: Path) -> None:
    """Twelve unbounded journal rescans in this same probe took Gerrit off the air for 41
    minutes. ``/var/tmp`` is by definition a tree nobody planned the size of, so its walk must
    not be able to hold the 5-minute timer open. A bound that fires reports SILENCE, which
    pages — never a truncated number, which does not."""
    env, aws_log, _ = _environment(tmp_path)
    bin_dir = tmp_path / "bin"
    # A `timeout` that always reports the timeout exit status, without running the command.
    _stub(bin_dir, "timeout", "exit 124")
    assert _run(env).returncode == 0
    assert _values(aws_log, "var_tmp_bytes") == []
    assert _values(aws_log, "var_tmp_used_percent") == []


# --------------------------------------------------------------------------------------
# Resolving the cap script the probe has to execute (bug 5fb0-89ab-4466-41cc)
# --------------------------------------------------------------------------------------


def test_the_probe_finds_the_cap_script_under_its_installed_name(tmp_path: Path) -> None:
    """The INSTALLED layout, which no other test in this file exercises.

    Every case above runs ``infra/scripts/observability.sh`` in place, where ``vartmp-cap.sh``
    is a sibling — so they all drive the CHECKOUT layout and none of them can see the defect.
    In production the probe is installed as ``/usr/local/bin/rebar-observability.sh`` and
    ``vartmp-cap.sh`` is never written beside it: the cap script self-installs as
    ``rebar-vartmp-cap.sh``, because that path is the ``ExecStart`` of the reaper unit it
    renders. The sibling lookup therefore resolved a path nothing creates, ``--print-env`` and
    ``--check-active`` both failed with rc 127, and the result was NOT silence — it was
    ``var_tmp_used_percent`` off the air entirely plus a confident, false
    ``var_tmp_cleanup_active=0`` on a host whose reaper was genuinely running.

    Here the probe is copied to a directory holding ONLY the installed name, which is exactly
    what the box looks like. Reverting ``resolve_cap_sh`` to the bare sibling makes this fail.
    """
    env, aws_log, _ = _environment(tmp_path, var_tmp_bytes=GIB, cap=4 * GIB)

    installed = tmp_path / "usr-local-bin"
    installed.mkdir()
    probe = installed / "rebar-observability.sh"
    probe.write_bytes(SCRIPT.read_bytes())
    probe.chmod(probe.stat().st_mode | stat.S_IXUSR)
    cap = installed / "rebar-vartmp-cap.sh"
    cap.write_bytes((SCRIPT.parent / "vartmp-cap.sh").read_bytes())
    cap.chmod(cap.stat().st_mode | stat.S_IXUSR)

    # The defining property of the layout: the name the OLD code looked for is absent.
    assert not (installed / "vartmp-cap.sh").exists()

    env["VAR_TMP_INSTALLED_PATH"] = str(cap)
    # The budget must come from the cap script's `--print-env`, exactly as on the box. Leaving
    # the fixture's VAR_TMP_MAX_BYTES in the environment would satisfy the percent's guard
    # without the script ever being executed, and the percent half of this defect would pass
    # against an unreachable cap script. The cap script's own default is 4 GiB, so the expected
    # percentage is unchanged.
    env.pop("VAR_TMP_MAX_BYTES", None)
    assert subprocess.run(["bash", str(probe)], env=env, timeout=180, check=False).returncode == 0

    # The budget was readable, so the percent is on the air rather than silently skipped.
    assert _one(aws_log, "var_tmp_used_percent") == 25
    # And the heartbeat reports the truth instead of a coerced 0.
    assert _one(aws_log, "var_tmp_cleanup_active") == 1


def test_an_explicit_cap_script_override_still_beats_every_candidate(tmp_path: Path) -> None:
    """``VARTMP_CAP_SH`` is the seam the other suites inject a stub through, so the candidate
    list must never outrank it — otherwise a machine that happens to have a real cap script in
    ``/usr/local/bin`` would silently steer tests at the host's copy."""
    env, aws_log, _ = _environment(tmp_path, var_tmp_bytes=GIB, cap=4 * GIB)

    override = tmp_path / "override-cap.sh"
    override.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  --print-env) printf 'VAR_TMP_MAX_BYTES=%s\\n' 2147483648 ;;\n"
        "  --check-active) printf '1\\n' ;;\n"
        "  --check-quota) printf '0\\n' ;;\n"
        "esac\nexit 0\n"
    )
    override.chmod(override.stat().st_mode | stat.S_IXUSR)

    env["VARTMP_CAP_SH"] = str(override)
    assert _run(env).returncode == 0
    # 1 GiB against the override's 2 GiB budget — proof the override's number, not the
    # sibling's 4 GiB, is what the percent was measured against.
    assert _one(aws_log, "var_tmp_used_percent") == 50


@pytest.mark.parametrize(
    ("check_active_body", "expected"),
    [
        ("printf '1\\n'; exit 0", 1),
        ("printf '0\\n'; exit 0", 0),
        ("exit 127", -1),  # the cap script could not be executed at all
        ("exit 1", -1),  # it ran and failed without answering
        ("printf '\\n'; exit 0", -1),  # it answered with nothing
    ],
)
def test_the_heartbeat_separates_a_measured_no_from_an_unanswered_check(
    tmp_path: Path, check_active_body: str, expected: int
) -> None:
    """THREE outcomes, not two: 1 in force, 0 measured NOT in force, -1 could not determine.

    The old code coerced every non-``1`` answer to ``0``, so "the check said no" and "the check
    never ran" published the same confident number. That is what let this metric read "the
    reaper is dead" for hours on a host where the reaper units did not exist to be asked
    (bug 5fb0-89ab-4466-41cc) — an operator reading 0 had no way to tell a measurement from a
    coercion.

    The distinction is carried in the VALUE and never by withholding it: a heartbeat still
    publishes on every tick including its 0 path, because bug ``bff5-9163-cddd-4158`` reserves
    ABSENCE for "the publisher died", which is what makes the dead-man construction trustworthy
    under ``treat_missing_data = "breaching"``. Spending silence on "healthy but unknown" would
    make every dead-man alarm on the box ambiguous.

    ``rebar-var-tmp-cleanup-not-active`` is ``LessThanThreshold 1.0``, so -1 is below the
    threshold by construction and pages exactly as 0 does. The sentinel changes what an operator
    READS, not whether anyone is woken, and no alarm needs retuning to keep catching it.
    """
    env, aws_log, _ = _environment(tmp_path, var_tmp_bytes=GIB, cap=4 * GIB)

    cap = tmp_path / "probe-cap.sh"
    cap.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  --print-env) printf 'VAR_TMP_MAX_BYTES=%s\\n' 4294967296; exit 0 ;;\n"
        f"  --check-active) {check_active_body} ;;\n"
        "  --check-quota) printf '0\\n'; exit 0 ;;\n"
        "esac\nexit 0\n"
    )
    cap.chmod(cap.stat().st_mode | stat.S_IXUSR)
    env["VARTMP_CAP_SH"] = str(cap)

    assert _run(env).returncode == 0
    assert _one(aws_log, "var_tmp_cleanup_active") == expected
