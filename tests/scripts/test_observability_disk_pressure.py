"""The AUTODEPLOY_DISK_PRESSURE marker is published as a metric (task 9d15-d576-e0ca-4596).

`autodeploy.sh` emits a countable ``AUTODEPLOY_DISK_PRESSURE`` journal marker whenever the
pressure-triggered reclaim fires (story 28f9), but story 28f9 intentionally scoped the metric
out, so whether the reclaim gate ran at all was readable ONLY from the host journal. During
the 2026-08-17 root-disk incident that made a first-order question — "did the reclaim gate
run?" — unanswerable from CloudWatch. These tests drive the REAL ``observability.sh`` over a
synthetic unit journal and assert the marker is counted into ``rebar/host:disk_pressure_prunes``
with a record-anchored pattern and its own delta offset, and that a cold start seeds to 0
rather than publishing the inherited journal count.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40

METRIC = "disk_pressure_prunes"
PERSIST_METRIC = "disk_pressure_persists"

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
    path.chmod(0o755)


def _marker() -> str:
    """Reproduce the exact line autodeploy.sh `marker()` writes for a pressure prune."""
    payload = json.dumps(
        {"ts": 1787175217, "reason": "pressure-prune", "detail": "root disk at 92% (threshold 90%)"}
    )
    return f"AUTODEPLOY_DISK_PRESSURE {payload}"


def _persist_marker(streak: int = 3) -> str:
    """The line autodeploy.sh writes when a reclaim has been INEFFECTIVE for `streak` cycles."""
    payload = json.dumps(
        {
            "ts": 1787175217,
            "reason": "reclaim-ineffective",
            "detail": (
                f"root disk STILL 92% (threshold 80%) after {streak} consecutive reclaim "
                f"cycles; reclaim is not recovering the disk"
            ),
        }
    )
    return f"AUTODEPLOY_DISK_PRESSURE_PERSISTS {payload}"


def _environment(
    tmp_path: Path, journal_lines: list[str], *, seed_offsets: bool = True
) -> tuple[dict[str, str], Path, dict[str, Path]]:
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

    journal = tmp_path / "journal.txt"
    journal.write_text("".join(f"{line}\n" for line in journal_lines))
    _stub(bin_dir, "journalctl", 'cat "$JOURNAL_FILE"; exit 0')

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    paths = {name: offsets / name.lower() for name in _OFFSET_VARIABLES}
    if seed_offsets:
        # An explicit 0 in every offset: these tests are about the disk-pressure counter, and
        # an absent offset file is a cold start (seeds to the journal total and publishes 0 —
        # bug e2a6, asserted separately below).
        for path in paths.values():
            path.write_text("0\n")
    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "JOURNAL_FILE": str(journal),
            "REPL_LOG": str(tmp_path / "replication.log"),
            **{name: str(path) for name, path in paths.items()},
        }
    )
    (tmp_path / "replication.log").write_text("")
    return env, aws_log, paths


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
        values.append(int(parts[parts.index("--value") + 1]))
    return values


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)


def test_disk_pressure_markers_are_counted_into_their_own_metric(tmp_path: Path) -> None:
    """Marker lines are counted into disk_pressure_prunes; prose naming the marker is not.

    The prose line is the record-anchor regression (bug 8c2f): only journal records that BEGIN
    with `AUTODEPLOY_DISK_PRESSURE {` may count, so a log sentence that mentions the marker
    name mid-line never inflates the counter.
    """
    lines = [_marker(), "saw an AUTODEPLOY_DISK_PRESSURE marker earlier today", _marker()]
    env, aws_log, paths = _environment(tmp_path, lines)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, METRIC) == [2]
    assert paths["DISK_PRESSURE_OFFSET_FILE"].read_text().strip() == "2"


def test_cold_start_seeds_the_offset_and_publishes_zero(tmp_path: Path) -> None:
    """No offset file yet -> publish 0, never the inherited journal count (bug e2a6)."""
    env, aws_log, paths = _environment(tmp_path, [_marker()] * 3, seed_offsets=False)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, METRIC) == [0]
    assert paths["DISK_PRESSURE_OFFSET_FILE"].read_text().strip() == "3"


def test_second_run_publishes_only_the_new_markers(tmp_path: Path) -> None:
    """The counter is a DELTA: a second run counts only markers that arrived since the first."""
    env, aws_log, paths = _environment(tmp_path, [_marker()] * 2)

    first = _run(env)
    journal = Path(env["JOURNAL_FILE"])
    journal.write_text(journal.read_text() + _marker() + "\n")
    second = _run(env)

    assert (first.returncode, second.returncode) == (0, 0)
    assert _values(aws_log, METRIC) == [2, 1]
    assert paths["DISK_PRESSURE_OFFSET_FILE"].read_text().strip() == "3"


# --- the "reclaim is ineffective" counter (bug 9bc0-1200-1451-44bb) -----------
# disk_pressure_prunes counts INVOCATIONS, so "the reclaim gate never ran" and "it ran and
# reclaimed nothing" are the same number — the exact question the 2026-08-28 incident could not
# answer from CloudWatch. AUTODEPLOY_DISK_PRESSURE_PERSISTS is emitted only after N consecutive
# reclaim cycles each completed with the disk still pressured, so it is publishable as the
# alarmable "reclaim is ineffective" signal without host access.


def test_persistence_markers_are_counted_into_their_own_metric(tmp_path: Path) -> None:
    env, aws_log, paths = _environment(tmp_path, [_persist_marker(), _persist_marker(4)])

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, PERSIST_METRIC) == [2]
    assert paths["DISK_PRESSURE_PERSIST_OFFSET_FILE"].read_text().strip() == "2"


def test_the_two_disk_pressure_counters_never_cross_count(tmp_path: Path) -> None:
    """The DISCRIMINATOR, at the metric layer. `AUTODEPLOY_DISK_PRESSURE` is a strict prefix of
    `AUTODEPLOY_DISK_PRESSURE_PERSISTS`, so a pattern that is not anchored on the JSON `{` would
    count every persistence marker into the invocation counter too — drowning the rare
    "reclaim is ineffective" signal in ordinary reclaims, and inflating a counter that CI and
    incident sweeps read as a plain prune count."""
    lines = [_marker(), _persist_marker(), _persist_marker(4)]
    env, aws_log, paths = _environment(tmp_path, lines)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, METRIC) == [1], (
        "the invocation counter must see ONE prune, not three — the persistence marker's token "
        "starts with the invocation marker's token and must not match it"
    )
    assert _values(aws_log, PERSIST_METRIC) == [2]
    assert paths["DISK_PRESSURE_OFFSET_FILE"].read_text().strip() == "1"
    assert paths["DISK_PRESSURE_PERSIST_OFFSET_FILE"].read_text().strip() == "2"


def test_persistence_prose_is_not_counted(tmp_path: Path) -> None:
    """Record-anchored like every counter above (bug 8c2f): a log sentence naming the marker
    mid-line never inflates the counter."""
    lines = [
        "saw an AUTODEPLOY_DISK_PRESSURE_PERSISTS marker earlier today",
        _persist_marker(),
    ]
    env, aws_log, _ = _environment(tmp_path, lines)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, PERSIST_METRIC) == [1]


def test_persistence_cold_start_seeds_the_offset_and_publishes_zero(tmp_path: Path) -> None:
    """No offset file yet -> publish 0, never the inherited journal count (bug e2a6). Without
    this, a box that had been persistently pressured before the counter shipped would page on
    its first observability run for history it had already recovered from."""
    env, aws_log, paths = _environment(tmp_path, [_persist_marker()] * 5, seed_offsets=False)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, PERSIST_METRIC) == [0]
    assert paths["DISK_PRESSURE_PERSIST_OFFSET_FILE"].read_text().strip() == "5"


def test_persistence_second_run_publishes_only_the_new_markers(tmp_path: Path) -> None:
    env, aws_log, paths = _environment(tmp_path, [_persist_marker()])

    first = _run(env)
    journal = Path(env["JOURNAL_FILE"])
    journal.write_text(journal.read_text() + _persist_marker(4) + "\n")
    second = _run(env)

    assert (first.returncode, second.returncode) == (0, 0)
    assert _values(aws_log, PERSIST_METRIC) == [1, 1]
    assert paths["DISK_PRESSURE_PERSIST_OFFSET_FILE"].read_text().strip() == "2"


def test_no_persistence_markers_publishes_zero(tmp_path: Path) -> None:
    """NEGATIVE CONTROL at the metric layer: a box whose reclaims all worked publishes 0 here
    even while ordinary prunes are being counted, so the alarm is silent on a healthy box."""
    env, aws_log, _ = _environment(tmp_path, [_marker(), _marker()])

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, METRIC) == [2]
    assert _values(aws_log, PERSIST_METRIC) == [0]


# --- the review-gate SCRATCH volume (ADR 0112 decision 3, story aa40-cbda-ee38-481c) ---
# Gate snapshots and reviewbot-* clones moved off the root filesystem onto their own EBS
# volume. Root's used-percent therefore no longer answers for them, so the probe grew §2e.
# Two metrics, because a volume that failed to mount reads 0% used — indistinguishable from
# healthy — while every gate on the box refuses.


def _dimensioned(log: Path, metric: str, mount: str) -> list[int]:
    """Values published for ``metric`` carrying ``mount=<mount>``.

    ``disk_used_percent`` is ONE metric name shared by every mount and selected by its
    dimension (the convention §2 established for the data volume and the alarms match on),
    so a test that ignored dimensions could not tell the two publishers apart.
    """
    values: list[int] = []
    for line in log.read_text().splitlines() if log.exists() else []:
        parts = line.split()
        if "--metric-name" not in parts or parts[parts.index("--metric-name") + 1] != metric:
            continue
        if f"mount={mount}" not in line:
            continue
        values.append(int(parts[parts.index("--value") + 1]))
    return values


def _scratch_env(tmp_path: Path, *, mounted: bool) -> tuple[dict[str, str], Path, Path]:
    env, aws_log, _paths = _environment(tmp_path, [])
    # `df --output=pcent` is GNU-only and the suite also runs on macOS, so stub it the way
    # `aws`/`curl`/`journalctl` are already stubbed. The probe's parse (`tail -1 | tr -dc`)
    # is what is under test here, not the host df's flag support.
    _stub(tmp_path / "bin", "df", "printf 'Use%%\\n 42%%\\n'; exit 0")
    scratch = tmp_path / "gate-scratch"
    scratch.mkdir()
    if mounted:
        (scratch / ".gate-scratch-mounted").write_text("")
    env["GATE_SCRATCH_MOUNT"] = str(scratch)
    return env, aws_log, scratch


def test_a_mounted_scratch_volume_publishes_the_heartbeat_and_its_usage(tmp_path: Path) -> None:
    env, aws_log, scratch = _scratch_env(tmp_path, mounted=True)

    assert _run(env).returncode == 0

    assert _values(aws_log, "gate_scratch_mounted") == [1]
    assert _dimensioned(aws_log, "disk_used_percent", str(scratch)) == [42]


def test_an_unmounted_scratch_volume_still_publishes_a_zero_heartbeat(tmp_path: Path) -> None:
    """HEARTBEAT, NOT AN EVENT (ticket bff5). Publishing nothing on the bad path would leave
    ``rebar-gate-scratch-unmounted`` with no datapoint to evaluate, and its
    ``treat_missing_data = "breaching"`` would then page for the *dead publisher* reason —
    right outcome, wrong story, and indistinguishable from a dead host."""
    env, aws_log, _scratch = _scratch_env(tmp_path, mounted=False)

    assert _run(env).returncode == 0

    assert _values(aws_log, "gate_scratch_mounted") == [0]


def test_the_probe_reads_the_same_marker_the_gate_refusal_reads(tmp_path: Path) -> None:
    """The probe must not have its OWN idea of "mounted".

    ``mountpoint -q`` and rebar's proof marker can disagree — a bind mount is a mount point
    inside a container whether or not the host volume is mounted underneath it — and a
    monitoring signal that disagrees with the enforcement is worse than none: the alarm says
    healthy while every gate refuses. This pins them to one file.
    """
    from rebar.llm import gate_admission as ga

    code = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert ga._SCRATCH_MOUNTED_MARKER in code
    assert "mountpoint" not in code


def test_an_unmounted_scratch_volume_publishes_no_usage_reading(tmp_path: Path) -> None:
    """`df` on an unmounted mount point answers for the filesystem CONTAINING it.

    So an ungated publish would report ROOT's usage under `mount=<scratch>` — a real number
    about the wrong volume. rebar-gate-scratch-disk-high would then read healthy while scratch
    is gone, or page "scratch is full" during a root incident. Silence is the honest reading,
    and it is not a blind spot: the alarm's treat_missing_data = "breaching" pages on it, and
    rebar-gate-scratch-unmounted names the actual condition.
    """
    env, aws_log, scratch = _scratch_env(tmp_path, mounted=False)

    assert _run(env).returncode == 0

    assert _dimensioned(aws_log, "disk_used_percent", str(scratch)) == []
    assert _values(aws_log, "gate_scratch_mounted") == [0]
