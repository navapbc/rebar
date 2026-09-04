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

"OF THE SAME BYTES" is the load-bearing half, and patchset 1 of this change got it wrong:
it differenced a ``du`` of ``overlay2`` alone against a ledger that also counted the build
cache, so the subtrahend spanned bytes the minuend never did. Both sides are now the WHOLE
Docker root against the WHOLE ledger — a set neither the storage driver nor a future engine
row can quietly move out from under. Two tests below pin that choice against each of the
narrower formulations, and each would fail under the other.

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
    assert unaccounted == 17 * GIB - 9_529_000_000
    # ~7.7 GiB: far above the 2 GiB alarm threshold, and utterly invisible to `docker prune`.
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
        du_overlay2=5 * GIB,
    )
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_unaccounted_bytes") == 0


def test_the_ledger_sums_every_row_docker_accounts_for(tmp_path: Path) -> None:
    """Every ledger row is bytes Docker KNOWS about, so every row is subtracted.

    Including ``Local Volumes``. The minuend is a ``du`` of the WHOLE Docker root, and
    ``.../volumes`` is inside it, so leaving that row out of the subtrahend would report
    every byte of every named volume as unreachable residue.
    """
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("4GB", "1GB", "3GB", "2GB"),
        du_total=20 * GIB,
        du_overlay2=10 * GIB,
    )
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_unaccounted_bytes") == 20 * GIB - 10 * GB


def test_a_build_cache_is_not_reported_as_unreachable_residue(tmp_path: Path) -> None:
    """BuildKit cache bytes are ACCOUNTED bytes, so they belong in the subtrahend.

    This is the pin against the other candidate fix for the patchset-1 finding — narrowing the
    ledger to Images + Containers and keeping an overlay2 minuend. On this daemon the moby
    BuildKit worker is backed by the graphdriver's own layer store (moby
    ``builder/builder-next/controller.go`` hands ``GraphDriver`` and ``LayerStore`` to
    ``snapshot.NewSnapshotter``), so cache snapshots sit INSIDE ``overlay2``: dropping the
    Build Cache row would republish a fully prunable 4 GB cache as "bytes prune cannot reach"
    and page, on a 2 GiB threshold, every time a build ran.
    """
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("6GB", "0B", "0B", "4GB"),
        du_total=10 * GB,
        du_overlay2=10 * GB,
    )
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_unaccounted_bytes") == 0


def test_the_residue_survives_a_daemon_that_stores_layers_outside_overlay2(
    tmp_path: Path,
) -> None:
    """The two sides span the Docker ROOT, not one storage driver's subdirectory.

    This is the pin against the OTHER candidate fix — widening the ``du`` to overlay2 plus
    buildkit — and against patchset 1's overlay2-only minuend. With the containerd snapshotter
    enabled (``features.containerd-snapshotter``) the layer bytes live under
    ``/var/lib/docker/containerd`` and ``overlay2`` is essentially empty, so BOTH of those
    formulations clamp to 0 and the incident metric reads "perfectly healthy" forever on a box
    with 7.5 GiB of residue. Measuring the whole root is what makes the answer independent of
    which subdirectory this engine happens to use.
    """
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("9.529GB", "0B", "0B", "0B"),
        du_total=17 * GIB,
        du_overlay2=64 * 1024,  # a stale, near-empty overlay2 tree
    )
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_unaccounted_bytes") == 17 * GIB - 9_529_000_000


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


def test_an_unparseable_size_in_a_known_row_publishes_no_residue(tmp_path: Path) -> None:
    """A ledger that under-reports would inflate the residue and page for accounted bytes."""
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("4 gigs", "0B", "0B", "0B"),
        du_total=100 * GIB,
        du_overlay2=100 * GIB,
    )
    assert _run(env).returncode == 0
    assert _values(aws_log, "docker_unaccounted_bytes") == []
    assert _values(aws_log, "docker_buildkit_cache_bytes") == []


def test_an_unknown_ledger_row_does_not_take_the_metric_off_the_air(tmp_path: Path) -> None:
    """``docker system df``'s row set has grown before; a fifth type must not be fatal.

    Failing the whole read on a Type this does not recognise would let a future engine's
    presentation change silently retire the one metric the 2026-09-02 incident turned on. The
    residue then under-reports by that row instead — visible, bounded, and recoverable.
    """
    rows = _df_rows("6GB", "0B", "0B", "0B") + "Content Store|17 furlongs\\n"
    env, aws_log = _environment(tmp_path, df_rows=rows, du_total=10 * GB, du_overlay2=10 * GB)
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_unaccounted_bytes") == 4 * GB


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


def test_an_unreadable_overlay2_suppresses_nothing_at_all(tmp_path: Path) -> None:
    """The overlay2 read is a DIAGNOSTIC BREADCRUMB and nothing is derived from it.

    It names the subtree during an incident (16G of the 17G on 2026-09-02) and it goes into the
    ``rebar-health`` log line, but the published residue is the whole root against the whole
    ledger. Under patchset 1 an unreadable overlay2 took the incident metric off the air; it no
    longer can.
    """
    env, aws_log = _environment(tmp_path, du_overlay2=None)
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_storage_bytes") == 17 * GIB
    assert _one(aws_log, "docker_unaccounted_bytes") > 0


def test_an_unreadable_docker_root_publishes_no_residue(tmp_path: Path) -> None:
    """The root `du` IS the minuend now, so without it there is no defensible residue."""
    env, aws_log = _environment(tmp_path, du_total=None)
    assert _run(env).returncode == 0
    assert _values(aws_log, "docker_storage_bytes") == []
    assert _values(aws_log, "docker_unaccounted_bytes") == []
    # The ledger half is independent and still measurable, so it is still reported.
    assert _one(aws_log, "docker_buildkit_cache_bytes") > 0


def test_a_budget_overrun_publishes_a_percent_that_is_still_a_percent(tmp_path: Path) -> None:
    """CloudWatch's ``Percent`` unit is defined over 0-100; 300 rescales a whole dashboard.

    Nothing is lost by clamping: the unclamped ``docker_storage_bytes`` gauge beside it carries
    the magnitude, and the 85% alarm fires either way.
    """
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("1GB", "0B", "0B", str(12 * GIB) + "B"),
        du_total=60 * GIB,
        du_overlay2=60 * GIB,
        env_extra={
            "DOCKER_BUDGET_BYTES": str(20 * GIB),
            "DOCKER_BUILDKIT_CACHE_BYTES": str(5 * GIB),
        },
    )
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_storage_used_percent") == 100
    assert _one(aws_log, "docker_buildkit_cache_used_percent") == 100
    assert _one(aws_log, "docker_storage_bytes") == 60 * GIB


def test_a_missing_cap_script_still_publishes_the_raw_byte_readings(tmp_path: Path) -> None:
    """The caps and the readings come from different places, and only one can be missing.

    ``observability.sh`` reads the budget from its sibling ``docker-storage-cap.sh`` so that
    "percent of cap" can never mean a different cap than the one the daemon enforces. If that
    file is absent — a partial deploy — the percentages are genuinely underivable, but the raw
    byte readings are not: they are measured, not configured. Publishing them keeps the
    unaccounted-bytes alarm (the one no cap participates in) working on a host whose caps
    could not be read, and `set -u` must not turn the missing budget into a crashed probe.
    """
    env, aws_log = _environment(tmp_path, env_extra={"DOCKER_CAP_SH": str(tmp_path / "absent.sh")})
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_storage_bytes") == 17 * GIB
    assert _one(aws_log, "docker_unaccounted_bytes") > 0
    assert _values(aws_log, "docker_storage_used_percent") == []
    assert _values(aws_log, "docker_buildkit_cache_used_percent") == []
