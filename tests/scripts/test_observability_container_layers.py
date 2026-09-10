"""Writable container-layer metrics (story ``910b-2d43-4482-4c64``).

Each overlay2 ``upperdir`` is writable data unreachable by image or cache pruning.
``observability.sh`` §2i publishes total and exited-container bytes, percent of the reaper-held
share, and two heartbeats without a second ``docker system df`` walk.

Unmeasurable sizes stay silent rather than becoming zero; the breaching missing-data policy
then pages. Both heartbeats publish every tick, including zero, reserving absence for probe,
timer, or host failure. ``container_reaper_active`` reports whether cleanup bounds debris;
``container_quota_enforceable`` separately reports whether a hard ceiling is possible and
is deliberately unalarmed; zero is expected until ``rootflags=pquota`` is enabled.

Tests run the real scripts through PATH stubs without Docker, systemd, XFS, AWS, or CI.
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
    """Read the enforced share from ``docker-storage-cap.sh`` to prevent ratio drift."""
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
    """Render Docker's three-column rows.

    ``containers=None`` removes Containers; ``reclaimable=None`` models older two-column output.
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
    # The real cap script derives reaper and quota heartbeats from these stubs.
    _stub(bin_dir, "systemctl", f"exit {0 if reaper_active else 3}")
    enforcement = "ON" if quota_enforced else "OFF"
    _stub(
        bin_dir,
        "xfs_quota",
        f"cat <<'STATE'\nProject quota on / (/dev/nvme0n1p1)\n  Accounting: ON\n"
        f"  Enforcement: {enforcement}\nSTATE\nexit 0",
    )

    # Use a heredoc because `printf` treats the Reclaimable value's `%)` as a conversion.
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

    # Reaper health requires rendered units and a running timer, so install healthy fixtures.
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
            # `--check-active` combines the timer stub with rendered-unit state here.
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


# Readings


def test_the_writable_layer_size_and_the_exited_subset_are_both_published(
    tmp_path: Path,
) -> None:
    """Total bytes size the problem; exited bytes distinguish debris from live services."""
    env, aws_log = _environment(tmp_path, df_rows=_df_rows(containers="512MB", reclaimable="256MB"))
    _run(env)
    assert _one(aws_log, "container_writable_bytes") == 512_000_000
    assert _one(aws_log, "container_exited_bytes") == 256_000_000


def test_the_percentage_is_measured_against_the_share_the_reaper_holds(tmp_path: Path) -> None:
    """Use Docker's SizeRw bytes over the reaper-held share; overlay2 ``du`` includes images."""
    share = _share_bytes()
    env, aws_log = _environment(
        tmp_path, df_rows=_df_rows(containers=f"{share // 2}", reclaimable="0B")
    )
    _run(env)
    assert _one(aws_log, "container_writable_used_percent") == 50


def test_a_share_overrun_publishes_its_true_ratio(tmp_path: Path) -> None:
    """Report the unclamped ratio because unreapable running layers can exceed the share
    arbitrarily (bug ``b380-3dfc-99fc-4a0e``)."""
    share = _share_bytes()
    env, aws_log = _environment(
        tmp_path, df_rows=_df_rows(containers=f"{share * 3}", reclaimable="0B")
    )
    _run(env)
    assert _one(aws_log, "container_writable_used_percent") == 300
    assert _one(aws_log, "container_writable_bytes") == share * 3


def test_the_metrics_are_dimensionless_on_the_publishing_side(tmp_path: Path) -> None:
    """Metrics and alarms must both be dimensionless or CloudWatch stays in INSUFFICIENT_DATA."""
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


# Measurement failures stay silent


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
    """Publish silence, not false zero, when sizing fails; breaching missing-data then pages."""
    env, aws_log = _environment(tmp_path, df_rows=rows)
    _run(env)
    assert _values(aws_log, "container_writable_bytes") == [], why
    assert _values(aws_log, "container_writable_used_percent") == [], why


def test_a_two_column_rendering_still_publishes_the_total(tmp_path: Path) -> None:
    """Total and reclaimable bytes fail independently; missing Reclaimable preserves total."""
    env, aws_log = _environment(tmp_path, df_rows=_df_rows(containers="512MB", reclaimable=None))
    _run(env)
    assert _one(aws_log, "container_writable_bytes") == 512_000_000
    assert _values(aws_log, "container_exited_bytes") == []


# Heartbeats


@pytest.mark.parametrize("reaper_active", [True, False])
def test_the_reaper_heartbeat_publishes_on_every_tick_including_its_zero_path(
    tmp_path: Path, reaper_active: bool
) -> None:
    """Publish reaper health every tick, including zero, so absence means probe failure
    (bug ``bff5``)."""
    env, aws_log = _environment(tmp_path, reaper_active=reaper_active)
    _run(env)
    assert _one(aws_log, "container_reaper_active") == (1 if reaper_active else 0)


def test_the_reaper_heartbeat_is_published_even_when_the_daemon_is_gone(tmp_path: Path) -> None:
    """Reaper health is independent of Docker sizing and survives daemon failure."""
    env, aws_log = _environment(tmp_path, df_rows=None, reaper_active=True)
    _run(env)
    assert _one(aws_log, "container_reaper_active") == 1


@pytest.mark.parametrize("enforced", [True, False])
def test_the_quota_reading_reports_the_regime_the_box_is_actually_in(
    tmp_path: Path, enforced: bool
) -> None:
    """Report hard-quota enforceability; zero is expected until ``rootflags=pquota``, and the
    metric is deliberately unalarmed."""
    env, aws_log = _environment(tmp_path, quota_enforced=enforced)
    _run(env)
    assert _one(aws_log, "container_quota_enforceable") == (1 if enforced else 0)


def test_the_probe_never_reaps(tmp_path: Path) -> None:
    """All observer-invoked cap-script modes stay read-only; this timer never removes containers."""
    env, _ = _environment(tmp_path)
    _run(env)
    log = tmp_path / "docker-calls.log"
    calls = log.read_text() if log.exists() else ""
    assert "docker rm" not in calls, calls
    assert "prune" not in calls, calls


# --------------------------------------------------------------------------------------
# Resolving the cap script the probe has to execute (bug 5fb0-89ab-4466-41cc)
# --------------------------------------------------------------------------------------


def test_the_probe_finds_the_cap_script_under_its_installed_name(tmp_path: Path) -> None:
    """The INSTALLED layout, which no other case in this file exercises.

    Every case above runs ``infra/scripts/observability.sh`` in place, where
    ``container-cap.sh`` is a sibling — so they all drive the CHECKOUT layout and none can see
    the defect. In production the probe is installed as
    ``/usr/local/bin/rebar-observability.sh`` and ``container-cap.sh`` is never written beside
    it: the cap script self-installs as ``rebar-container-cap.sh``, because that path is the
    ``ExecStart`` of the reaper unit it renders. The sibling lookup therefore resolved a path
    nothing creates, and the result was ``container_writable_used_percent`` off the air
    entirely plus a confident, false ``container_reaper_active=0`` on a host whose reaper was
    genuinely running.
    """
    installed = tmp_path / "usr-local-bin"
    installed.mkdir()
    cap = installed / "rebar-container-cap.sh"
    cap.write_bytes((REPO_ROOT / "infra" / "scripts" / "container-cap.sh").read_bytes())
    cap.chmod(cap.stat().st_mode | stat.S_IXUSR)
    # container-cap.sh reads the writable-layer SHARE from docker-storage-cap.sh as its own
    # sibling (ONE budget with an internal split, ADR 0112), so the installed layout has to
    # carry that script too. install-observability.sh now deploys it under its repo basename —
    # the only name it has, since it defines no installed path of its own — and this fixture
    # models that layout rather than a half-installed box.
    docker_cap = installed / "docker-storage-cap.sh"
    docker_cap.write_bytes((REPO_ROOT / "infra" / "scripts" / "docker-storage-cap.sh").read_bytes())
    docker_cap.chmod(docker_cap.stat().st_mode | stat.S_IXUSR)

    env, aws_log = _environment(
        tmp_path, reaper_active=True, env_extra={"CONTAINER_INSTALLED_PATH": str(cap)}
    )

    probe = installed / "rebar-observability.sh"
    probe.write_bytes(SCRIPT.read_bytes())
    probe.chmod(probe.stat().st_mode | stat.S_IXUSR)

    # The defining property of the layout: the name the OLD code looked for is absent.
    assert not (installed / "container-cap.sh").exists()

    assert subprocess.run(["bash", str(probe)], env=env, timeout=180, check=False).returncode == 0

    # The share was readable, so the percent is on the air rather than silently skipped.
    assert _values(aws_log, "container_writable_used_percent")
    # And the heartbeat reports the truth instead of a coerced 0.
    assert _one(aws_log, "container_reaper_active") == 1
