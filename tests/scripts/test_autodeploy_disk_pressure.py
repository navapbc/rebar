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
    ts_file = state / "pressure-prune-ts"
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
          *"builder prune"*) echo "builder-prune" >> "{cmd_log}"; exit "$rc" ;;
          *"image prune"*)   echo "image-prune"   >> "{cmd_log}"; exit "$rc" ;;
          *"compose build"*) echo "compose-build" >> "{cmd_log}" ;;
          *"compose up"*)    echo "compose-up"    >> "{cmd_log}" ;;
        esac
        exit 0
        """,
    )
    # df stub: `df --output=pcent /` -> a header line + the file-backed percent, so
    # `df … | tail -1 | tr -dc '0-9'` yields the controlled number.
    _stub(
        bin_dir,
        "df",
        f"""
        echo "Use%"
        printf ' %s%%\\n' "$(cat "{pct_file}")"
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
        "ts_file": ts_file,
    }


def _set_pct(box: dict[str, object], pct: int) -> None:
    pct_file: Path = box["pct_file"]  # type: ignore[assignment]
    pct_file.write_text(str(pct))


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
