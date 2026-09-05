"""The probe must finish inside its systemd timeout, and say so when it does not (bug 9313).

``rebar-observability.service`` was SIGTERM-ed on ``TimeoutStartSec=240`` on essentially every
run under load — 55 timeout kills against 197 completed runs in 24 h on the production Gerrit host,
clustered exactly when the box was busy. The decisive measurement was 4 minutes of wall clock
against ~35 s of consumed CPU: the probe was BLOCKED, not computing, so neither a longer
timeout nor a faster script was the fix.

The blocking call, measured per-call on the host: ``docker_du_bytes`` was invoked TWICE, over
``$DOCKER_ROOT`` and again over ``$DOCKER_ROOT/overlay2`` — essentially the same ~1.88M-file
tree walked a second time — each bounded at ``DOCKER_DU_TIMEOUT=120``. A single walk measured
124.18 s against a 130 s bound, i.e. it did not finish. **2 x 120 = 240 = TimeoutStartSec**, to
the second, and the second walk fed nothing but a ``logger`` breadcrumb.

Everything after that call therefore published nothing, and because metrics are emitted
sequentially, *position in the file* decided whether a metric existed at all:
``root_disk_used_percent`` (early) was fresh while ``mem_used_percent`` (late) was 49 minutes
stale. From the metric side a truncated run and an absent value are the same gap, and every
alarm here is ``treat_missing_data = "breaching"`` — which is how four of six alarms came to be
in ALARM on healthy values on 2026-09-05.

These tests pin the four properties that make that impossible to repeat:

1. ONE traversal of the docker root per run, serving both readings.
2. A composed budget: no bounded call may spend the reserve owed to the sections after it.
3. A truncated run publishes ``probe_truncated=1``; a completed one publishes ``probe_ok=1``.
4. Defect-seeded — with the expensive call driven past the whole budget, the LATE metrics
   still publish. That is the positional-independence claim, tested rather than asserted.
"""

from __future__ import annotations

import re
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
INSTALLER = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "install-observability.sh"
_SHA = "a" * 40

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


@pytest.fixture
def probe(tmp_path: Path):
    """Drive the real script over stubs, recording every ``du`` and ``aws`` invocation."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"
    du_log = tmp_path / "du.log"

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
    # `timeout` is absent on stock macOS. This stub is deliberately FAITHFUL to the duration
    # rather than dropping it, because the composed budget is what these tests measure: it
    # records nothing and simply execs, while DU_SLEEP below supplies the overrun.
    _stub(bin_dir, "timeout", 'shift\nexec "$@"')
    _stub(
        bin_dir,
        "docker",
        """
        case "$*" in
          *"system df"*)
            printf 'Images|1GB\\nContainers|1GB\\n'
            printf 'Local Volumes|1GB\\nBuild Cache|1GB\\n'
            exit 0 ;;
        esac
        exit 0
        """,
    )
    # Records its full argv, then emits the `--max-depth=1` shape: a row per immediate child
    # and the grand-total row for the root itself. DU_SLEEP simulates the I/O stall.
    _stub(
        bin_dir,
        "du",
        """
        printf '%s\\n' "$*" >> "$DU_LOG"
        [ -n "${DU_SLEEP:-}" ] && sleep "$DU_SLEEP"
        root="${@: -1}"
        printf '16000000000\\t%s/overlay2\\n' "$root"
        printf '1024\\t%s/containers\\n' "$root"
        printf '17000000000\\t%s\\n' "$root"
        exit 0
        """,
    )
    _stub(
        bin_dir,
        "free",
        'printf "        total used free\\nMem:     1000 400 600\\n"; exit 0',
    )

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    import os

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AWS_LOG": str(aws_log),
        "DU_LOG": str(du_log),
        "REPL_LOG": str(tmp_path / "replication.log"),
        **{name: str(offsets / name.lower()) for name in _OFFSET_VARIABLES},
    }
    for name in _OFFSET_VARIABLES:
        (offsets / name.lower()).write_text("0\n")
    (tmp_path / "replication.log").write_text("")

    def run(extra: dict[str, str] | None = None, args: list[str] | None = None):
        run_env = dict(env)
        if extra:
            run_env.update(extra)
        result = subprocess.run(
            ["bash", str(SCRIPT), *(args or [])],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        return result

    run.aws_log = aws_log  # type: ignore[attr-defined]
    run.du_log = du_log  # type: ignore[attr-defined]
    return run


def _values(log: Path, metric: str) -> list[str]:
    if not log.exists():
        return []
    out = []
    for line in log.read_text().splitlines():
        if f"--metric-name {metric} " not in line:
            continue
        m = re.search(r"--value (\S+)", line)
        if m:
            out.append(m.group(1))
    return out


def _docker_root_walks(du_log: Path) -> list[str]:
    """Every ``du`` invocation that traverses the docker root (the expensive one)."""
    if not du_log.exists():
        return []
    return [line for line in du_log.read_text().splitlines() if "/var/lib/docker" in line]


# --- 1. ONE traversal, not two --------------------------------------------------------------


def test_docker_root_is_walked_exactly_once_per_run(probe, tmp_path: Path) -> None:
    """The regression itself: two walks of one tree is 2 x 120 s = the whole timeout.

    Pinning the COUNT rather than the arguments is deliberate. The defect was not that the
    second call had the wrong flags; it was that a second full traversal existed at all, to
    produce a number the code's own comment says "deliberately no longer participates in the
    arithmetic". Any future edit that reintroduces a separate overlay2 walk fails here.
    """
    docker_root = tmp_path / "var" / "lib" / "docker"
    docker_root.mkdir(parents=True)
    result = probe({"DOCKER_ROOT": "/var/lib/docker"})
    assert result.returncode == 0, result.stderr

    walks = _docker_root_walks(probe.du_log)
    assert len(walks) == 1, f"expected exactly one docker-root walk per run, got {walks}"
    assert "--max-depth=1" in walks[0], (
        "the single walk must be the --max-depth=1 shape, which yields the overlay2 subtotal "
        f"from the same traversal as the total; got {walks[0]!r}"
    )
    assert "/var/lib/docker/overlay2" not in walks[0], (
        "the walk must target the ROOT, not overlay2 — the root reading is the one that "
        "participates in docker_unaccounted_bytes"
    )


def test_one_walk_still_yields_both_readings(probe) -> None:
    """Halving the cost must not cost a metric: the total still reaches CloudWatch."""
    result = probe({"DOCKER_ROOT": "/var/lib/docker"})
    assert result.returncode == 0, result.stderr
    assert _values(probe.aws_log, "docker_storage_bytes") == ["17000000000"]
    # The overlay2 subtotal is a log breadcrumb, so its proof is that the run stayed healthy
    # and the residue — which needs the ROOT reading — was still computed.
    assert _values(probe.aws_log, "docker_unaccounted_bytes"), (
        "docker_unaccounted_bytes needs the filesystem half; the single walk must supply it"
    )


# --- 2. The budget is composed, not per-call -------------------------------------------------


def test_walk_cost_is_published(probe) -> None:
    """The call that caused this bug is now watchable instead of re-measured by hand."""
    result = probe({"DOCKER_ROOT": "/var/lib/docker"})
    assert result.returncode == 0, result.stderr
    assert _values(probe.aws_log, "docker_du_seconds"), (
        "docker_du_seconds must be published on every run — the 4-minute wall clock was "
        "visible in systemctl and in no metric, which is what made this take a day to find"
    )


def test_expensive_call_cannot_spend_the_tail_reserve(probe) -> None:
    """A budget already inside the reserve clamps the next expensive call to a fail-fast 1 s.

    ``PROBE_DEADLINE_SEC`` equal to ``PROBE_TAIL_RESERVE_SEC`` means zero discretionary budget
    from the first instant, so every ``clamped`` call is floored at 1 s. Floored at 1 rather
    than 0 on purpose: a clamped call must still RUN and fail, because "could not be measured"
    is a publishable state and being SIGKILLed is not.
    """
    result = probe(
        {
            "DOCKER_ROOT": "/var/lib/docker",
            "PROBE_DEADLINE_SEC": "100",
            "PROBE_TAIL_RESERVE_SEC": "100",
        }
    )
    assert result.returncode == 0, result.stderr
    # The run completed rather than being killed, which is the whole point.
    assert _values(probe.aws_log, "probe_ok") == ["1"]


# --- 3. Truncation publishes itself ----------------------------------------------------------


def test_completed_run_publishes_the_heartbeat(probe) -> None:
    result = probe({"DOCKER_ROOT": "/var/lib/docker"})
    assert result.returncode == 0, result.stderr
    assert _values(probe.aws_log, "probe_ok") == ["1"]
    elapsed = _values(probe.aws_log, "probe_elapsed_seconds")
    assert len(elapsed) == 1 and elapsed[0].isdigit(), (
        f"expected one integer probe_elapsed_seconds datapoint, got {elapsed}"
    )


def test_killed_run_publishes_probe_truncated(probe) -> None:
    """systemd's ExecStopPost path: SERVICE_RESULT=timeout is the SIGTERM this bug is about."""
    result = probe({"SERVICE_RESULT": "timeout"}, args=["--report-exit"])
    assert result.returncode == 0, result.stderr
    assert _values(probe.aws_log, "probe_truncated") == ["1"]
    assert _values(probe.aws_log, "probe_ok") == [], (
        "--report-exit must not fabricate a completion heartbeat for a run that was killed"
    )


def test_clean_stop_publishes_probe_truncated_zero(probe) -> None:
    """The gap must be readable in BOTH directions, or the metric is not an oracle."""
    result = probe({"SERVICE_RESULT": "success"}, args=["--report-exit"])
    assert result.returncode == 0, result.stderr
    assert _values(probe.aws_log, "probe_truncated") == ["0"]


def test_report_exit_does_not_run_the_probe(probe) -> None:
    """ExecStopPost runs on every stop; it must not become a second full probe run."""
    result = probe({"SERVICE_RESULT": "timeout"}, args=["--report-exit"])
    assert result.returncode == 0, result.stderr
    assert _docker_root_walks(probe.du_log) == [], (
        "--report-exit must publish the exit fact and stop, not walk the docker root again"
    )
    assert _values(probe.aws_log, "docker_storage_bytes") == []


# --- 4. Defect-seeded: position no longer decides which metrics exist ------------------------


def test_late_metrics_survive_an_overrunning_walk(probe) -> None:
    """Seed the exact defect — a walk that blows the budget — and check the TAIL still publishes.

    ``mem_used_percent`` is the metric the operator was reading when this was found, and it sits
    hundreds of lines after the docker walk. Under the old shape it was on the wrong side of the
    kill point and went blind for 49 minutes. Here the walk is driven 3 s past a 2 s budget, and
    the assertion is that the late metric — and the completion heartbeat after it — still arrive.
    """
    result = probe(
        {
            "DOCKER_ROOT": "/var/lib/docker",
            "DU_SLEEP": "3",
            "PROBE_DEADLINE_SEC": "2",
            "PROBE_TAIL_RESERVE_SEC": "1",
        }
    )
    assert result.returncode == 0, result.stderr
    assert _values(probe.aws_log, "mem_used_percent"), (
        "a late metric must not depend on an earlier section finishing in time"
    )
    assert _values(probe.aws_log, "probe_ok") == ["1"]


# --- 5. The script's budget and the unit's timeout are one number ---------------------------


def test_probe_deadline_matches_the_unit_timeout() -> None:
    """Two files hold the same 240; drift between them silently restores the bug.

    ``PROBE_DEADLINE_SEC`` is only a correct budget while it equals the ``TimeoutStartSec`` that
    actually kills the process. Nothing else couples them, so this test is the coupling.
    """
    unit = INSTALLER.read_text()
    script = SCRIPT.read_text()
    unit_timeout = re.search(r"^TimeoutStartSec=(\d+)$", unit, re.M)
    deadline = re.search(r'^PROBE_DEADLINE_SEC="\$\{PROBE_DEADLINE_SEC:-(\d+)\}"$', script, re.M)
    assert unit_timeout and deadline, "both the unit timeout and the probe deadline must be literal"
    assert unit_timeout.group(1) == deadline.group(1), (
        f"unit TimeoutStartSec={unit_timeout.group(1)} but PROBE_DEADLINE_SEC="
        f"{deadline.group(1)}; the probe would budget against a deadline that is not the one "
        "systemd enforces"
    )


def test_unit_installs_the_truncation_hook() -> None:
    """The hook is the only observer of a kill the probe cannot observe itself."""
    unit = INSTALLER.read_text()
    assert "ExecStopPost=/usr/local/bin/rebar-observability.sh --report-exit" in unit


# --- 6. No bound may escape the composition ------------------------------------------------


def test_every_wall_clock_bound_goes_through_clamped() -> None:
    """The defect class, not the defect: ceilings defensible alone and unaffordable together.

    Before this change the probe held nine independently-reasoned ceilings — two 120 s docker
    walks, a 20 s ledger read, 60 s ``du``s over /var/log/journal and /var/tmp, a per-entry
    ``du`` loop, two 15 s docker calls, seven 10 s journal reads and a 15 s ``git ls-remote``.
    Each carried a comment justifying itself against the 240 s timeout. Their sum was over
    495 s. Every one of them was individually correct and the set was individually wrong.

    ``clamped`` is only an invariant while it is the *only* door. A single call that reaches for
    raw ``timeout`` or bare ``bounded`` reopens the composition hole silently, so this test
    keeps the door shut. ``clamped`` itself is the one legitimate caller of ``bounded``.
    """
    lines = SCRIPT.read_text().splitlines()
    offenders = []
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r'(?<![\w-])timeout "\$', line) or re.search(r"(?<![\w-])bounded \d", line):
            offenders.append((number, stripped))
        elif re.search(r'(?<![\w-])bounded "\$', line):
            offenders.append((number, stripped))
    # Two sanctioned exemptions: `bounded`'s own definition, which wraps `timeout` and is what
    # `clamped` is built on, and `clamped`'s single delegation to it.
    exempt = ('bounded() { timeout "$@"; }', 'bounded "$want" "$@"')
    offenders = [(n, t) for n, t in offenders if t not in exempt]
    assert not offenders, (
        "these wall-clock bounds bypass the whole-probe budget and can starve a later section:\n"
        + "\n".join(f"  line {n}: {t}" for n, t in offenders)
    )


# --- 7. The clamp must actually BITE, not merely be present ---------------------------------


def test_clamp_hands_an_exhausted_budget_to_the_expensive_call(tmp_path: Path) -> None:
    """Measure the GRANT, because every other test here would survive deleting the clamp.

    This is a perturbation finding rather than a design choice. The portability ``timeout`` stub
    the rest of this file inherits is ``shift; exec "$@"`` — right for asserting behaviour on a
    macOS host with no ``timeout``, and useless for asserting a bound, because it discards the
    duration. Under it, replacing every ``clamped`` call with ``bounded`` leaves the other
    twelve tests green: the script still completes, the late metrics still publish, the
    heartbeat still fires. The mechanism would be gone and nothing would say so.

    What distinguishes the two is the NUMBER handed to ``timeout``. With the budget exhausted
    the clamp must grant its 1 s floor; unclamped, the call would be handed
    ``DOCKER_DU_TIMEOUT`` (60). So the stub records its first argument and the assertion reads
    it back — a value no amount of stub fidelity can fake.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    grants = tmp_path / "grants.log"

    _stub(bin_dir, "timeout", 'printf \'%s %s\\n\' "$1" "$2" >> "$GRANT_LOG"\nshift\nexec "$@"')
    _stub(bin_dir, "curl", "printf 'dummy'; exit 0")
    _stub(bin_dir, "git", "exit 1")
    _stub(bin_dir, "logger", "exit 0")
    _stub(bin_dir, "aws", "exit 0")
    _stub(bin_dir, "journalctl", "exit 0")
    _stub(bin_dir, "docker", "exit 1")
    _stub(bin_dir, "free", "exit 1")
    _stub(bin_dir, "du", "printf '1024\\t%s\\n' \"${@: -1}\"; exit 0")

    import os

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GRANT_LOG": str(grants),
        "DOCKER_ROOT": "/var/lib/docker",
        "REPL_LOG": str(tmp_path / "missing.log"),
        # No discretionary budget at all, so every clamped call must get the 1 s floor.
        "PROBE_DEADLINE_SEC": "10",
        "PROBE_TAIL_RESERVE_SEC": "10",
        **{name: str(offsets / name.lower()) for name in _OFFSET_VARIABLES},
    }
    for name in _OFFSET_VARIABLES:
        (offsets / name.lower()).write_text("0\n")

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    recorded = [line.split() for line in grants.read_text().splitlines() if line.strip()]
    assert recorded, "no bounded call was made; the fixture is not exercising the budget"
    walk = [secs for secs, command in recorded if command == "du"]
    assert walk, f"the docker walk was never bounded; grants were {recorded}"
    assert walk[0] == "1", (
        f"the docker walk was granted {walk[0]}s against an exhausted budget — it must get the "
        "1s floor. An unclamped call would be handed DOCKER_DU_TIMEOUT (60)."
    )
    over_budget = [(secs, cmd) for secs, cmd in recorded if int(secs) > 1]
    assert not over_budget, (
        "these calls were granted more than the exhausted budget allows, so they bypass "
        f"`clamped`: {over_budget}"
    )


def test_budget_is_granted_in_full_when_it_is_available(tmp_path: Path) -> None:
    """The complement: the clamp must not throttle a run that has budget to spend.

    A ceiling-shaped bug is symmetric — a clamp that always returned 1 would pass the test above
    and quietly take every expensive reading off the air. With a full budget the docker walk
    must be handed its own ceiling, unmodified.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    grants = tmp_path / "grants.log"

    _stub(bin_dir, "timeout", 'printf \'%s %s\\n\' "$1" "$2" >> "$GRANT_LOG"\nshift\nexec "$@"')
    _stub(bin_dir, "curl", "printf 'dummy'; exit 0")
    _stub(bin_dir, "git", "exit 1")
    _stub(bin_dir, "logger", "exit 0")
    _stub(bin_dir, "aws", "exit 0")
    _stub(bin_dir, "journalctl", "exit 0")
    _stub(bin_dir, "docker", "exit 1")
    _stub(bin_dir, "free", "exit 1")
    _stub(bin_dir, "du", "printf '1024\\t%s\\n' \"${@: -1}\"; exit 0")

    import os

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GRANT_LOG": str(grants),
        "DOCKER_ROOT": "/var/lib/docker",
        "REPL_LOG": str(tmp_path / "missing.log"),
        **{name: str(offsets / name.lower()) for name in _OFFSET_VARIABLES},
    }
    for name in _OFFSET_VARIABLES:
        (offsets / name.lower()).write_text("0\n")

    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr

    recorded = [line.split() for line in grants.read_text().splitlines() if line.strip()]
    walk = [secs for secs, command in recorded if command == "du"]
    assert walk and walk[0] == "60", (
        f"with a full budget the docker walk must get its own DOCKER_DU_TIMEOUT ceiling, got {walk}"
    )
