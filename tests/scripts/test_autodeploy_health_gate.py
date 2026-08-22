"""autodeploy's health gate must outlast the app's own startup budget, and must record the
failing container's stderr before it rolls back (bug 2ae9-5aac-1342-4118).

On 2026-07-31 the review-bot needed ~62s to become ready because ``run_ensures()`` burns the
store write-lock budget (``_DEFAULT_TIMEOUT`` x ``_DEFAULT_ATTEMPTS``) before giving up and
continuing. ``HEALTH_TIMEOUT`` defaulted to 30s — BELOW a budget the application may
legitimately spend — so the container was killed at +30s and rolled back to ``:prev`` seven
consecutive times, each rolled-back container then becoming healthy ~30s later.

The second half of the incident was undiagnosability: ``bot-unhealthy`` logged only "ROLLING
BACK to :prev". The decisive evidence (the missing ``ensure ...`` sweep lines and a 61s gap)
lived solely in the container's own stderr, and the rollback replaced that container
immediately — so the deploy journal could not distinguish "the image is broken" from "the
image is fine, just slow", which have opposite remediations.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

AUTODEPLOY = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "autodeploy.sh"
_DEPLOYED = "d" * 40
_TARGET = "e" * 40

#: Emitted by the stubbed ``docker compose logs`` — stands in for the real container stderr
#: that the incident could only recover by host access to an already-replaced container.
_BOT_STDERR_SENTINEL = "REVIEWBOT-STDERR-SENTINEL ensure sweep never logged"

#: The readiness loop's granularity: ``sleep 2`` between probes, each ``curl -m 3``. A
#: container that becomes ready at T can therefore be observed as late as T+5.
_HEALTH_POLL_GRANULARITY_SECONDS = 5


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


@pytest.fixture
def deploy_box(tmp_path: Path) -> dict[str, object]:
    """A fake box where main advanced with a review-bot source change and the new container
    NEVER passes its health check (curl always fails)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    (deploy_repo / "infra" / "scripts").mkdir(parents=True)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)  # so autodeploy skips the bootstrap clone

    (deploy_repo / "infra" / "compose" / ".env").write_text("PREEXISTING=1\n")
    # The rsync'd TARGET copy autodeploy invokes before `compose up`; succeed so the deploy
    # reaches the health gate (this test is about the gate, not about secrets).
    fs = deploy_repo / "infra" / "scripts" / "fetch-secrets.sh"
    fs.write_text("#!/usr/bin/env bash\nexit 0\n")
    fs.chmod(0o755)

    cmd_log = tmp_path / "cmd-log"
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")

    # git stub: report a change ONLY for a BOT_PATHS entry (src/rebar/), so the review-bot
    # rebuild+health block runs and the config/probe/certbot blocks stay skipped.
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
          rev-parse) echo "{_TARGET}"; exit 0 ;;
          checkout) exit 0 ;;
          diff)
            case "$*" in *src/rebar/*) echo "src/rebar/review_bot/app.py"; exit 0 ;; esac
            exit 0 ;;
          *) exit 0 ;;
        esac
        """,
    )
    # docker stub: log the ordered invocations the rollback contract depends on, and serve a
    # container log tail carrying the sentinel.
    _stub(
        bin_dir,
        "docker",
        f"""
        case "$*" in
          *"compose build"*)  echo "compose-build" >> "{cmd_log}" ;;
          *"compose up"*)     echo "compose-up" >> "{cmd_log}" ;;
          *"compose logs"*)   echo "compose-logs" >> "{cmd_log}"
                              echo "{_BOT_STDERR_SENTINEL}" ;;
          *"image inspect"*)  exit 0 ;;
          *tag*:latest*:prev*) echo "tag-save-prev" >> "{cmd_log}" ;;
          *tag*:prev*:latest*) echo "tag-rollback-latest" >> "{cmd_log}" ;;
        esac
        exit 0
        """,
    )
    # flock/timeout are GNU/Linux-only; stub so the deploy actually runs on macOS runners too.
    _stub(bin_dir, "flock", "exit 0")
    _stub(bin_dir, "timeout", 'shift; exec "$@"')  # `timeout <dur> cmd …` -> run cmd
    _stub(bin_dir, "curl", "exit 1")  # the health check NEVER passes
    for tool in ("rsync", "chown", "stat"):
        _stub(bin_dir, tool, "exit 0")

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_DIR": str(state),
        "DEPLOY_REPO": str(deploy_repo),
        "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
        "MIRROR_DIR": str(mirror),
        # Prove the default stays env-overridable AND keep this test fast: without it the
        # unhealthy path would burn the (deliberately large) default deadline.
        "HEALTH_TIMEOUT": "1",
    }
    return {"env": env, "cmd_log": cmd_log, "state": state}


def _run(env: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=env,  # type: ignore[arg-type]
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_bot_unhealthy_records_container_log_tail_in_the_deploy_journal(
    deploy_box: dict[str, object],
) -> None:
    """The failing container's own output must reach the deploy journal BEFORE the rollback
    replaces it — that is the only thing that separates "broken image" from "just slow"."""
    result = _run(deploy_box["env"])  # type: ignore[arg-type]
    journal = result.stdout + result.stderr
    cmd_log: Path = deploy_box["cmd_log"]  # type: ignore[assignment]
    lines = cmd_log.read_text().splitlines() if cmd_log.exists() else []
    context = f"rc={result.returncode}\nlog={lines}\njournal:\n{journal}"

    assert _BOT_STDERR_SENTINEL in journal, (
        "a bot-unhealthy rollback must capture a bounded tail of the failing container's own "
        "output into the deploy journal. Without it the container is replaced by the rollback "
        "and its stderr is recoverable only by host access to a container that no longer "
        f"exists, leaving the journal unable to diagnose the failure.\n{context}"
    )
    assert "compose-logs" in lines and lines.index("compose-logs") < lines.index(
        "tag-rollback-latest"
    ), (
        "the log tail must be captured BEFORE the rollback re-tags and restarts the service — "
        f"afterwards the failing container is gone.\n{context}"
    )


def test_captured_container_output_cannot_inflate_the_deploy_errors_alarm(
    deploy_box: dict[str, object], tmp_path: Path
) -> None:
    """The capture must not let container output forge the alarmed marker.

    ``observability.sh`` counts ``AUTODEPLOY_ERROR`` occurrences in THIS unit's journal and
    publishes the delta as the ``deploy_errors`` CloudWatch metric. Echoing captured container
    output verbatim would let a bot log line containing that token inflate the alarm.
    """
    env: dict[str, str] = deploy_box["env"]  # type: ignore[assignment]
    bin_dir = Path(env["PATH"].split(os.pathsep)[0])
    cmd_log: Path = deploy_box["cmd_log"]  # type: ignore[assignment]
    _stub(
        bin_dir,
        "docker",
        f"""
        case "$*" in
          *"compose build"*)  echo "compose-build" >> "{cmd_log}" ;;
          *"compose up"*)     echo "compose-up" >> "{cmd_log}" ;;
          *"compose logs"*)   echo "traceback: AUTODEPLOY_ERROR lookalike in bot output" ;;
          *"image inspect"*)  exit 0 ;;
          *tag*:prev*:latest*) echo "tag-rollback-latest" >> "{cmd_log}" ;;
        esac
        exit 0
        """,
    )

    result = _run(env)  # type: ignore[arg-type]
    journal = result.stdout + result.stderr

    # observability.sh counts LINES matching the token, so every line carrying it must be a
    # genuine marker this script emitted (they lead the line and carry a JSON reason).
    carriers = [ln for ln in journal.splitlines() if "AUTODEPLOY_ERROR" in ln]
    forged = [ln for ln in carriers if not re.match(r"^AUTODEPLOY_ERROR \{.*\"reason\":", ln)]
    assert carriers and not forged, (
        "only genuine markers may carry the AUTODEPLOY_ERROR token — captured container output "
        "must have it redacted, or any bot log line containing it inflates the deploy_errors "
        f"alarm.\nforged={forged}\njournal:\n{journal}"
    )
    assert "lookalike in bot output" in journal, (
        "redaction must neuter the token, not drop the diagnostic line"
    )


def test_bot_unhealthy_still_rolls_back_to_prev_failsafe(
    deploy_box: dict[str, object],
) -> None:
    """The fail-safe guarantee is unchanged: last-known-good is restored, the deploy is
    marked failed, and the deployed SHA does not advance."""
    result = _run(deploy_box["env"])  # type: ignore[arg-type]
    cmd_log: Path = deploy_box["cmd_log"]  # type: ignore[assignment]
    state: Path = deploy_box["state"]  # type: ignore[assignment]
    lines = cmd_log.read_text().splitlines() if cmd_log.exists() else []
    context = f"rc={result.returncode}\nlog={lines}\nstderr:\n{result.stderr}"

    match = re.search(r"^AUTODEPLOY_ERROR (\{.*\})$", result.stderr, re.MULTILINE)
    assert match, (
        f"an unhealthy review-bot must emit the alarmed AUTODEPLOY_ERROR marker\n{context}"
    )
    assert json.loads(match.group(1))["reason"] == "bot-unhealthy", context

    assert "tag-rollback-latest" in lines, (
        f"the :prev image must be re-tagged :latest so last-known-good is restored\n{context}"
    )
    assert lines.count("compose-up") >= 2, (
        f"the service must be restarted after the rollback re-tag\n{context}"
    )
    assert lines.index("tag-rollback-latest") < len(lines) - 1 and (
        "compose-up" in lines[lines.index("tag-rollback-latest") :]
    ), f"the restart must follow the rollback re-tag\n{context}"
    assert (state / "deploy-backoff").exists(), (
        f"a failed deploy must record a SHA-keyed backoff\n{context}"
    )
    assert (state / "deployed-sha").read_text().strip() == _DEPLOYED, (
        f"the deployed SHA must NOT advance past a failed deploy\n{context}"
    )


def test_health_timeout_default_outlasts_the_store_lock_budget() -> None:
    """The deploy's readiness deadline must sit ABOVE the application's own worst-case
    internal budget, not below it.

    ``run_ensures()`` (``src/rebar/_store/ensures.py``) takes the store write lock on the
    review-bot's startup path and, when the lock is contended, spends the full
    ``_DEFAULT_TIMEOUT x _DEFAULT_ATTEMPTS`` budget before logging and continuing — a wait the
    application is designed to spend. Reading those constants here (rather than restating a
    literal) keeps the two in lockstep: raising the lock budget fails this test until the
    deploy deadline follows.
    """
    from rebar._store import lock as _lock

    lock_budget = _lock._DEFAULT_TIMEOUT * _lock._DEFAULT_ATTEMPTS
    floor = lock_budget + _HEALTH_POLL_GRANULARITY_SECONDS

    match = re.search(r'HEALTH_TIMEOUT="\$\{HEALTH_TIMEOUT:-(\d+)\}"', AUTODEPLOY.read_text())
    assert match, "autodeploy.sh must define an env-overridable HEALTH_TIMEOUT default"
    default = int(match.group(1))

    assert default > floor, (
        f"HEALTH_TIMEOUT defaults to {default}s but the review-bot may legitimately spend "
        f"{lock_budget}s inside run_ensures()'s write-lock budget alone, and the readiness "
        f"loop needs up to {_HEALTH_POLL_GRANULARITY_SECONDS}s more to observe the result. A "
        f"deadline at or below {floor}s rolls back a container that was merely slow — which is "
        "what produced seven consecutive rollbacks on 2026-07-31."
    )
