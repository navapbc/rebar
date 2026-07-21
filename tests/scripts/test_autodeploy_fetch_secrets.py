"""autodeploy re-runs fetch-secrets.sh before `docker compose up` so new/rotated
SSM-backed env keys reach the review-bot container on deploy without a manual boot
(ticket f600 / incident 2731 AC2).

The container .env is SSM-sourced (``fetch-secrets.sh``) and rsync-EXCLUDED, so a new
env key added via ``fetch-secrets.sh``/``ssm.tf`` would never reach the box on deploy —
those paths are NOT in ``BOT_PATHS``. autodeploy therefore triggers the review-bot
redeploy on ``BOT_PATHS`` OR ``SECRETS_PATHS`` and re-runs ``fetch-secrets.sh`` right
before ``compose up``.

These tests drive autodeploy.sh under a PATH shim where ``main`` advanced and ``git
diff`` reports only ``infra/scripts/fetch-secrets.sh`` changed (a secrets-only deploy).
They assert (1) fetch-secrets runs, and runs BEFORE ``compose up`` (ordering proof via a
shared command log); and (2) a fetch-secrets FAILURE aborts the deploy fail-safe — no
build/up, a backoff recorded — so the running bot is never left on a stale/half-written
secrets file.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

AUTODEPLOY = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "autodeploy.sh"
_DEPLOYED = "d" * 40
_TARGET = "e" * 40


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


@pytest.fixture()
def deploy_box(tmp_path: Path) -> dict[str, object]:
    """A fake box where main advanced and ONLY a secrets source (fetch-secrets.sh) changed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    (deploy_repo / "infra" / "scripts").mkdir(parents=True)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)  # so autodeploy skips bootstrap clone

    # A pre-existing .env whose content must survive a fetch-secrets FAILURE untouched.
    env_file = deploy_repo / "infra" / "compose" / ".env"
    env_file.write_text("PREEXISTING=1\n")

    # Shared command log: stubs append a line as they run, so tests can assert ordering.
    cmd_log = tmp_path / "cmd-log"

    # git stub: report a change ONLY for fetch-secrets.sh (a SECRETS_PATHS, secrets-only deploy).
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
            case "$*" in *fetch-secrets.sh*) echo "infra/scripts/fetch-secrets.sh"; exit 0 ;; esac
            exit 0 ;;
          *) exit 0 ;;
        esac
        """,
    )
    # docker stub: log every `compose <subcmd>` invocation so we can assert build/up order.
    _stub(
        bin_dir,
        "docker",
        f"""
        case "$*" in
          *"compose build"*) echo "compose-build" >> "{cmd_log}" ;;
          *"compose up"*)     echo "compose-up" >> "{cmd_log}" ;;
        esac
        exit 0
        """,
    )
    # flock/timeout are GNU/Linux-only; stub so the deploy actually runs on macOS runners too.
    _stub(bin_dir, "flock", "exit 0")
    _stub(bin_dir, "timeout", 'shift; exec "$@"')  # `timeout <dur> cmd …` -> run cmd
    _stub(bin_dir, "curl", "exit 0")  # health check passes
    for tool in ("rsync", "chown", "stat"):
        _stub(bin_dir, tool, "exit 0")

    # Seed deployed-sha so this is neither first-run nor up-to-date.
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.update(
        {
            "STATE_DIR": str(state),
            "DEPLOY_REPO": str(deploy_repo),
            "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
            "MIRROR_DIR": str(mirror),
        }
    )
    return {
        "env": env,
        "cmd_log": cmd_log,
        "env_file": env_file,
        "deploy_repo": deploy_repo,
        "state": state,
    }


def _write_fetch_secrets(deploy_repo: Path, marker: Path, exit_code: int) -> None:
    """Place the fetch-secrets.sh autodeploy will invoke (the rsync'd TARGET copy) as a stub
    that records it ran (marker) then exits with ``exit_code``."""
    fs = deploy_repo / "infra" / "scripts" / "fetch-secrets.sh"
    fs.write_text(f'#!/usr/bin/env bash\necho fetch-secrets >> "{marker}"\nexit {exit_code}\n')
    fs.chmod(0o755)


def _run(env: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=env,  # type: ignore[arg-type]
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_fetch_secrets_runs_before_compose_up_on_secrets_change(
    deploy_box: dict[str, object],
) -> None:
    cmd_log: Path = deploy_box["cmd_log"]  # type: ignore[assignment]
    _write_fetch_secrets(deploy_box["deploy_repo"], cmd_log, exit_code=0)  # type: ignore[arg-type]

    result = _run(deploy_box["env"])  # type: ignore[arg-type]
    lines = cmd_log.read_text().splitlines() if cmd_log.exists() else []

    assert "fetch-secrets" in lines, (
        "autodeploy must re-run fetch-secrets.sh when a SECRETS_PATHS source changed "
        f"(secrets-only deploy). rc={result.returncode}\nlog={lines}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "compose-up" in lines, (
        "the review-bot must still be (re)deployed on a secrets-only change"
    )
    assert lines.index("fetch-secrets") < lines.index("compose-up"), (
        "fetch-secrets.sh must run BEFORE `docker compose up` so the container starts with the "
        f"refreshed .env. order={lines}"
    )


def test_fetch_secrets_failure_aborts_deploy_failsafe(
    deploy_box: dict[str, object],
) -> None:
    cmd_log: Path = deploy_box["cmd_log"]  # type: ignore[assignment]
    env_file: Path = deploy_box["env_file"]  # type: ignore[assignment]
    state: Path = deploy_box["state"]  # type: ignore[assignment]
    _write_fetch_secrets(deploy_box["deploy_repo"], cmd_log, exit_code=1)  # type: ignore[arg-type]

    result = _run(deploy_box["env"])  # type: ignore[arg-type]
    lines = cmd_log.read_text().splitlines() if cmd_log.exists() else []

    assert "fetch-secrets" in lines, "fetch-secrets.sh must have been attempted"
    assert "compose-build" not in lines and "compose-up" not in lines, (
        "a fetch-secrets FAILURE must abort BEFORE building/starting the container — never "
        f"deploy against a stale/half-written secrets file. log={lines}\nstderr:\n{result.stderr}"
    )
    assert env_file.read_text() == "PREEXISTING=1\n", (
        "the existing .env must be left intact on failure"
    )
    assert (state / "deploy-backoff").exists(), (
        "a fetch-secrets failure must record a backoff (fail-safe: retried later, "
        "running bot untouched)"
    )
