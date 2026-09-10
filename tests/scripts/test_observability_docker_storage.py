"""Docker metrics include bytes absent from ``docker system df`` (story 9183).

During 2026-09-02, filesystem use exceeded Docker's ledger by about 6.5 GB, beyond prune's
reach. ``observability.sh`` therefore subtracts every recognized ledger row from the Docker
root's apparent bytes and publishes the nonnegative residue as ``docker_unaccounted_bytes``.
Whole-root parity covers BuildKit and alternate storage-driver trees; allocated bytes would
misclassify filesystem overhead.

Filesystem and ledger measurements fail independently. An unreadable source suppresses only
its derived metrics rather than fabricating zero, so breaching missing-data policy pages.
Separate storage and BuildKit gauges use their own caps and publish unclamped percentages.
Tests run the real script through ``docker``, ``find``, and ``aws`` stubs.
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
    apparent_total: int | None = None,
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
    # Model macOS without `timeout` by executing the wrapped command.
    _stub(bin_dir, "timeout", 'shift\nexec "$@"')

    # `docker system df` supplies the ledger; stats/ps only keep §2d running.
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

    # `find` is the FILESYSTEM half. ONE metadata walk now serves all readings (bugs
    # 9313-1fac-9f32-4b07 and 81cc-bced-62f4-40b9): allocated bytes for
    # docker_storage_bytes, apparent bytes for docker_unaccounted_bytes, and the overlay2
    # breadcrumb. Emitting a third child row keeps the parser honest about picking overlay2 by
    # PATH rather than by position.
    if du_total is None:
        find_body = "exit 1"
    else:
        apparent_total = du_total if apparent_total is None else apparent_total
        root = '"$1"'
        rows = []
        rows.append(f'printf "1\\t1\\t0\\t0\\t%s\\n" {root}')
        if du_overlay2 is not None:
            overlay_apparent = min(apparent_total, du_overlay2)
            overlay_blocks = du_overlay2 // 512
            rows.append(
                f'printf "1\\t2\\t{overlay_apparent}\\t{overlay_blocks}\\t%s/overlay2\\n" {root}'
            )
            remaining_allocated = du_total - du_overlay2
            remaining_apparent = apparent_total - overlay_apparent
        else:
            remaining_allocated = du_total
            remaining_apparent = apparent_total
        rows.append(
            f'printf "1\\t3\\t{remaining_apparent}\\t{remaining_allocated // 512}'
            f'\\t%s/containers\\n" {root}'
        )
        find_body = "\n".join(rows) + "\nexit 0\n"
    _stub(bin_dir, "find", find_body)
    _stub(bin_dir, "du", 'printf "1024\\t%s\\n" "${@: -1}"; exit 0')

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


# Bytes absent from Docker's ledger


def test_the_incident_shape_is_reported_as_unaccounted_bytes(tmp_path: Path) -> None:
    """Replay the incident: 17 GiB root minus the ~9.5 GB ledger exposes prune-invisible
    residue."""
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("9.529GB", "0B", "0B", "0B"),
        du_total=17 * GIB,
        du_overlay2=16 * GIB,
    )
    assert _run(env).returncode == 0
    unaccounted = _one(aws_log, "docker_unaccounted_bytes")
    assert unaccounted == 17 * GIB - 9_529_000_000
    # ~8.1 GiB, above the 2 GiB alarm and invisible to prune.
    assert unaccounted > 6 * GIB


def test_unaccounted_bytes_use_apparent_not_allocated_root_size(tmp_path: Path) -> None:
    """The Docker ledger is apparent-size accounting; the filesystem minuend must match.

    The production host measured 13,642,739,712 allocated bytes and 12,140,814,875 apparent
    bytes under the Docker root while Docker's own ledger accounted for 10,957,600,000 bytes.
    Subtracting the ledger from allocated bytes falsely reports XFS allocation overhead as
    unreachable residue.
    """
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("10.9576GB", "0B", "0B", "0B"),
        du_total=13_642_739_712,
        apparent_total=12_140_814_875,
        du_overlay2=12_000_000_000,
    )
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_storage_bytes") == 13_642_739_712
    assert _one(aws_log, "docker_unaccounted_bytes") == 1_183_214_875


def test_unaccounted_bytes_are_clamped_at_zero(tmp_path: Path) -> None:
    """Clamp negative residue to zero; a negative GreaterThanThreshold metric implies health."""
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("12GB", "0B", "0B", "0B"),
        du_total=5 * GIB,
        du_overlay2=5 * GIB,
    )
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_unaccounted_bytes") == 0


def test_the_ledger_sums_every_row_docker_accounts_for(tmp_path: Path) -> None:
    """Subtract every recognized ledger row, including volumes inside the whole-root minuend."""
    env, aws_log = _environment(
        tmp_path,
        df_rows=_df_rows("4GB", "1GB", "3GB", "2GB"),
        du_total=20 * GIB,
        du_overlay2=10 * GIB,
    )
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_unaccounted_bytes") == 20 * GIB - 10 * GB


def test_a_build_cache_is_not_reported_as_unreachable_residue(tmp_path: Path) -> None:
    """Subtract BuildKit cache because its graphdriver snapshots live inside overlay2;
    otherwise prunable cache becomes false residue."""
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
    """Measure the whole root so residue remains visible when containerd stores layers outside
    overlay2."""
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
    """Ignore unknown row types so future output cannot retire the metric; residue then
    over-reports by the unknown row."""
    rows = _df_rows("6GB", "0B", "0B", "0B") + "Content Store|17 furlongs\\n"
    env, aws_log = _environment(tmp_path, df_rows=rows, du_total=10 * GB, du_overlay2=10 * GB)
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_unaccounted_bytes") == 4 * GB


# Per-generator caps


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
    # BuildKit can reach its cap while total storage remains at 50%.
    assert _one(aws_log, "docker_buildkit_cache_used_percent") == 100
    assert _one(aws_log, "docker_du_ok") == 1


def test_the_docker_metrics_carry_no_dimensions(tmp_path: Path) -> None:
    """Keep metric and alarm dimensionless; asymmetry leaves CloudWatch in INSUFFICIENT_DATA."""
    env, aws_log = _environment(tmp_path)
    assert _run(env).returncode == 0
    for line in aws_log.read_text().splitlines():
        parts = line.split()
        if "--metric-name" not in parts:
            continue
        if not parts[parts.index("--metric-name") + 1].startswith("docker_"):
            continue
        assert "--dimensions" not in parts, line


# Measurement silence


def test_a_failed_du_publishes_nothing_at_all(tmp_path: Path) -> None:
    """On filesystem failure, publish ``docker_du_ok=0`` and suppress storage metrics instead
    of false zeros."""
    env, aws_log = _environment(tmp_path, du_total=None, du_overlay2=None)
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_du_ok") == 0
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
    """Overlay2 is diagnostic only; unreadability must not suppress whole-root storage or
    residue metrics."""
    env, aws_log = _environment(tmp_path, du_overlay2=None)
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_storage_bytes") == 17 * GIB
    assert _one(aws_log, "docker_unaccounted_bytes") > 0


def test_an_unreadable_docker_root_publishes_no_residue(tmp_path: Path) -> None:
    """The Docker-root walk IS the minuend now, so without it there is no defensible residue."""
    env, aws_log = _environment(tmp_path, du_total=None)
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_du_ok") == 0
    assert _values(aws_log, "docker_storage_bytes") == []
    assert _values(aws_log, "docker_unaccounted_bytes") == []
    # The ledger half is independent and still measurable, so it is still reported.
    assert _one(aws_log, "docker_buildkit_cache_bytes") > 0


def test_a_budget_overrun_publishes_its_true_ratio(tmp_path: Path) -> None:
    """Publish both percentages unclamped: best-effort targets can be exceeded, and 100 would
    hide breach magnitude (bug ``b380-3dfc-99fc-4a0e``)."""
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
    assert _one(aws_log, "docker_storage_used_percent") == 300  # 60 of 20 GiB
    assert _one(aws_log, "docker_buildkit_cache_used_percent") == 240  # 12 of 5 GiB
    assert _one(aws_log, "docker_storage_bytes") == 60 * GIB


def test_a_missing_cap_script_still_publishes_the_raw_byte_readings(tmp_path: Path) -> None:
    """An absent cap script must not crash `set -u`; suppress only percentages while preserving
    measured bytes and the cap-independent residue alarm."""
    env, aws_log = _environment(tmp_path, env_extra={"DOCKER_CAP_SH": str(tmp_path / "absent.sh")})
    assert _run(env).returncode == 0
    assert _one(aws_log, "docker_storage_bytes") == 17 * GIB
    assert _one(aws_log, "docker_unaccounted_bytes") > 0
    assert _values(aws_log, "docker_storage_used_percent") == []
    assert _values(aws_log, "docker_buildkit_cache_used_percent") == []
