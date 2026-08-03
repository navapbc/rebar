"""autodeploy must not recreate the review-bot container mid-review (bug 34cd).

``docker compose up -d`` STOPS the running container. uvicorn's shutdown drain covers only the
webhook QUEUE (``app.py`` waits on ``queue.join()``); the backfill reconciler's inline review is
cancelled outright — and the reconciler is the path that RETRIES a killed review. So a deploy
arriving mid-review kills it, and the retry is killed by the next deploy.

Measured on 2026-08-03: SEVEN recreations in 90 minutes (gaps of 18/7/22/4/15/20 min) against a
full review that takes ~10 MINUTES. Four of six gaps were shorter than one review, and changes
1302/1303 sat ``Verified +1`` with ``LLM-Review = 0`` for 20-35 minutes, unsubmittable.

The failure mode was INVISIBLE. A killed review fails nothing — the process was asked to stop —
so no ``VOTER_ERROR`` is emitted, ``restarts`` stays 0, the deploy logs "redeployed + healthy",
and all 11 CloudWatch alarms read OK while the gate was live-locked. Hence the marker assertions
here: the deferral and the interruption must each be COUNTABLE in the unit journal, and a routine
deferral must NOT carry the ``AUTODEPLOY_ERROR`` token that drives the deploy_errors alarm.

The harness is the one from ``test_autodeploy_health_gate.py``: stub the box's binaries onto PATH
and run the REAL script, so these assertions bind the shipped bash rather than a reimplementation.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

AUTODEPLOY = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "autodeploy.sh"
_DEPLOYED = "d" * 40


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


def _set_health_body(box: dict[str, object], body: str) -> None:
    """Point the ``curl`` stub at a new /health payload.

    One stub serves BOTH callers of ``HEALTH_URL``: the drain probe (which reads ``in_flight``)
    and the post-deploy readiness gate (which only needs a 200). A body with ``in_flight: 0`` is
    therefore an idle bot that also passes readiness.
    """
    health_file: Path = box["health_file"]  # type: ignore[assignment]
    health_file.write_text(body)


def _set_target(box: dict[str, object], sha: str) -> None:
    """Advance the mirror's ``origin/main`` tip, as a new commit landing would."""
    target_file: Path = box["target_file"]  # type: ignore[assignment]
    target_file.write_text(sha + "\n")


@pytest.fixture()
def box(tmp_path: Path) -> dict[str, object]:
    """A fake box where main advanced with a review-bot source change, so the redeploy block
    (and therefore the drain gate) is reached. The /health body and the target SHA are both
    file-backed so a test can change them BETWEEN ticks — that is what makes a burst testable.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    (deploy_repo / "infra" / "scripts").mkdir(parents=True)
    (deploy_repo / "infra" / "compose" / ".env").write_text("PREEXISTING=1\n")
    fs = deploy_repo / "infra" / "scripts" / "fetch-secrets.sh"
    fs.write_text("#!/usr/bin/env bash\nexit 0\n")
    fs.chmod(0o755)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)  # so autodeploy skips the bootstrap clone

    cmd_log = tmp_path / "cmd-log"
    target_file = tmp_path / "target-sha"
    health_file = tmp_path / "health-body"
    target_file.write_text("e" * 40 + "\n")
    health_file.write_text('{"status":"ok","in_flight":0}')
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")

    # git stub: the target tip is read from a FILE so a burst can advance it between ticks.
    # diff reports a BOT_PATHS entry only, so the bot block runs and config/probe/certbot
    # blocks stay skipped.
    _stub(
        bin_dir,
        "git",
        f"""
        args=("$@"); sub=""
        for ((i=0; i<${{#args[@]}}; i++)); do
          case "${{args[i]}}" in -C) ((i++));; -*) ;; *) sub="${{args[i]}}"; break;; esac
        done
        case "$sub" in
          remote) echo "https://github.com/navapbc/rebar.git"; exit 0 ;;
          fetch)  exit 0 ;;
          rev-parse) cat "{target_file}"; exit 0 ;;
          checkout) exit 0 ;;
          diff)
            case "$*" in *src/rebar/*) echo "src/rebar/review_bot/app.py"; exit 0 ;; esac
            exit 0 ;;
          *) exit 0 ;;
        esac
        """,
    )
    _stub(
        bin_dir,
        "docker",
        f"""
        case "$*" in
          *"compose build"*)  echo "compose-build" >> "{cmd_log}" ;;
          *"compose up"*)     echo "compose-up" >> "{cmd_log}" ;;
          *"compose logs"*)   echo "bot-log-tail" ;;
          *"image inspect"*)  exit 0 ;;
          *tag*:latest*:prev*) echo "tag-save-prev" >> "{cmd_log}" ;;
          *tag*:prev*:latest*) echo "tag-rollback-latest" >> "{cmd_log}" ;;
        esac
        exit 0
        """,
    )
    # curl stub: serves the file-backed /health body to BOTH the drain probe and the readiness
    # gate. Exit 22 (curl's HTTP-error code) when the body is the sentinel "DOWN".
    _stub(
        bin_dir,
        "curl",
        f"""
        body="$(cat "{health_file}")"
        [ "$body" = "DOWN" ] && exit 22
        printf '%s' "$body"
        exit 0
        """,
    )
    # flock/timeout are GNU/Linux-only; stub so the deploy runs on macOS runners too.
    _stub(bin_dir, "flock", "exit 0")
    _stub(bin_dir, "timeout", 'shift; exec "$@"')
    for tool in ("rsync", "chown", "stat"):
        _stub(bin_dir, tool, "exit 0")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.update(
        {
            "STATE_DIR": str(state),
            "DEPLOY_REPO": str(deploy_repo),
            "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
            "MIRROR_DIR": str(mirror),
            "HEALTH_TIMEOUT": "5",
        }
    )
    return {
        "env": env,
        "cmd_log": cmd_log,
        "state": state,
        "target_file": target_file,
        "health_file": health_file,
    }


def _run(box: dict[str, object]) -> subprocess.CompletedProcess[str]:
    """One timer tick."""
    return subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=box["env"],  # type: ignore[arg-type]
        capture_output=True,
        text=True,
        timeout=120,
    )


def _commands(box: dict[str, object]) -> list[str]:
    cmd_log: Path = box["cmd_log"]  # type: ignore[assignment]
    return cmd_log.read_text().splitlines() if cmd_log.exists() else []


def _markers(result: subprocess.CompletedProcess[str], token: str) -> list[str]:
    journal = result.stdout + result.stderr
    return [ln for ln in journal.splitlines() if ln.startswith(token + " ")]


# --- the busy case: defer instead of killing the review (AC1) -----------------


def test_a_deploy_that_would_interrupt_a_review_is_deferred(box: dict[str, object]) -> None:
    """The core defect. With a review in flight, the container must NOT be recreated."""
    _set_health_body(box, '{"status":"ok","in_flight":1}')
    result = _run(box)
    state: Path = box["state"]  # type: ignore[assignment]
    commands = _commands(box)
    context = f"rc={result.returncode}\ncommands={commands}\n{result.stdout}\n{result.stderr}"

    assert "compose-build" not in commands and "compose-up" not in commands, (
        "a deploy must NOT rebuild or recreate the container while a review is in flight — "
        f"`compose up -d` stops the container and KILLS the running review.\n{context}"
    )
    assert "tag-save-prev" not in commands, (
        f"nothing may be mutated on a deferred tick, not even the :prev rollback tag\n{context}"
    )
    assert (state / "deployed-sha").read_text().strip() == _DEPLOYED, (
        "a deferred deploy must NOT advance deployed-sha, or the box would record work it "
        f"never applied and never retry it.\n{context}"
    )
    assert result.returncode == 0, (
        f"a deferral is a normal outcome, not a unit failure (systemd would mark it "
        f"failed and trip the deploy alarms)\n{context}"
    )


def test_an_idle_bot_still_deploys_immediately(box: dict[str, object]) -> None:
    """The drain gate must not add latency when there is nothing to protect."""
    _set_health_body(box, '{"status":"ok","in_flight":0}')
    result = _run(box)
    state: Path = box["state"]  # type: ignore[assignment]
    commands = _commands(box)
    context = f"rc={result.returncode}\ncommands={commands}\n{result.stdout}\n{result.stderr}"

    assert "compose-build" in commands and "compose-up" in commands, (
        f"an idle bot must be redeployed on the very first tick\n{context}"
    )
    assert (state / "deployed-sha").read_text().strip() == "e" * 40, (
        f"a successful deploy must advance deployed-sha\n{context}"
    )
    assert not _markers(result, "AUTODEPLOY_DEFERRED"), (
        f"an idle bot must not be reported as deferred\n{context}"
    )
    assert not _markers(result, "AUTODEPLOY_REVIEW_INTERRUPT"), (
        f"deploying into an idle bot interrupts nothing\n{context}"
    )


# --- the deferral must be visible (AC3) --------------------------------------


def test_a_deferred_deploy_is_logged_and_countable(box: dict[str, object]) -> None:
    """A skipped cycle must never be mistakable for "nothing landed"."""
    _set_health_body(box, '{"status":"ok","in_flight":2}')
    result = _run(box)
    journal = result.stdout + result.stderr
    deferred = _markers(result, "AUTODEPLOY_DEFERRED")

    assert deferred, (
        "a deferral must emit the countable AUTODEPLOY_DEFERRED marker — observability.sh "
        "publishes its per-interval delta as rebar/host:deploy_deferrals, and without it a "
        f"skipped cycle is indistinguishable from no change having landed.\njournal:\n{journal}"
    )
    assert re.match(r'^AUTODEPLOY_DEFERRED \{.*"reason": ?"review-in-flight"', deferred[0]), (
        f"the marker must carry a machine-readable reason\n{deferred}"
    )
    assert "in_flight=2" in deferred[0] and "bound=" in deferred[0], (
        f"the marker must state what it is waiting on and the bound it is waiting under\n{deferred}"
    )
    assert "DEFERRING" in journal, (
        f"the deferral must also be human-readable in the journal\n{journal}"
    )


def test_a_deferral_does_not_inflate_the_deploy_errors_alarm(box: dict[str, object]) -> None:
    """A deferral is HEALTHY behaviour — expected on every landing burst.

    ``observability.sh`` counts ``AUTODEPLOY_ERROR`` lines in this unit's journal and publishes
    the delta as rebar/host:deploy_errors, which pages. Folding a routine deferral into that
    token would page an operator every time main advanced while the bot was working.
    """
    _set_health_body(box, '{"status":"ok","in_flight":1}')
    result = _run(box)
    journal = result.stdout + result.stderr
    carriers = [ln for ln in journal.splitlines() if "AUTODEPLOY_ERROR" in ln]

    assert not carriers, (
        "a deferral must not emit the AUTODEPLOY_ERROR token — it drives the paging "
        f"deploy_errors alarm.\ncarriers={carriers}\njournal:\n{journal}"
    )


# --- the bound (AC2) ---------------------------------------------------------


def test_the_deferral_is_bounded_and_the_interruption_is_countable(
    box: dict[str, object],
) -> None:
    """A permanently-busy bot must not block deploys forever — and when the bound is spent
    and a review IS killed, that must be observable rather than silent (AC4)."""
    _set_health_body(box, '{"status":"ok","in_flight":1}')
    env: dict[str, str] = box["env"]  # type: ignore[assignment]
    env["DEPLOY_DEFER_MAX"] = "0"  # bound already spent on the first tick

    result = _run(box)
    commands = _commands(box)
    interrupts = _markers(result, "AUTODEPLOY_REVIEW_INTERRUPT")
    context = f"rc={result.returncode}\ncommands={commands}\n{result.stdout}\n{result.stderr}"

    assert "compose-build" in commands and "compose-up" in commands, (
        f"once the bound is spent the deploy must proceed — a permanently-busy bot cannot be "
        f"allowed to freeze deploys indefinitely\n{context}"
    )
    assert interrupts, (
        "recreating the container over a live review must emit the countable "
        "AUTODEPLOY_REVIEW_INTERRUPT marker. This is the criterion the bug exists for: a "
        "killed review fails nothing, so without an explicit marker the interruption is "
        f"invisible to every alarm the box has.\n{context}"
    )
    assert '"reason": "bound-exceeded"' in interrupts[0] or (
        '"reason":"bound-exceeded"' in interrupts[0]
    ), f"the marker must distinguish a spent bound from a broken signal\n{interrupts}"


def test_the_bound_is_not_reset_by_a_new_commit_landing(box: dict[str, object]) -> None:
    """The bound is keyed to the deferral EPISODE, not to the target SHA.

    This is the subtle way the bound can be defeated. ``BACKOFF_FILE`` is deliberately keyed to
    the target SHA so a new tip retries promptly (fix-forward). If the DEFERRAL timer copied
    that, it would reset on almost every tick of a landing burst — TARGET advances each time —
    and a permanently-busy bot would defer forever, which is precisely the unbounded wait the
    bound exists to prevent.
    """
    _set_health_body(box, '{"status":"ok","in_flight":1}')
    env: dict[str, str] = box["env"]  # type: ignore[assignment]
    env["DEPLOY_DEFER_MAX"] = "2"

    _set_target(box, "a" * 40)
    first = _run(box)
    assert _markers(first, "AUTODEPLOY_DEFERRED"), (
        f"tick 1 should defer (bound not yet spent)\n{first.stdout}\n{first.stderr}"
    )

    time.sleep(3)  # let the 2s bound elapse

    _set_target(box, "b" * 40)  # a NEW commit lands, as during a burst
    second = _run(box)
    commands = _commands(box)
    context = f"commands={commands}\n{second.stdout}\n{second.stderr}"

    assert _markers(second, "AUTODEPLOY_REVIEW_INTERRUPT"), (
        "the elapsed bound must still fire even though the target SHA changed — otherwise a "
        f"burst of new commits resets the timer forever and the bound is unreachable\n{context}"
    )
    assert "compose-build" in commands, f"and the deploy must actually proceed\n{context}"


def test_a_stale_deferral_episode_cannot_kill_a_later_review(box: dict[str, object]) -> None:
    """An episode that ended must not be carried forward.

    If the episode marker survived a completed deploy, the NEXT busy tick would compute a huge
    elapsed time, judge the bound already spent, and kill a review on its FIRST tick — turning
    the fix into a slower version of the bug.
    """
    env: dict[str, str] = box["env"]  # type: ignore[assignment]
    env["DEPLOY_DEFER_MAX"] = "600"
    state: Path = box["state"]  # type: ignore[assignment]

    # Tick 1: busy -> defer, which opens an episode.
    _set_health_body(box, '{"status":"ok","in_flight":1}')
    _run(box)
    assert (state / "deploy-defer").exists(), "tick 1 should have opened a deferral episode"

    # Tick 2: idle -> deploy. The episode is over.
    _set_health_body(box, '{"status":"ok","in_flight":0}')
    _run(box)
    assert not (state / "deploy-defer").exists(), (
        "a completed deploy must clear the deferral episode, or the next busy tick inherits a "
        "spent bound and interrupts a review immediately"
    )

    # Tick 3: a new commit lands and the bot is busy again -> must DEFER, not interrupt.
    _set_target(box, "f" * 40)
    _set_health_body(box, '{"status":"ok","in_flight":1}')
    third = _run(box)
    context = f"{third.stdout}\n{third.stderr}"
    assert _markers(third, "AUTODEPLOY_DEFERRED"), (
        f"a fresh episode must start deferring from zero\n{context}"
    )
    assert not _markers(third, "AUTODEPLOY_REVIEW_INTERRUPT"), (
        f"a fresh episode must not inherit a spent bound\n{context}"
    )


def test_up_to_date_tick_clears_a_stale_deferral_episode(box: dict[str, object]) -> None:
    """The other way an episode ends: the pending deploy stops being pending (e.g. main is
    reverted to the deployed tip), so no deploy is outstanding to protect a review from."""
    env: dict[str, str] = box["env"]  # type: ignore[assignment]
    env["DEPLOY_DEFER_MAX"] = "600"
    state: Path = box["state"]  # type: ignore[assignment]

    _set_health_body(box, '{"status":"ok","in_flight":1}')
    _run(box)
    assert (state / "deploy-defer").exists()

    _set_target(box, _DEPLOYED)  # nothing left to deploy
    result = _run(box)
    assert not (state / "deploy-defer").exists(), (
        "an up-to-date tick must clear the episode — otherwise it lingers indefinitely and the "
        f"next real deploy inherits a spent bound\n{result.stdout}\n{result.stderr}"
    )


def test_defer_bound_default_outlasts_the_apps_own_per_review_cap() -> None:
    """The bound must sit ABOVE the longest a single review can possibly take.

    The review-bot wraps every review in ``asyncio.wait_for(..., REVIEW_TIMEOUT_SECONDS)`` on
    BOTH paths (the queue worker and the backfill reconciler), so in-flight necessarily returns
    to 0 within that window of the last review starting. A bound below it would force-deploy
    through a review that was about to finish — all of the interruption, none of the protection.
    Reading the constant here rather than restating a literal keeps the two in lockstep: raising
    the app's per-review cap fails this test until the deploy bound follows.
    """
    from rebar.review_bot.config import DEFAULT_REVIEW_TIMEOUT_SECONDS

    source = AUTODEPLOY.read_text()
    match = re.search(r'^DEPLOY_DEFER_MAX="\$\{DEPLOY_DEFER_MAX:-(\d+)\}"', source, re.MULTILINE)
    assert match, "autodeploy.sh must define a DEPLOY_DEFER_MAX default"
    bound = int(match.group(1))

    assert bound >= DEFAULT_REVIEW_TIMEOUT_SECONDS, (
        f"DEPLOY_DEFER_MAX default ({bound}s) must be >= the review-bot's own per-review cap "
        f"REVIEW_TIMEOUT_SECONDS ({DEFAULT_REVIEW_TIMEOUT_SECONDS}s). Below it, the bound "
        "expires while an ordinary review is still legitimately running and the deploy kills it."
    )


# --- the burst (AC5) --------------------------------------------------------


def test_a_landing_burst_ends_with_the_review_surviving_and_one_deploy(
    box: dict[str, object],
) -> None:
    """The measured scenario: commits arriving FASTER than one review completes.

    On 2026-08-03 seven recreations landed in 90 minutes — gaps of 18/7/22/4/15/20 minutes —
    against a ~10-minute review, so four of six gaps were shorter than one review and the same
    review could be killed again and again. This simulates that burst: six ticks arrive while
    one review runs (the tip advancing each time, as real commits would), then the review
    finishes.

    The contract: ZERO recreations while the review is in flight, and then exactly ONE deploy —
    at the NEWEST tip, not six deploys walking the burst. That second half is the coalescing
    that falls out of deferring for free: TARGET is recomputed every tick, so a run of deferred
    ticks collapses to one deploy.
    """
    env: dict[str, str] = box["env"]  # type: ignore[assignment]
    env["DEPLOY_DEFER_MAX"] = "3600"  # longer than the burst, so the bound never fires
    state: Path = box["state"]  # type: ignore[assignment]

    _set_health_body(box, '{"status":"ok","in_flight":1}')  # one long review, all burst long
    burst_tips = [f"{n:040x}" for n in range(1, 7)]
    for tip in burst_tips:
        _set_target(box, tip)
        result = _run(box)
        assert _markers(result, "AUTODEPLOY_DEFERRED"), (
            f"every tick of the burst must defer while the review runs (tip={tip})\n"
            f"{result.stdout}\n{result.stderr}"
        )

    mid_burst_commands = _commands(box)
    assert "compose-build" not in mid_burst_commands and "compose-up" not in mid_burst_commands, (
        "the review must survive the WHOLE burst. This is the live-lock: each recreation kills "
        "the review, the reconciler retries, the next commit kills the retry, and the "
        f"LLM-Review gate never votes.\ncommands={mid_burst_commands}"
    )
    assert not _markers(result, "AUTODEPLOY_REVIEW_INTERRUPT"), (
        "no review may be reported killed during the burst"
    )

    # The review finishes and votes; the next tick sees an idle bot.
    _set_health_body(box, '{"status":"ok","in_flight":0}')
    final = _run(box)
    commands = _commands(box)
    context = f"commands={commands}\n{final.stdout}\n{final.stderr}"

    assert commands.count("compose-build") == 1, (
        f"the burst must coalesce into ONE deploy, not one per commit\n{context}"
    )
    assert (state / "deployed-sha").read_text().strip() == burst_tips[-1], (
        f"and that deploy must land the NEWEST tip of the burst\n{context}"
    )


# --- the signal itself must not fail silently (AC4) -------------------------


def test_an_unreadable_inflight_signal_deploys_but_is_counted(box: dict[str, object]) -> None:
    """Fail OPEN on a broken signal, but never SILENTLY.

    A bot that cannot answer /health is likely broken, and deploying is how a broken bot gets
    fixed — blocking deploys on an unparseable field would invent a new way to freeze the gate.
    But an unnoticed signal regression (renamed field, wedged bot) puts the box straight back
    into the original blind state, so it must be counted.
    """
    _set_health_body(box, "not-json-at-all")
    result = _run(box)
    commands = _commands(box)
    interrupts = _markers(result, "AUTODEPLOY_REVIEW_INTERRUPT")
    context = f"commands={commands}\n{result.stdout}\n{result.stderr}"

    assert "compose-build" in commands, (
        f"an unreadable in-flight signal must not block the deploy (fail-open)\n{context}"
    )
    assert interrupts, (
        "a deploy that ran WITHOUT a usable drain check must be counted — otherwise a silently "
        f"broken probe restores the exact blindness this bug is about\n{context}"
    )
    assert "signal-unavailable" in interrupts[0], (
        f"the reason must distinguish a broken signal from a spent bound: the remediations "
        f"differ (fix the probe vs. investigate a chronically busy bot)\n{interrupts}"
    )


def test_a_missing_inflight_field_is_treated_as_unknown_not_as_idle(
    box: dict[str, object],
) -> None:
    """An older bot image (or a renamed field) answers /health without ``in_flight``.

    Reading that as "idle" would deploy blind while REPORTING that a drain check had passed —
    the most dangerous outcome, because it looks safe. It must be the counted unknown instead.
    """
    _set_health_body(box, '{"status":"ok"}')
    result = _run(box)
    interrupts = _markers(result, "AUTODEPLOY_REVIEW_INTERRUPT")
    context = f"{result.stdout}\n{result.stderr}"

    assert interrupts and "signal-unavailable" in interrupts[0], (
        f"a /health payload with no in_flight field must count as UNKNOWN, not as idle\n{context}"
    )


def test_an_unreachable_bot_is_unknown_and_does_not_block_the_deploy(
    box: dict[str, object],
) -> None:
    """A down bot must still be redeployable — that is the recovery path."""
    _set_health_body(box, "DOWN")  # curl exits non-zero for both probe and readiness
    result = _run(box)
    commands = _commands(box)
    context = f"commands={commands}\n{result.stdout}\n{result.stderr}"

    assert not _markers(result, "AUTODEPLOY_DEFERRED"), (
        f"an unreachable bot must never be mistaken for a busy one — that would make a dead "
        f"bot un-redeployable, freezing the gate for the full bound\n{context}"
    )
    assert "compose-build" in commands, (
        f"the deploy must proceed so a broken bot can be replaced\n{context}"
    )
