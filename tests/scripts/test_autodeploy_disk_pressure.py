"""autodeploy must reclaim docker garbage on the quiescent-main no-op tick (story 28f9).

``prune_docker_caches`` only runs on autodeploy's deploy and backoff paths. When ``main`` is
quiescent every ~2-min timer tick hits the "up to date … no-op" early exit (autodeploy.sh:228)
and returns WITHOUT pruning, so nothing reclaims the 30G root disk between deploys. On
2026-08-04/06 that left the box pinned at 92-96% for ~39h and held ``rebar-root-disk-pressure``
in ALARM. The fix adds a pressure-triggered, throttled reclaim to the no-op path: when root
``/`` usage is at/above ``DISK_PRESSURE_PCT`` and the throttle window has elapsed, run the same
``prune_docker_caches`` the deploy path uses, and emit a countable ``AUTODEPLOY_DISK_PRESSURE``
journal marker.

The harness follows ``test_autodeploy_review_drain.py``: stub the box's binaries onto PATH and
run the REAL script, so these assertions bind the shipped bash rather than a reimplementation.
The tests drive the NO-OP path (``TARGET == DEPLOYED``) and control the reported disk percent
via a file-backed ``df`` stub.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

AUTODEPLOY = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "autodeploy.sh"
_SHA = "d" * 40  # deployed == target -> the no-op path


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


@pytest.fixture
def box(tmp_path: Path) -> dict[str, object]:
    """A fake box on the NO-OP path: the mirror tip equals the deployed sha, so autodeploy
    reaches the line-228 "up to date" exit. The reported root-disk percent is file-backed
    (``pct_file``) so a test can set it per tick; ``docker`` prune/compose calls and the
    ``df`` percent are both observable.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)  # so autodeploy skips the bootstrap clone

    cmd_log = tmp_path / "cmd-log"
    pct_file = tmp_path / "root-pct"
    pct_file.write_text("50")  # below threshold by default; a test raises it
    # When present, `pct_after_file` is what the disk reads AFTER a prune runs: the docker stub
    # copies it over `pct_file` on the builder-prune call, so a test can model a reclaim that
    # actually recovered the disk versus one that freed nothing (bug 9bc0). Absent = the disk
    # is unchanged by the prune, which is the incident's behaviour.
    pct_after_file = tmp_path / "root-pct-after"
    ts_file = state / "pressure-prune-ts"
    streak_file = state / "pressure-prune-streak"
    (state / "deployed-sha").write_text(_SHA + "\n")

    # git stub: rev-parse returns the deployed sha, so TARGET == DEPLOYED (no-op path).
    _stub(
        bin_dir,
        "git",
        f"""
        args=("$@"); sub=""
        for ((i=0; i<${{#args[@]}}; i++)); do
          case "${{args[i]}}" in -C) ((i++));; -*) ;; *) sub="${{args[i]}}"; break;; esac
        done
        case "$sub" in
          remote)    echo "https://github.com/navapbc/rebar.git"; exit 0 ;;
          fetch)     exit 0 ;;
          rev-parse) printf '%s\\n' "{_SHA}"; exit 0 ;;
          *)         exit 0 ;;
        esac
        """,
    )
    # docker stub: record prune + compose calls. Prune exits per DOCKER_PRUNE_RC (default 0).
    _stub(
        bin_dir,
        "docker",
        f"""
        rc="${{DOCKER_PRUNE_RC:-0}}"
        case "$*" in
          *"builder prune"*)
            echo "builder-prune" >> "{cmd_log}"
            [ -f "{pct_after_file}" ] && cp "{pct_after_file}" "{pct_file}"
            exit "$rc" ;;
          *"image prune"*)   echo "image-prune"   >> "{cmd_log}"; exit "$rc" ;;
          *"compose build"*) echo "compose-build" >> "{cmd_log}" ;;
          *"compose up"*)    echo "compose-up"    >> "{cmd_log}" ;;
        esac
        exit 0
        """,
    )
    # df stub: `df --output=pcent /` -> a header line + the file-backed percent, so
    # `df … | tail -1 | tr -dc '0-9'` yields the controlled number. `--output=avail /` is the
    # free-space probe prune_docker_caches measures itself with; it is derived from the same
    # file so a modelled reclaim moves BOTH readings coherently (a lower used-% means more
    # free kB), which is what makes the before/after log line meaningful here.
    _stub(
        bin_dir,
        "df",
        f"""
        pct="$(cat "{pct_file}")"
        case "$*" in
          *avail*) echo "Avail"; printf '%s\\n' "$(( (100 - pct) * 1000 ))"; exit 0 ;;
        esac
        echo "Use%"
        printf ' %s%%\\n' "$pct"
        exit 0
        """,
    )
    # flock/timeout are GNU/Linux-only; stub so the deploy runs on macOS runners too.
    _stub(bin_dir, "flock", "exit 0")
    _stub(bin_dir, "timeout", 'shift; exec "$@"')

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_DIR": str(state),
        "DEPLOY_REPO": str(deploy_repo),
        "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
        "MIRROR_DIR": str(mirror),
    }
    return {
        "env": env,
        "cmd_log": cmd_log,
        "state": state,
        "pct_file": pct_file,
        "pct_after_file": pct_after_file,
        "ts_file": ts_file,
        "streak_file": streak_file,
    }


def _set_pct(box: dict[str, object], pct: int, *, after: int | None = None) -> None:
    """Set the reported root-disk used percent. ``after`` is what it reads once a prune has
    run — omit it to model the incident (a reclaim that frees nothing)."""
    pct_file: Path = box["pct_file"]  # type: ignore[assignment]
    pct_file.write_text(str(pct))
    pct_after_file: Path = box["pct_after_file"]  # type: ignore[assignment]
    if after is None:
        pct_after_file.unlink(missing_ok=True)
    else:
        pct_after_file.write_text(str(after))


def _streak(box: dict[str, object]) -> int | None:
    """The persisted consecutive-ineffective-cycle count, or None if never written."""
    streak_file: Path = box["streak_file"]  # type: ignore[assignment]
    if not streak_file.exists():
        return None
    return int(streak_file.read_text().strip())


def _run(box: dict[str, object], **extra_env: str) -> subprocess.CompletedProcess[str]:
    """One timer tick."""
    env = dict(box["env"])  # type: ignore[arg-type]
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _commands(box: dict[str, object]) -> list[str]:
    cmd_log: Path = box["cmd_log"]  # type: ignore[assignment]
    return cmd_log.read_text().splitlines() if cmd_log.exists() else []


def _markers(result: subprocess.CompletedProcess[str], token: str) -> list[str]:
    journal = result.stdout + result.stderr
    return [ln for ln in journal.splitlines() if ln.startswith(token + " ")]


# --- HAPPY PATH: pressure prune fires at/above the threshold ------------------


def test_pressure_prune_fires_when_disk_at_or_above_threshold(box: dict[str, object]) -> None:
    """On the no-op tick, root disk >= DISK_PRESSURE_PCT triggers the same reclaim the deploy
    path uses (builder prune + image prune), and records the throttle timestamp.
    """
    _set_pct(box, 90)
    result = _run(box, DISK_PRESSURE_PCT="80")
    commands = _commands(box)
    ts_file: Path = box["ts_file"]  # type: ignore[assignment]
    context = f"rc={result.returncode}\ncommands={commands}\n{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, f"a pressure reclaim tick is a normal no-op exit\n{context}"
    assert "builder-prune" in commands, (
        f"root disk 90% >= 80% must run `docker builder prune`\n{context}"
    )
    assert "image-prune" in commands, (
        f"root disk 90% >= 80% must run `docker image prune`\n{context}"
    )
    assert "compose-build" not in commands and "compose-up" not in commands, (
        f"the no-op path must not redeploy the container\n{context}"
    )
    assert ts_file.exists(), (
        f"a pressure prune must record its timestamp so the throttle bounds the next one\n{context}"
    )


# --- EDGE: below the threshold, nothing is reclaimed (HELD OUT) ---------------


def test_no_prune_when_disk_below_threshold(box: dict[str, object]) -> None:
    """Below DISK_PRESSURE_PCT the no-op tick must stay a pure no-op: no prune, no marker, no
    throttle timestamp written."""
    _set_pct(box, 70)
    result = _run(box, DISK_PRESSURE_PCT="80")
    commands = _commands(box)
    ts_file: Path = box["ts_file"]  # type: ignore[assignment]
    context = f"rc={result.returncode}\ncommands={commands}\n{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, context
    assert "builder-prune" not in commands and "image-prune" not in commands, (
        f"root disk 70% < 80% must NOT prune\n{context}"
    )
    assert not _markers(result, "AUTODEPLOY_DISK_PRESSURE"), (
        f"no pressure marker below the threshold\n{context}"
    )
    assert not ts_file.exists(), f"no throttle timestamp is written when no prune ran\n{context}"


# --- EDGE: the throttle suppresses a second prune in the window (HELD OUT) ----


def test_throttle_suppresses_a_second_prune_within_the_interval(box: dict[str, object]) -> None:
    """A recent throttle timestamp must suppress a second pressure prune, even while the disk is
    still above the threshold, so a stuck-high disk does not prune every 2-min tick."""
    _set_pct(box, 90)
    ts_file: Path = box["ts_file"]  # type: ignore[assignment]
    ts_file.write_text(str(int(time.time())))  # pruned "just now"

    result = _run(box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="600")
    commands = _commands(box)
    context = f"rc={result.returncode}\ncommands={commands}\n{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, context
    assert "builder-prune" not in commands and "image-prune" not in commands, (
        f"a prune {0}s ago (< 600s throttle) must suppress this tick's prune\n{context}"
    )


# --- FAILURE: a prune failure never changes the exit code (HELD OUT) ----------


def test_prune_failure_is_non_fatal_and_still_marks(box: dict[str, object]) -> None:
    """A failing prune (docker exits non-zero) must only log — the no-op tick still exits 0 — and
    the countable pressure marker is still emitted so the reclaim attempt is visible."""
    _set_pct(box, 90)
    result = _run(box, DISK_PRESSURE_PCT="80", DOCKER_PRUNE_RC="1")
    context = f"rc={result.returncode}\n{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, (
        f"a prune failure on the pressure path must NOT change the exit code\n{context}"
    )
    assert len(_markers(result, "AUTODEPLOY_DISK_PRESSURE")) == 1, (
        f"the pressure prune attempt must emit exactly one countable marker\n{context}"
    )


# --- the marker fires exactly once on trigger, zero below threshold (HELD OUT) -


def test_pressure_marker_emitted_once_on_trigger(box: dict[str, object]) -> None:
    _set_pct(box, 88)
    triggered = _run(box, DISK_PRESSURE_PCT="80")
    assert len(_markers(triggered, "AUTODEPLOY_DISK_PRESSURE")) == 1, (
        f"exactly one AUTODEPLOY_DISK_PRESSURE marker per triggered prune\n"
        f"{triggered.stdout}\n{triggered.stderr}"
    )


# --- the reclaim MEASURES itself (bug 9bc0) -----------------------------------


def test_pressure_reclaim_logs_free_space_before_after_and_the_delta(
    box: dict[str, object],
) -> None:
    """`observability.sh` claims its marker lets an incident sweep distinguish "the reclaim gate
    never ran" from "it ran and reclaimed nothing". It cannot — it counts invocations. The log
    line is what makes that claim true: free space before, after, and the delta, every time."""
    _set_pct(box, 90, after=40)
    result = _run(box, DISK_PRESSURE_PCT="80")
    journal = result.stdout + result.stderr
    context = f"rc={result.returncode}\n{journal}"

    assert result.returncode == 0, context
    assert "before=10000kB after=60000kB freed=50000kB" in journal, (
        f"the reclaim must report free space before AND after AND the delta\n{context}"
    )
    assert "root disk 90% -> 40%" in journal, (
        f"the completion line must name the used-% it actually achieved, not just 'complete'\n"
        f"{context}"
    )


# --- the persistent-pressure streak counter -----------------------------------


def test_streak_increments_when_a_reclaim_leaves_the_disk_pressured(
    box: dict[str, object],
) -> None:
    """One completed reclaim after which the disk is STILL >= DISK_PRESSURE_PCT is one
    ineffective cycle. This is the incident: the reclaim ran and freed nothing."""
    _set_pct(box, 92)  # no `after` -> the prune changes nothing, exactly as in the outage
    result = _run(box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="0")
    context = f"rc={result.returncode}\nstreak={_streak(box)}\n{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, context
    assert _streak(box) == 1, (
        f"a reclaim that completed with the disk still pressured is ONE ineffective cycle\n"
        f"{context}"
    )


def test_streak_resets_to_zero_once_the_disk_falls_below_the_threshold(
    box: dict[str, object],
) -> None:
    """The reset must fire on the BELOW-THRESHOLD tick, which takes `reclaim_under_pressure`'s
    early return and never reaches the post-reclaim code. A reset written next to the increment
    would be unreachable here and the counter would latch high forever."""
    streak_file: Path = box["streak_file"]  # type: ignore[assignment]
    streak_file.write_text("2\n")
    _set_pct(box, 55)  # healthy: the tick early-returns before any prune

    result = _run(box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="0")
    commands = _commands(box)
    context = (
        f"rc={result.returncode}\nstreak={_streak(box)}\ncommands={commands}\n"
        f"{result.stdout}\n{result.stderr}"
    )

    assert result.returncode == 0, context
    assert "builder-prune" not in commands, f"a healthy tick must still not prune\n{context}"
    assert _streak(box) == 0, (
        f"a below-threshold tick must clear the streak even though it early-returns\n{context}"
    )


def test_a_reclaim_that_recovers_the_disk_clears_the_streak(box: dict[str, object]) -> None:
    """The other reset edge: the reclaim RAN and worked. The post-prune re-read is below the
    threshold, so the cycle was effective and the streak returns to zero."""
    streak_file: Path = box["streak_file"]  # type: ignore[assignment]
    streak_file.write_text("2\n")
    _set_pct(box, 92, after=57)  # the manual reclaim's real numbers: 92% -> 57%

    result = _run(box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="0")
    context = f"rc={result.returncode}\nstreak={_streak(box)}\n{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, context
    assert _streak(box) == 0, (
        f"an EFFECTIVE reclaim must clear the streak — the counter tracks ineffectiveness, "
        f"not pressure\n{context}"
    )
    assert not _markers(result, "AUTODEPLOY_DISK_PRESSURE_PERSISTS"), (
        f"a reclaim that worked must never emit the persistence marker\n{context}"
    )


def test_a_throttled_pressured_tick_leaves_the_streak_unchanged(box: dict[str, object]) -> None:
    """The counter counts reclaim CYCLES, not ticks. A throttled tick ran no reclaim, so it is
    evidence neither for nor against effectiveness — and counting ticks would reach the alarm
    inside a single 600s throttle window on the ~2-min timer, firing after ONE real reclaim."""
    streak_file: Path = box["streak_file"]  # type: ignore[assignment]
    streak_file.write_text("1\n")
    ts_file: Path = box["ts_file"]  # type: ignore[assignment]
    ts_file.write_text(str(int(time.time())))  # pruned "just now"
    _set_pct(box, 92)

    result = _run(box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="600")
    commands = _commands(box)
    context = (
        f"rc={result.returncode}\nstreak={_streak(box)}\ncommands={commands}\n"
        f"{result.stdout}\n{result.stderr}"
    )

    assert result.returncode == 0, context
    assert "builder-prune" not in commands, f"the throttle must suppress the prune\n{context}"
    assert _streak(box) == 1, (
        f"a throttled tick performed no reclaim cycle, so the streak must not move\n{context}"
    )
    assert not _markers(result, "AUTODEPLOY_DISK_PRESSURE_PERSISTS"), (
        f"a throttled tick must not emit the persistence marker\n{context}"
    )


# --- the discriminator: the marker fires on PERSISTENCE, not on pressure ------


def test_persistence_marker_fires_at_the_third_consecutive_ineffective_cycle(
    box: dict[str, object],
) -> None:
    """Three consecutive reclaims that each left the disk pressured — the actionable condition
    no existing signal expresses. The threshold alarm flaps with the disk and
    AUTODEPLOY_DISK_PRESSURE counts invocations, so neither can say "reclaim is ineffective"."""
    _set_pct(box, 92)
    results = [_run(box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="0") for _ in range(3)]
    streaks_context = f"streak={_streak(box)}"

    assert [r.returncode for r in results] == [0, 0, 0], streaks_context
    assert _streak(box) == 3, f"three ineffective cycles must count three\n{streaks_context}"
    assert not _markers(results[0], "AUTODEPLOY_DISK_PRESSURE_PERSISTS"), (
        f"cycle 1 is below PRESSURE_STREAK_ALARM\n{results[0].stdout}\n{results[0].stderr}"
    )
    assert not _markers(results[1], "AUTODEPLOY_DISK_PRESSURE_PERSISTS"), (
        f"cycle 2 is below PRESSURE_STREAK_ALARM\n{results[1].stdout}\n{results[1].stderr}"
    )
    persists = _markers(results[2], "AUTODEPLOY_DISK_PRESSURE_PERSISTS")
    assert len(persists) == 1, (
        f"cycle 3 must emit exactly one persistence marker\n{results[2].stdout}\n"
        f"{results[2].stderr}"
    )
    assert '"reason": "reclaim-ineffective"' in persists[0], persists[0]


def test_a_single_pressured_cycle_that_recovers_never_emits_the_marker(
    box: dict[str, object],
) -> None:
    """NEGATIVE CONTROL — the discriminator this whole counter exists for. One pressured cycle
    followed by relief must emit NOTHING: a marker that fires on any pressure is just the
    flapping `rebar-root-disk-pressure` threshold alarm we already have, which sat in ALARM for
    ~11h saying nothing actionable. Only PERSISTENCE may speak."""
    _set_pct(box, 92)
    pressured = _run(box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="0")
    assert _streak(box) == 1, f"the pressured cycle counts one\n{pressured.stderr}"

    _set_pct(box, 55)  # the disk recovered before the next tick
    relieved = _run(box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="0")

    context = (
        f"streak={_streak(box)}\npressured:\n{pressured.stdout}\n{pressured.stderr}\n"
        f"relieved:\n{relieved.stdout}\n{relieved.stderr}"
    )
    assert (pressured.returncode, relieved.returncode) == (0, 0), context
    assert not _markers(pressured, "AUTODEPLOY_DISK_PRESSURE_PERSISTS"), (
        f"ONE pressured cycle must not emit the persistence marker\n{context}"
    )
    assert not _markers(relieved, "AUTODEPLOY_DISK_PRESSURE_PERSISTS"), context
    assert _streak(box) == 0, f"relief must clear the streak\n{context}"


def test_the_persistence_marker_is_distinct_from_the_invocation_counter(
    box: dict[str, object],
) -> None:
    """The two markers must be separately countable. AUTODEPLOY_DISK_PRESSURE counts INVOCATIONS
    (it fires on every triggered prune, including the first); the persistence marker fires only
    at the alarm threshold. If they shared a token the "reclaim is ineffective" signal would be
    drowned by ordinary reclaims."""
    _set_pct(box, 92)
    first = _run(box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="0")
    assert len(_markers(first, "AUTODEPLOY_DISK_PRESSURE")) == 1, first.stderr
    assert not _markers(first, "AUTODEPLOY_DISK_PRESSURE_PERSISTS"), first.stderr

    _run(box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="0")
    third = _run(box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="0")

    context = f"{third.stdout}\n{third.stderr}"
    # `_markers` matches on "<token> " so the invocation counter's own token must not also
    # match the persistence line; the persistence token is a strict superstring of it.
    assert len(_markers(third, "AUTODEPLOY_DISK_PRESSURE")) == 1, (
        f"the invocation marker must still fire exactly once, uninflated by the persistence "
        f"marker sharing its record\n{context}"
    )
    assert len(_markers(third, "AUTODEPLOY_DISK_PRESSURE_PERSISTS")) == 1, context


def test_a_corrupt_streak_file_fails_safe_toward_silence(box: dict[str, object]) -> None:
    """The counter is read from disk across separate processes, so it can be absent, truncated,
    or garbage. A non-numeric read is 0 — a lost counter DELAYS the marker rather than firing a
    false 'reclaim is ineffective' page."""
    streak_file: Path = box["streak_file"]  # type: ignore[assignment]
    streak_file.write_text("not-a-number\n")
    _set_pct(box, 92)

    result = _run(box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="0")
    context = f"rc={result.returncode}\nstreak={_streak(box)}\n{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, context
    assert _streak(box) == 1, f"a corrupt counter restarts at zero, not at garbage\n{context}"
    assert not _markers(result, "AUTODEPLOY_DISK_PRESSURE_PERSISTS"), (
        f"a corrupt counter must never short-circuit straight to the alarm\n{context}"
    )


def test_the_alarm_threshold_is_configurable(box: dict[str, object]) -> None:
    """PRESSURE_STREAK_ALARM is a named tunable alongside DISK_PRESSURE_PCT and
    PRESSURE_PRUNE_MIN_INTERVAL, not a literal 3 buried in the reclaim."""
    _set_pct(box, 92)
    result = _run(
        box, DISK_PRESSURE_PCT="80", PRESSURE_PRUNE_MIN_INTERVAL="0", PRESSURE_STREAK_ALARM="1"
    )
    context = f"rc={result.returncode}\n{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, context
    assert len(_markers(result, "AUTODEPLOY_DISK_PRESSURE_PERSISTS")) == 1, (
        f"with the threshold lowered to 1, the FIRST ineffective cycle must alarm\n{context}"
    )
