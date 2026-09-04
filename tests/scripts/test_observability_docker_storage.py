"""Docker generator metrics, including the bytes ``docker system df`` cannot see (story 9183).

The 2026-09-02 outage is the whole argument for this file. ``/var/lib/docker`` held 17G — 16G
of it ``overlay2`` across 67 layer directories — while ``docker system df`` reported ~9.5 GB
and ZERO dangling images. Roughly **6.5 GB of orphaned overlay2 was invisible to Docker's own
accounting**, so four rounds of prune-based reclamation had a combined ceiling of ~1.06 GB
against a 29 GB problem. Any cap or alarm built on ``docker system df`` alone is blind to
exactly the bytes that caused the incident.

So ``observability.sh`` takes TWO INDEPENDENT measurements of the same bytes — the filesystem
(``du``) and Docker's ledger (``docker system df``) — and publishes their difference as
``docker_unaccounted_bytes``. These tests drive the real script over stubbed ``docker``, ``du``
and ``aws``.

The other load-bearing property is SILENCE: a probe that could not measure publishes NOTHING
rather than a plausible ``0``. Every one of these alarms is ``treat_missing_data = "breaching"``
(bug 3276 defect 2), so silence pages — while a fabricated ``0`` would read as a healthy,
empty Docker root on a box whose disk is filling.
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
GB = 1000**3

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


def _df_rows(images: str, containers: str, volumes: str, build_cache: str) -> str:
    """The four rows ``docker system df --format '{{.Type}}|{{.Size}}'`` emits."""
    return (
        f"Images|{images}\\n"
        f"Containers|{containers}\\n"
        f"Local Volumes|{volumes}\\n"
        f"Build Cache|{build_cache}\\n"
    )


def _environment(
    tmp_path: Path,
    *,
    df_rows: str | None = _df_rows("9.529GB", "0B", "0B", "1.2GB"),
    du_total: int | None = 17 * GIB,
    du_overlay2: int | None = 16 * GIB,
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
    # `timeout` is absent on stock macOS: drop the duration and exec the wrapped command,
    # the same portability stub tests/unit/test_autodeploy_prune.py already uses.
    _stub(bin_dir, "timeout", 'shift\nexec "$@"')

    # `docker system df` is the LEDGER half. `docker stats`/`ps` keep §2d alive so the whole
    # script still runs; they are not what these tests are about.
    df_body = "exit 1" if df_rows is None else f'printf "{df_rows}"; exit 0'
    _stub(
        bin_dir,
        "docker",
        f"""
        case "$*" in
          *"system df"*) {df_body} ;;
          *stats*)       exit 0 ;;
          *ps*)          exit 0 ;;
        esac
        exit 0
        """,
    )

    # `du` is the FILESYSTEM half: `du -sx --block-size=1 <path>` prints "<bytes>\\t<path>".
    if du_total is None and du_overlay2 is None:
        du_body = "exit 1"
    else:
        overlay = "exit 1" if du_overlay2 is None else f'printf "{du_overlay2}\\t$1\\n"; exit 0'
        total = "exit 1" if du_total is None else f'printf "{du_total}\\t$1\\n"; exit 0'
        du_body = f"""
        for a in "$@"; do
          case "$a" in
            *overlay2) {overlay} ;;
          esac
        done
        {total}
        """
    _stub(bin_dir, "du", du_body)

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "REPL_LOG": str(tmp_path / "replication.log"),
            **{name: str(offsets / name.lower()) for name in _OFFSET_VARIABLES},
        }
    )
    for name in _OFFSET_VARIABLES:
        (offsets / name.lower()).write_text("0\n")
    (tmp_path / "replication.log").write_text("")
    if env_extra:
        env.update(env_extra)
    return env, aws_log


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
# The crux: bytes Docker's own accounting cannot see
# --------------------------------------------------------------------------------------


def test_the_incident_shape_is_reported_as_unaccounted_bytes(tmp_path: Path) -> None:
    """16 GiB of overlay2 against a ~9.5 GB ledger must surface the ~6.5 GB residue.

    This is the 2026-09-02 measurement replayed. ``docker system df`` said 9.529GB with zero
    dangling images, so no prune could reach the difference — and nothing published it, which
    is why five hours went into ``du``.
    """
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("9.529GB", "0B", "0B", "0B"),
        du_total=17 * GIB,
        du_overlay2=16 * GIB,
    )
    assert _run(env).returncode == 0
    unaccounted = _one(aws_log, "docker_unaccounted_bytes")
    assert unaccounted == 16 * GIB - 9_529_000_000
    # ~6.6 GiB: far above the 2 GiB alarm threshold, and utterly invisible to `docker prune`.
    assert unaccounted > 6 * GIB


def test_unaccounted_bytes_are_clamped_at_zero(tmp_path: Path) -> None:
    """The ledger may legitimately exceed a `du` of overlay2 (shared layers, other dirs).

    A negative "unaccounted" would be nonsense, and a negative datapoint against a
    ``GreaterThanThreshold`` alarm reads as reassuring — worse than nonsense.
    """
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("12GB", "0B", "0B", "0B"),
        du_total=5 * GIB,
        du_overlay2=4 * GIB,
    )
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_unaccounted_bytes") == 0


def test_the_ledger_sums_images_containers_and_build_cache(tmp_path: Path) -> None:
    """All three ledger rows are bytes Docker KNOWS about, so all three are subtracted."""
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("4GB", "1GB", "0B", "2GB"),
        du_total=10 * GIB,
        du_overlay2=10 * GIB,
    )
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_unaccounted_bytes") == 10 * GIB - 7 * GB


@pytest.mark.parametrize(
    ("rendered", "expected"),
    [
        ("0B", 0),
        ("512B", 512),
        ("1.2kB", 1200),
        ("9.529GB", 9_529_000_000),
        ("1.5MB", 1_500_000),
        ("2GiB", 2 * 1024**3),
        ("100MiB", 100 * 1024**2),
    ],
)
def test_docker_human_sizes_parse_to_bytes(tmp_path: Path, rendered: str, expected: int) -> None:
    """``docker system df`` renders human sizes; SI and binary suffixes both occur."""
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows(rendered, "0B", "0B", "0B"),
        du_total=100 * GIB,
        du_overlay2=100 * GIB,
    )
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_unaccounted_bytes") == 100 * GIB - expected


# --------------------------------------------------------------------------------------
# Per-generator usage against the caps
# --------------------------------------------------------------------------------------


def test_storage_and_buildkit_are_published_against_their_own_caps(tmp_path: Path) -> None:
    """Two generators, two readings — neither answers for the other (the 3e92 argument)."""
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("4GB", "0B", "0B", str(5 * 1024**3) + "B"),
        du_total=10 * GIB,
        du_overlay2=9 * GIB,
        env_extra={
            "DOCKER_BUDGET_BYTES": str(20 * GIB),
            "DOCKER_BUILDKIT_CACHE_BYTES": str(5 * GIB),
        },
    )
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_storage_bytes") == 10 * GIB
    assert _one(aws_log, "docker_storage_used_percent") == 50  # 10 of 20 GiB
    assert _one(aws_log, "docker_buildkit_cache_bytes") == 5 * GIB
    # BuildKit is AT its cap while the whole budget reads a comfortable 50%: one alarm
    # structurally cannot answer for the other.
    assert _one(aws_log, "docker_buildkit_cache_used_percent") == 100


def test_the_docker_metrics_carry_no_dimensions(tmp_path: Path) -> None:
    """Dimensionless on BOTH sides, following ``root_disk_used_percent`` (§2b).

    CloudWatch keys a metric by namespace+name+dimensions, so a dimension on only one side
    silently never matches and the alarm sits on ``INSUFFICIENT_DATA`` forever.
    """
    env, aws_log = _environment(tmp_path)
    assert _run(env).returncode == 0
    for line in aws_log.read_text().splitlines():
        parts = line.split()
        if "--metric-name" not in parts:
            continue
        if not parts[parts.index("--metric-name") + 1].startswith("docker_"):
            continue
        assert "--dimensions" not in parts, line


# --------------------------------------------------------------------------------------
# Silence, not a plausible zero
# --------------------------------------------------------------------------------------


def test_a_failed_du_publishes_nothing_at_all(tmp_path: Path) -> None:
    """A `du` that could not run must not be reported as an empty Docker root.

    ``treat_missing_data = "breaching"`` turns this silence into a page; a fabricated ``0``
    would instead read as perfect health on a box that is filling.
    """
    env, aws_log = _environment(tmp_path, du_total=None, du_overlay2=None)
    assert _run(env).returncode == 0
    for metric in (
        "docker_storage_bytes",
        "docker_storage_used_percent",
        "docker_unaccounted_bytes",
    ):
        assert _values(aws_log, metric) == []


def test_a_failed_docker_system_df_publishes_no_ledger_derived_metric(tmp_path: Path) -> None:
    """Without the ledger there is no defensible "unaccounted", so none is invented."""
    env, aws_log = _environment(tmp_path, df_rows=None)
    assert _run(env).returncode == 0
    assert _values(aws_log, "docker_unaccounted_bytes") == []
    assert _values(aws_log, "docker_buildkit_cache_bytes") == []
    # The filesystem half is independent and still measurable, so it is still reported.
    assert _one(aws_log, "docker_storage_bytes") == 17 * GIB


def test_an_unreadable_overlay2_does_not_suppress_the_whole_budget_reading(
    tmp_path: Path,
) -> None:
    """The two `du` reads answer different questions; one failing must not mute the other."""
    env, aws_log = _environment(tmp_path, du_overlay2=None)
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_storage_bytes") == 17 * GIB
    assert _values(aws_log, "docker_unaccounted_bytes") == []


def test_a_missing_cap_script_still_publishes_the_raw_byte_readings(tmp_path: Path) -> None:
    """The caps and the readings come from different places, and only one can be missing.

    ``observability.sh`` reads the budget from its sibling ``docker-storage-cap.sh`` so that
    "percent of cap" can never mean a different cap than the one the daemon enforces. If that
    file is absent — a partial deploy — the percentages are genuinely underivable, but the raw
    byte readings are not: they are measured, not configured. Publishing them keeps the
    unaccounted-overlay2 alarm (the one no cap participates in) working on a host whose caps
    could not be read, and `set -u` must not turn the missing budget into a crashed probe.
    """
    env, aws_log = _environment(tmp_path, env_extra={"DOCKER_CAP_SH": str(tmp_path / "absent.sh")})
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_storage_bytes") == 17 * GIB
    assert _one(aws_log, "docker_unaccounted_bytes") > 0
    assert _values(aws_log, "docker_storage_used_percent") == []
    assert _values(aws_log, "docker_buildkit_cache_used_percent") == []
