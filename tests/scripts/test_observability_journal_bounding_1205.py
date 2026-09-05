"""The 5-minute observability probe must not read the whole retained journal (bug 1205).

`observability.sh` publishes marker-count deltas by persisting a CUMULATIVE count and
subtracting the previous one. A cumulative total that is RECOMPUTED can only be recomputed by
reading the journal from the beginning, so every counter re-read the entire retained journal:
four fixed scans plus one per `publish_autodeploy_marker_delta` call site, thirteen whole-journal
reads per run on a 1.7 GB journal.

Measured under gp3-throttled conditions, that exhausts the 300-second timer period at ~1.37 GB.
`install-observability.sh` renders a `Type=oneshot` service with no `TimeoutStartSec` — systemd
reports `TimeoutStartUSec=infinity` — driven by `OnUnitActiveSec=5min`, which is computed from the
last COMPLETED activation. Confirmed on systemd 255: a hung run does not delay the timer, it
DELETES its next elapse. One overrun silences the probe permanently, which is the 41-minute Gerrit
outage recorded on bug 1205-63b2-2c01-4e7f.

These tests assert OBSERVABLE BEHAVIOUR, never source text:

- how much journal a probe run actually asks for, measured by an argument-aware `journalctl` stub
  that records what it emitted, across two corpora of very different sizes;
- that a scan which CANNOT be measured publishes nothing rather than a fabricated 0 (the probe's
  own contract: "a probe that could not measure publishes NOTHING rather than a plausible value");
- that the rendered units carry a finite start timeout below their own timer period, asserted as a
  relation between the two rendered units rather than as a literal.
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

from _subprocess_env import subprocess_env

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra" / "scripts" / "observability.sh"
INSTALLER = REPO / "infra" / "scripts" / "install-observability.sh"
AUTODEPLOY_UNIT = REPO / "infra" / "systemd" / "rebar-autodeploy.service"
_SHA = "a" * 40

# Every persisted-state variable the script honours, so a test can put ALL of the probe's
# state under tmp_path and observe what it does to it.
_STATE_VARIABLES = (
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
    "MCP_RETIRE_CAP_OFFSET_FILE",
    "MCP_MEM_ABORT_OFFSET_FILE",
    "G2P_OFFSET_FILE",
)

# An argument-aware journald emulator. $JOURNAL_FILE holds one entry per line and the 1-based
# line number IS the cursor. It records the number of lines it emitted on every invocation into
# $EMITTED_FILE, which is what lets a test measure the probe's actual journal read volume.
#
# The pre-existing stubs in this suite are argument-BLIND (`cat "$JOURNAL_FILE"`), which is
# precisely why nobody noticed the scans were unbounded: a stub that returns the whole journal
# however it is asked cannot tell a bounded read from an unbounded one.
_JOURNALCTL_EMULATOR = """
    after=""
    tail_only=0
    show_cursor=0
    while [ $# -gt 0 ]; do
      case "$1" in
        --after-cursor) after="$2"; shift 2 ;;
        --after-cursor=*) after="${1#*=}"; shift ;;
        -n) tail_only="$2"; shift 2 ;;
        --lines) tail_only="$2"; shift 2 ;;
        --lines=*) tail_only="${1#*=}"; shift ;;
        --show-cursor) show_cursor=1; shift ;;
        *) shift ;;
      esac
    done
    total=$(wc -l < "$JOURNAL_FILE" | tr -d ' ')
    start=1
    if [ "$tail_only" != 0 ]; then
      start=$((total - tail_only + 1))
      [ "$start" -lt 1 ] && start=1
    elif [ -n "$after" ]; then
      start=$((after + 1))
    fi
    emitted=0
    if [ "$start" -le "$total" ]; then
      sed -n "${start},${total}p" "$JOURNAL_FILE"
      emitted=$((total - start + 1))
    fi
    printf '%s\\n' "$emitted" >> "$EMITTED_FILE"
    if [ "$show_cursor" -eq 1 ]; then
      printf -- '-- cursor: %s\\n' "$total"
    fi
    exit 0
"""


# A faithful `timeout`, provided explicitly rather than inherited: coreutils `timeout` does not
# exist on macOS, so without this the wall-clock bound would be exercised on Linux CI and
# silently skipped everywhere else — the test would pass for the wrong reason on half the hosts
# that run it. It signals the child rather than relying on an inherited alarm, because bash
# handles SIGALRM itself and an alarm-based stub therefore cannot interrupt a shell child.
# It also signals the process GROUP, as GNU timeout does: a grandchild that inherited stdout
# keeps a command substitution waiting for EOF long after its parent is dead, so killing only
# the direct child would leave the caller blocked past the bound it asked for.
_TIMEOUT_STUB = """
    exec perl -e '
      use POSIX ":sys_wait_h";
      my $t = shift;
      my $pid = fork();
      if ($pid == 0) { POSIX::setpgid(0, 0); exec @ARGV; exit 127; }
      POSIX::setpgid($pid, $pid);
      my $deadline = time() + $t;
      while (1) {
        if (waitpid($pid, WNOHANG) == $pid) { exit($? >> 8); }
        if (time() >= $deadline) {
          kill "TERM", -$pid; kill "KILL", -$pid; waitpid($pid, 0); exit 124;
        }
        select(undef, undef, undef, 0.1);
      }
    ' "$@"
"""


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _marker(token: str) -> str:
    payload = json.dumps({"ts": 1787645217, "reason": "over-cap", "detail": "synthetic"})
    return f"{token} {payload}"


def _environment(
    tmp_path: Path, journal_lines: list[str], *, journalctl_body: str = _JOURNALCTL_EMULATOR
) -> tuple[dict[str, str], Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"
    emitted = tmp_path / "emitted.log"
    emitted.write_text("")

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
    _stub(bin_dir, "journalctl", journalctl_body)
    # A real `timeout`, provided explicitly rather than inherited: coreutils `timeout` is absent
    # on macOS, so without this the wall-clock bound would be exercised on Linux and silently
    # skipped everywhere else — the test would pass for the wrong reason on half the hosts that
    # run it. perl's alarm gives the same semantics (run, then die on the deadline) everywhere.
    _stub(bin_dir, "timeout", _TIMEOUT_STUB)

    journal = tmp_path / "journal.txt"
    journal.write_text("".join(f"{line}\n" for line in journal_lines))

    state = tmp_path / "state"
    state.mkdir()
    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "EMITTED_FILE": str(emitted),
            "JOURNAL_FILE": str(journal),
            "REPL_LOG": str(tmp_path / "replication.log"),
            **{name: str(state / name.lower()) for name in _STATE_VARIABLES},
        }
    )
    (tmp_path / "replication.log").write_text("")
    return env, aws_log, emitted, journal


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
        values.append(int(parts[parts.index("--value") + 1]))
    return values


def _lines_read(emitted: Path) -> int:
    return sum(int(line) for line in emitted.read_text().split() if line)


def _journal_read_volume(tmp_path: Path, entries: int) -> tuple[int, int]:
    """Run the probe twice over a journal of `entries` lines; return (lines read, entries)."""
    lines = [_marker("AUTODEPLOY_MCP_RETIRE_CAP")] * entries
    env, _aws, emitted, _journal = _environment(tmp_path, lines)
    assert _run(env).returncode == 0
    # The first run initialises the counters. Measure the STEADY-STATE run: the one a healthy
    # box performs every five minutes forever.
    emitted.write_text("")
    assert _run(env).returncode == 0
    return _lines_read(emitted), entries


def test_steady_state_run_does_not_read_a_whole_copy_of_the_journal(tmp_path: Path) -> None:
    """A single probe run must not read even ONE full copy of the retained journal.

    This is the outage mechanism stated as a behaviour. On a 1.7 GB journal the probe read
    thirteen full copies per run, which cannot complete inside the 300-second timer period.
    """
    read, entries = _journal_read_volume(tmp_path, 4000)

    assert read < entries, (
        f"one probe run read {read} journal entries from a {entries}-entry journal — "
        "the probe re-reads the whole retained journal, once per counter"
    )


def test_journal_read_volume_does_not_scale_with_journal_size(tmp_path: Path) -> None:
    """Read volume is a function of the INTERVAL, not of how much journal is retained.

    This is the property that makes the probe safe as the box ages: a journal that grows from
    200 MB to its 4 GiB ceiling must not make the probe twenty times more expensive.
    """
    small_dir = tmp_path / "small"
    large_dir = tmp_path / "large"
    small_dir.mkdir()
    large_dir.mkdir()
    small, small_entries = _journal_read_volume(small_dir, 200)
    large, large_entries = _journal_read_volume(large_dir, 8000)

    assert large_entries == small_entries * 40
    assert large <= small + 200, (
        f"read volume scaled with retained journal: {small} entries read from a "
        f"{small_entries}-entry journal, {large} from a {large_entries}-entry one"
    )


def test_marker_delta_is_correct_across_intervals(tmp_path: Path) -> None:
    """Bounding must not cost correctness: each marker is counted exactly once, in one interval.

    A bounded scan that publishes a truncated or double-counted delta is worse than one that
    publishes nothing, so the delta contract is asserted alongside the read-volume one.
    """
    marker = _marker("AUTODEPLOY_MCP_RETIRE_CAP")
    env, aws_log, _emitted, journal = _environment(tmp_path, [marker] * 3)

    assert _run(env).returncode == 0
    # Cold start seeds and publishes 0: inherited history predates monitoring (bug e2a6).
    assert _values(aws_log, "mcp_retire_cap") == [0]

    journal.write_text(journal.read_text() + f"{marker}\n{marker}\n")
    assert _run(env).returncode == 0
    assert _values(aws_log, "mcp_retire_cap") == [0, 2]

    assert _run(env).returncode == 0
    assert _values(aws_log, "mcp_retire_cap") == [0, 2, 0]


def test_unmeasurable_journal_publishes_nothing_and_keeps_its_state(tmp_path: Path) -> None:
    """A scan that cannot complete publishes NOTHING — never a fabricated 0.

    This is the probe's own documented contract, stated at its memory and disk sections: "a probe
    that could not measure publishes NOTHING rather than a plausible value", because the ABSENCE
    of a datapoint is what the alarms read as "the host is dead". A journal read that fails or is
    cut off mid-scan must therefore leave the metric silent and the counter's state untouched, so
    the next successful run still covers the interval.
    """
    marker = _marker("AUTODEPLOY_MCP_RETIRE_CAP")
    env, aws_log, _emitted, _journal = _environment(tmp_path, [marker] * 3)

    # Establish state on a healthy journal first.
    assert _run(env).returncode == 0
    state = Path(env["MCP_RETIRE_CAP_OFFSET_FILE"])
    before = state.read_text() if state.exists() else ""
    aws_log.write_text("")

    # Now the journal cannot be read at all (journald wedged, or the scan hit its bound).
    _stub(tmp_path / "bin", "journalctl", "exit 1")

    assert _run(env).returncode == 0
    assert _values(aws_log, "mcp_retire_cap") == [], (
        "an unreadable journal published a fabricated datapoint; a wrong number is worse than "
        "a missing one, because the alarm cannot tell it from a healthy reading"
    )
    after = state.read_text() if state.exists() else ""
    assert after == before, "an unmeasurable scan advanced the counter's state"


def _render_units(tmp_path: Path) -> dict[str, str]:
    """Run the REAL installer with its side effects stubbed and return the rendered units."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    _stub(bin_dir, "systemctl", "exit 0")
    _stub(bin_dir, "install", "exit 0")
    env = subprocess_env()
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "UNIT_DIR": str(unit_dir)})

    result = subprocess.run(["bash", str(INSTALLER)], env=env, timeout=60, check=False)
    assert result.returncode == 0, "the installer could not be run with its unit dir redirected"
    return {path.name: path.read_text() for path in unit_dir.iterdir()}


def _seconds(unit_text: str, key: str) -> int | None:
    match = re.search(rf"^{key}=(\d+)(min|s|sec)?\s*$", unit_text, re.MULTILINE)
    if match is None:
        return None
    value = int(match.group(1))
    return value * 60 if match.group(2) == "min" else value


def test_observability_service_start_timeout_is_below_its_timer_period(tmp_path: Path) -> None:
    """A hung run must be killed before the timer's next elapse would have been.

    `OnUnitActiveSec` is measured from the last COMPLETED activation, so on a `Type=oneshot`
    service with no start timeout a run that never finishes does not merely delay the timer — it
    deletes the next elapse outright and the probe is silent until the host is rebooted. A start
    timeout strictly below the period is what makes that unreachable.
    """
    units = _render_units(tmp_path)
    service = units["rebar-observability.service"]
    timer = units["rebar-observability.timer"]

    timeout = _seconds(service, "TimeoutStartSec")
    period = _seconds(timer, "OnUnitActiveSec")

    assert period is not None
    assert timeout is not None, (
        "the oneshot service declares no TimeoutStartSec, so systemd gives it "
        "TimeoutStartUSec=infinity and one hung run latches the probe off permanently"
    )
    assert 0 < timeout < period


def test_autodeploy_service_start_timeout_is_below_its_timer_period() -> None:
    """The auto-deploy oneshot carries the identical latch, and it is what tracks `main`."""
    service = AUTODEPLOY_UNIT.read_text()
    timer = (REPO / "infra" / "systemd" / "rebar-autodeploy.timer").read_text()

    timeout = _seconds(service, "TimeoutStartSec")
    period = _seconds(timer, "OnUnitActiveSec")

    assert period is not None
    assert timeout is not None, (
        "rebar-autodeploy.service is a Type=oneshot with no TimeoutStartSec driven by "
        "OnUnitActiveSec; a hung deploy deletes the timer's next elapse the same way"
    )
    assert 0 < timeout < period


def test_old_format_state_without_a_cursor_publishes_zero_and_reads_no_history(
    tmp_path: Path,
) -> None:
    """The upgrade run: a state file holding a bare total and no cursor.

    On the first run after this change lands, every counter's persisted state is the OLD shape — a
    cumulative integer with no cursor. That is neither a cold start (the file exists) nor lost
    history. It must behave like a cold start for the cursor: seed from the journal tail, publish
    0, and above all NOT read the retention window to reconstruct a total, which is the very scan
    this change exists to remove.
    """
    marker = _marker("AUTODEPLOY_MCP_RETIRE_CAP")
    env, aws_log, emitted, _journal = _environment(tmp_path, [marker] * 500)
    Path(env["MCP_RETIRE_CAP_OFFSET_FILE"]).write_text("17\n")

    assert _run(env).returncode == 0

    assert _values(aws_log, "mcp_retire_cap") == [0], (
        "the upgrade run published a delta from an old-format state file; it must seed and "
        "publish 0, exactly as a cold start does"
    )
    assert _lines_read(emitted) < 500, (
        "the upgrade run read the retention window to reconstruct a cumulative total"
    )


def test_probe_terminates_when_a_sibling_dependency_hangs(tmp_path: Path) -> None:
    """No external command in a 5-minute probe may outlive its period.

    The journal scans are the ones that caused the outage, but they are an instance of a class:
    an unbounded external command inside a periodic probe. The mirror comparison's `git ls-remote`
    reaches the network with no bound at all, while the Gerrit REST read on the adjacent line is
    correctly `--max-time 10`. This drives the whole script with that dependency wedged and
    asserts the run still finishes.
    """
    marker = _marker("AUTODEPLOY_MCP_RETIRE_CAP")
    env, _aws, _emitted, _journal = _environment(tmp_path, [marker] * 5)
    # Bounded at 60s so the stub can never outlive the test, however the script behaves.
    _stub(tmp_path / "bin", "git", "sleep 60; exit 0")

    result = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=55, check=False)

    assert result.returncode == 0


# Same emulator, but journald REFUSES a cursor it no longer holds — which is what a real
# journalctl does once rotation has discarded the file the cursor names ("Failed to seek to
# cursor"). A tail read still works, because the journal itself is healthy.
_JOURNALCTL_ROTATED_PAST_CURSOR = """
    after=""
    tail_only=0
    show_cursor=0
    while [ $# -gt 0 ]; do
      case "$1" in
        --after-cursor) after="$2"; shift 2 ;;
        --after-cursor=*) after="${1#*=}"; shift ;;
        -n) tail_only="$2"; shift 2 ;;
        --lines) tail_only="$2"; shift 2 ;;
        --lines=*) tail_only="${1#*=}"; shift ;;
        --show-cursor) show_cursor=1; shift ;;
        *) shift ;;
      esac
    done
    total=$(wc -l < "$JOURNAL_FILE" | tr -d ' ')
    if [ -n "$after" ] && [ "$after" -gt "$total" ]; then
      echo "Failed to seek to cursor: Invalid argument" >&2
      exit 1
    fi
    start=1
    if [ "$tail_only" != 0 ]; then
      start=$((total - tail_only + 1))
      [ "$start" -lt 1 ] && start=1
    elif [ -n "$after" ]; then
      start=$((after + 1))
    fi
    emitted=0
    if [ "$start" -le "$total" ]; then
      sed -n "${start},${total}p" "$JOURNAL_FILE"
      emitted=$((total - start + 1))
    fi
    printf '%s\\n' "$emitted" >> "$EMITTED_FILE"
    if [ "$show_cursor" -eq 1 ]; then
      printf -- '-- cursor: %s\\n' "$total"
    fi
    exit 0
"""


def test_cursor_journald_no_longer_holds_reseeds_without_a_full_scan(tmp_path: Path) -> None:
    """A cursor rotated out from under the probe must reseed, never fall back to a full read.

    Once journald has discarded the file a cursor names it refuses the seek. The dangerous
    recovery is to give up on the cursor and re-read the journal from the beginning, which
    reinstates the exact scan this change removes — and does so precisely when the journal is at
    its largest. The safe recovery is to reseed from the tail and publish nothing for that run.
    """
    marker = _marker("AUTODEPLOY_MCP_RETIRE_CAP")
    env, aws_log, emitted, _journal = _environment(
        tmp_path, [marker] * 600, journalctl_body=_JOURNALCTL_ROTATED_PAST_CURSOR
    )
    state = Path(env["MCP_RETIRE_CAP_OFFSET_FILE"])
    # A total plus a cursor pointing past everything journald still holds.
    state.write_text("41 99999\n")

    assert _run(env).returncode == 0

    assert _values(aws_log, "mcp_retire_cap") == [], (
        "a rotated-past cursor published a datapoint; the interval cannot be measured"
    )
    assert _lines_read(emitted) < 600, (
        "the probe fell back to reading the whole journal when its cursor was refused"
    )
    assert "99999" not in state.read_text(), "the unusable cursor was left in place, stalling"
