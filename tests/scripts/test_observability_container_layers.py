"""Writable container layers, published as metrics (story ``910b-2d43-4482-4c64``).

Writable container layers are the part of ``/var/lib/docker`` no image or build-cache prune can
reach: each container's overlay2 ``upperdir``. On 2026-09-02 nothing measured them, so the only
signal was ``rebar-root-disk-pressure`` — "root disk high", which cannot name a generator.
``observability.sh`` §2i publishes their size, the exited-container subset, that size against
the share, and **two** heartbeats.

Four properties carry this file:

**Silence, never a fabricated 0.** A probe that could not size writable layers publishes
NOTHING, and ``rebar-container-writable-usage-high`` is ``treat_missing_data = "breaching"``
(bug 3276 defect 2) so the silence pages. A 0 would read as "no writable layers at all" on a box
that is filling.

**The heartbeats publish on EVERY tick, including their 0 path** (bug bff5), so their ABSENCE
means the probe, the timer or the host is dead rather than the reaper being fine.

**The two heartbeats answer different questions, and only one is alarmed.**
``container_reaper_active`` says whether anything is bounding the debris at all — a dead timer
leaves usage reading nominal while nothing enforces anything, which is the S4 finding this
story was told to expect. ``container_quota_enforceable`` says whether a HARD per-container
ceiling is even possible on this host, and is deliberately unalarmed because its honest value
is 0 until a reboot enables ``rootflags=pquota``.

**One ``docker system df``, not two.** The Containers row rides the same daemon walk §2f already
pays for; §2i adds no second one.

The tests drive the REAL ``observability.sh`` and the REAL ``container-cap.sh`` over PATH stubs:
no docker daemon, no systemd, no XFS, no AWS, no CI provider.
"""

from __future__ import annotations

import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "scripts" / "observability.sh"
CAP_SCRIPT = REPO_ROOT / "infra" / "scripts" / "docker-storage-cap.sh"
_SHA = "a" * 40

GIB = 1024**3

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


def _share_bytes() -> int:
    """The share as ``docker-storage-cap.sh`` states it — never a literal here, so a test cannot
    keep passing while the published percentage and the enforced share drift apart."""
    out = subprocess.run(
        ["bash", str(CAP_SCRIPT), "--print-env"], capture_output=True, text=True, check=True
    ).stdout
    for line in out.splitlines():
        if line.startswith("DOCKER_CONTAINER_WRITABLE_BYTES="):
            return int(line.split("=", 1)[1])
    raise AssertionError(f"docker-storage-cap.sh stated no writable-layer share:\n{out}")


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _df_rows(
    *,
    images: str = "1GB",
    containers: str | None = "512MB",
    reclaimable: str | None = "256MB",
    volumes: str = "0B",
    build_cache: str = "1GB",
) -> str:
    """The rows ``docker system df --format '{{.Type}}|{{.Size}}|{{.Reclaimable}}'`` emits.

    ``containers=None`` drops the Containers row entirely (an engine rendering §2i cannot read);
    ``reclaimable=None`` drops only the third field, modelling an OLDER engine whose format
    string yielded two columns.
    """
    lines = [f"Images|{images}|0B (0%)"]
    if containers is not None:
        row = f"Containers|{containers}"
        if reclaimable is not None:
            row += f"|{reclaimable} (100%)"
        lines.append(row)
    lines.append(f"Local Volumes|{volumes}|0B (0%)")
    lines.append(f"Build Cache|{build_cache}|0B (0%)")
    return "\n".join(lines)


def _environment(
    tmp_path: Path,
    *,
    df_rows: str | None = _df_rows(),
    reaper_active: bool = True,
    quota_enforced: bool = False,
    env_extra: dict[str, str] | None = None,
) -> tuple[dict[str, str], Path]:
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
    _stub(bin_dir, "journalctl", "exit 0")
    _stub(bin_dir, "timeout", 'shift\nexec "$@"')
    _stub(bin_dir, "du", 'printf "1024\\t$1\\n"; exit 0')
    # `systemctl is-active` decides the reaper heartbeat; `xfs_quota state -p` decides the quota
    # reading. Both are driven through the REAL container-cap.sh, not stubbed away.
    _stub(bin_dir, "systemctl", f"exit {0 if reaper_active else 3}")
    enforcement = "ON" if quota_enforced else "OFF"
    _stub(
        bin_dir,
        "xfs_quota",
        f"cat <<'STATE'\nProject quota on / (/dev/nvme0n1p1)\n  Accounting: ON\n"
        f"  Enforcement: {enforcement}\nSTATE\nexit 0",
    )

    # A heredoc, NOT `printf`: the Reclaimable column renders "256MB (100%)", and printf would
    # read the `%)` as a conversion and swallow the row — which is how the fixture silently
    # stopped modelling the thing under test the first time this was written.
    df_body = "exit 1" if df_rows is None else f"cat <<'DF'\n{df_rows}\nDF\nexit 0"
    docker_log = tmp_path / "docker-calls.log"
    _stub(
        bin_dir,
        "docker",
        f"""
        printf 'docker %s\\n' "$*" >> {docker_log}
        case "$*" in
          *"system df"*) {df_body} ;;
        esac
        exit 0
        """,
    )

    # A reaper is "in force" only when its units are the ones container-cap.sh renders AND the
    # timer is running — so the healthy case has to install them, exactly as the box does.
    unit_dir = tmp_path / "units"
    if reaper_active:
        _install_units(unit_dir)

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "REPL_LOG": str(tmp_path / "replication.log"),
            # The reaper units are absent in tmp, so `--check-active` answers on the timer stub
            # plus the rendered-unit comparison exactly as it does on the box.
            "CONTAINER_UNIT_DIR": str(tmp_path / "units"),
            **{name: str(offsets / name.lower()) for name in _OFFSET_VARIABLES},
        }
    )
    for name in _OFFSET_VARIABLES:
        (offsets / name.lower()).write_text("0\n")
    (tmp_path / "replication.log").write_text("")
    env.update(env_extra or {})
    return env, aws_log


def _install_units(unit_dir: Path) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    rendered = subprocess.run(
        ["bash", str(REPO_ROOT / "infra" / "scripts" / "container-cap.sh"), "--print-units"],
        capture_output=True,
        text=True,
        check=True,
        env=subprocess_env({"CONTAINER_INSTALLED_PATH": "/usr/local/bin/rebar-container-cap.sh"}),
    ).stdout
    current: Path | None = None
    for line in rendered.splitlines():
        if line.startswith("# ---- "):
            current = unit_dir / line.split()[2]
            current.write_text("")
            continue
        if current is not None:
            with current.open("a") as handle:
                handle.write(line + "\n")


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
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


def _lines_for(log: Path, metric: str) -> list[str]:
    return [
        line
        for line in log.read_text().splitlines()
        if f"--metric-name {metric} " in f"{line} " or line.split()[-1:] == [metric]
    ]


# --------------------------------------------------------------------------------------
# The readings
# --------------------------------------------------------------------------------------


def test_the_writable_layer_size_and_the_exited_subset_are_both_published(
    tmp_path: Path,
) -> None:
    """The total answers "how big"; the exited subset answers "is this debris or is it the live
    services", which is the difference between a cleanup task and an application problem."""
    env, aws_log = _environment(tmp_path, df_rows=_df_rows(containers="512MB", reclaimable="256MB"))
    _run(env)
    assert _one(aws_log, "container_writable_bytes") == 512_000_000
    assert _one(aws_log, "container_exited_bytes") == 256_000_000


def test_the_percentage_is_measured_against_the_share_the_reaper_holds(tmp_path: Path) -> None:
    """Both sides of the ratio must span the same bytes (the §2f/§2g/§2h rule): the numerator is
    the daemon's own SizeRw sum, not a ``du`` of overlay2, which would count image layers
    against a share that does not bound them."""
    share = _share_bytes()
    env, aws_log = _environment(
        tmp_path, df_rows=_df_rows(containers=f"{share // 2}", reclaimable="0B")
    )
    _run(env)
    assert _one(aws_log, "container_writable_used_percent") == 50


def test_the_percentage_is_clamped_to_one_hundred(tmp_path: Path) -> None:
    """CloudWatch's ``Percent`` unit is defined over 0-100, and a datapoint of 300 rescales every
    dashboard sharing the axis at exactly the moment somebody is reading it. Nothing is lost —
    the companion bytes gauge is unclamped and carries the magnitude."""
    share = _share_bytes()
    env, aws_log = _environment(
        tmp_path, df_rows=_df_rows(containers=f"{share * 3}", reclaimable="0B")
    )
    _run(env)
    assert _one(aws_log, "container_writable_used_percent") == 100
    assert _one(aws_log, "container_writable_bytes") == share * 3


def test_the_metrics_are_dimensionless_on_the_publishing_side(tmp_path: Path) -> None:
    """CloudWatch keys a metric by namespace+name+dimensions, so a dimension on only one side
    silently never matches and the alarm sits at INSUFFICIENT_DATA forever. The alarms in
    monitoring_autodeploy.tf declare none."""
    env, aws_log = _environment(tmp_path)
    _run(env)
    for metric in (
        "container_writable_bytes",
        "container_exited_bytes",
        "container_writable_used_percent",
        "container_reaper_active",
        "container_quota_enforceable",
    ):
        for line in _lines_for(aws_log, metric):
            assert "--dimensions" not in line, f"{metric} was published with a dimension: {line}"


# --------------------------------------------------------------------------------------
# Silence, never a fabricated 0
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "why"),
    [
        (None, "the daemon did not answer at all"),
        (_df_rows(containers=None), "this engine's rendering carried no Containers row"),
        (_df_rows(containers="wat"), "the Containers size was not parseable"),
    ],
)
def test_an_unmeasurable_writable_footprint_publishes_nothing(
    tmp_path: Path, rows: str | None, why: str
) -> None:
    """``treat_missing_data = "breaching"`` turns this silence into a page. A 0 would read as an
    empty container set on a box that is filling — bug 3276 defect 2, in metric form."""
    env, aws_log = _environment(tmp_path, df_rows=rows)
    _run(env)
    assert _values(aws_log, "container_writable_bytes") == [], why
    assert _values(aws_log, "container_writable_used_percent") == [], why


def test_a_two_column_rendering_still_publishes_the_total(tmp_path: Path) -> None:
    """The debris figure and the total fail INDEPENDENTLY. An engine rendering that costs us the
    Reclaimable column must not also cost us the size an operator sizes the problem with."""
    env, aws_log = _environment(tmp_path, df_rows=_df_rows(containers="512MB", reclaimable=None))
    _run(env)
    assert _one(aws_log, "container_writable_bytes") == 512_000_000
    assert _values(aws_log, "container_exited_bytes") == []


# --------------------------------------------------------------------------------------
# The heartbeats
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("reaper_active", [True, False])
def test_the_reaper_heartbeat_publishes_on_every_tick_including_its_zero_path(
    tmp_path: Path, reaper_active: bool
) -> None:
    """Bug ``bff5``: a heartbeat that only publishes when healthy makes "dead" and "fine"
    indistinguishable. This is the reading that pre-empts the finding S4's plan review raised —
    a byte cap enforced by a timer with no liveness signal can silently stop existing while
    usage reads nominal."""
    env, aws_log = _environment(tmp_path, reaper_active=reaper_active)
    _run(env)
    assert _one(aws_log, "container_reaper_active") == (1 if reaper_active else 0)


def test_the_reaper_heartbeat_is_published_even_when_the_daemon_is_gone(tmp_path: Path) -> None:
    """It reports on the REAPER, not on Docker. Losing the size reading must not also take the
    "is anything bounding this" answer off the air — they are separate failures with separate
    remediations."""
    env, aws_log = _environment(tmp_path, df_rows=None, reaper_active=True)
    _run(env)
    assert _one(aws_log, "container_reaper_active") == 1


@pytest.mark.parametrize("enforced", [True, False])
def test_the_quota_reading_reports_the_regime_the_box_is_actually_in(
    tmp_path: Path, enforced: bool
) -> None:
    """0 means no per-container ceiling is POSSIBLE here, so the reaper is the whole story and
    the percentage above is measured against a share only a timer holds. It is published without
    an alarm on purpose: enabling the quota needs ``rootflags=pquota`` and a reboot, so an alarm
    would page continuously and be muted within a day."""
    env, aws_log = _environment(tmp_path, quota_enforced=enforced)
    _run(env)
    assert _one(aws_log, "container_quota_enforceable") == (1 if enforced else 0)


def test_the_probe_never_reaps(tmp_path: Path) -> None:
    """The probe calls ``container-cap.sh`` twice every five minutes. If either read mode had a
    destructive side effect, the observability timer would be deleting containers on a schedule
    nobody reviewed as destructive."""
    env, _ = _environment(tmp_path)
    _run(env)
    log = tmp_path / "docker-calls.log"
    calls = log.read_text() if log.exists() else ""
    assert "docker rm" not in calls, calls
    assert "prune" not in calls, calls
