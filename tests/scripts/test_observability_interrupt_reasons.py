"""Interrupt counters split by the marker's reason field (bug 613a).

`autodeploy.sh` stamps every AUTODEPLOY_REVIEW_INTERRUPT with a `reason` that the rolled-up
`review_interrupts` metric discarded, so a CloudWatch-only sweep could not tell a chronically
busy bot (`bound-exceeded`) from a broken drain check (`signal-unavailable`) without shell
access to the host. These tests drive the REAL `observability.sh` over a synthetic unit journal
and assert the per-reason metrics separate, that the rolled-up total is still published, and
that each counter keeps its own delta offset.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40

BOUND_METRIC = "review_interrupts_bound_exceeded"
SIGNAL_METRIC = "review_interrupts_signal_unavailable"
TOTAL_METRIC = "review_interrupts"

_OFFSET_VARIABLES = (
    "REPL_OFFSET_FILE",
    "VOTER_OFFSET_FILE",
    "MERGE_OFFSET_FILE",
    "DEPLOY_OFFSET_FILE",
    "DEFER_OFFSET_FILE",
    "INTERRUPT_OFFSET_FILE",
    "INTERRUPT_BOUND_OFFSET_FILE",
    "INTERRUPT_SIGNAL_OFFSET_FILE",
    "G2P_OFFSET_FILE",
)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _marker(reason: str) -> str:
    """Reproduce the exact line autodeploy.sh `marker()` writes for an interrupt."""
    payload = json.dumps({"ts": 1786518175, "reason": reason, "detail": "target=x in_flight=2"})
    return f"AUTODEPLOY_REVIEW_INTERRUPT {payload}"


def _environment(tmp_path: Path, journal_lines: list[str]) -> tuple[dict[str, str], Path]:
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
    # Every offset starts at an EXPLICIT 0: these tests are about the reason SPLIT, and an
    # absent offset file is a cold start, which seeds to the journal total and publishes 0
    # instead of the inherited history (bug e2a6-9ee4-8d5c-4290, covered by
    # test_observability_coldstart_e2a6.py). Pre-initialising keeps the split under test.
    for name in _OFFSET_VARIABLES:
        (offsets / name.lower()).write_text("0\n")
    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "JOURNAL_FILE": str(journal),
            "REPL_LOG": str(tmp_path / "replication.log"),
            **{name: str(offsets / name.lower()) for name in _OFFSET_VARIABLES},
        }
    )
    (tmp_path / "replication.log").write_text("")
    return env, aws_log


def _values(log: Path, metric: str) -> list[int]:
    values: list[int] = []
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


def test_each_reason_is_counted_into_its_own_metric(tmp_path: Path) -> None:
    """A mixed journal splits by reason, and the rolled-up total is their sum."""
    lines = [_marker("bound-exceeded")] * 3 + [_marker("signal-unavailable")] * 2
    env, aws_log = _environment(tmp_path, lines)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, BOUND_METRIC) == [3]
    assert _values(aws_log, SIGNAL_METRIC) == [2]
    assert _values(aws_log, TOTAL_METRIC) == [5]


def test_bound_exceeded_alone_leaves_signal_unavailable_at_zero(tmp_path: Path) -> None:
    """The piranha case: four firings that were ALL bound-exceeded must not implicate the probe.

    This is the discrimination the ticket exists for — a sweep reading only CloudWatch sees the
    urgent `signal-unavailable` counter sitting at 0 and needs no shell access to conclude it.
    """
    env, aws_log = _environment(tmp_path, [_marker("bound-exceeded")] * 4)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, BOUND_METRIC) == [4]
    assert _values(aws_log, SIGNAL_METRIC) == [0]
    assert _values(aws_log, TOTAL_METRIC) == [4]


def test_signal_unavailable_alone_leaves_bound_exceeded_at_zero(tmp_path: Path) -> None:
    """The urgent case must be attributable on its own, not diluted into the total."""
    env, aws_log = _environment(tmp_path, [_marker("signal-unavailable")] * 2)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, BOUND_METRIC) == [0]
    assert _values(aws_log, SIGNAL_METRIC) == [2]
    assert _values(aws_log, TOTAL_METRIC) == [2]


def test_reason_counters_are_published_dimensionless(tmp_path: Path) -> None:
    """CloudWatch keys a metric by namespace+name+dimensions; the alarms carry no dimensions."""
    env, aws_log = _environment(tmp_path, [_marker("bound-exceeded")])

    _run(env)

    published = [
        line
        for line in aws_log.read_text().splitlines()
        if any(metric in line.split() for metric in (BOUND_METRIC, SIGNAL_METRIC, TOTAL_METRIC))
    ]
    assert published
    assert all("--dimensions" not in line for line in published)
    assert all("--namespace rebar/host" in line for line in published)


def test_each_reason_counter_keeps_its_own_offset(tmp_path: Path) -> None:
    """A second run over a grown journal publishes only that reason's NEW markers."""
    journal = [_marker("bound-exceeded"), _marker("signal-unavailable")]
    env, aws_log = _environment(tmp_path, journal)

    _run(env)
    Path(env["JOURNAL_FILE"]).write_text(
        "".join(f"{line}\n" for line in journal + [_marker("signal-unavailable")] * 2)
    )
    _run(env)

    assert _values(aws_log, BOUND_METRIC) == [1, 0]
    assert _values(aws_log, SIGNAL_METRIC) == [1, 2]
    assert _values(aws_log, TOTAL_METRIC) == [2, 2]
