"""A reviewbot-ensure-tickets.sh-only change must select the review-bot deploy path."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from _subprocess_env import subprocess_env

AUTODEPLOY = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "autodeploy.sh"
DEPLOYED = "d" * 40
TARGET = "e" * 40


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def test_ensure_script_only_change_selects_reviewbot_deploy(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "deployed-sha").write_text(DEPLOYED + "\n")
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    (deploy_repo / "infra" / "scripts").mkdir(parents=True)
    (deploy_repo / "infra" / "compose" / ".env").write_text("EXISTING=1\n")
    fetch_secrets = deploy_repo / "infra" / "scripts" / "fetch-secrets.sh"
    fetch_secrets.write_text("#!/bin/sh\nexit 0\n")
    fetch_secrets.chmod(0o755)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    command_log = tmp_path / "commands"

    _stub(
        bin_dir,
        "git",
        f"""
        args=("$@"); sub=""
        for ((i=0; i<${{#args[@]}}; i++)); do
          case "${{args[i]}}" in -C) ((i++));; -*) ;; *) sub="${{args[i]}}"; break;; esac
        done
        case "$sub" in
          remote) echo "https://github.com/navapbc/rebar.git" ;;
          fetch) exit 0 ;;
          rev-parse) echo "{TARGET}" ;;
          checkout) exit 0 ;;
          diff)
            case "$*" in
              *reviewbot-ensure-tickets.sh*) echo "infra/scripts/reviewbot-ensure-tickets.sh" ;;
            esac
            ;;
        esac
        exit 0
        """,
    )
    _stub(
        bin_dir,
        "docker",
        f"""
        case "$*" in *"compose up"*) echo compose-up >> "{command_log}" ;; esac
        exit 0
        """,
    )
    _stub(bin_dir, "flock", "exit 0")
    _stub(bin_dir, "timeout", 'shift; exec "$@"')
    _stub(bin_dir, "curl", "exit 0")
    for tool in ("rsync", "chown", "stat"):
        _stub(bin_dir, tool, "exit 0")

    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "STATE_DIR": str(state),
            "DEPLOY_REPO": str(deploy_repo),
            "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
            "MIRROR_DIR": str(mirror),
        }
    )
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)], env=env, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert command_log.exists(), (
        "a reviewbot-ensure-tickets.sh-only diff did not enter the review-bot deploy branch"
    )
    assert command_log.read_text().splitlines() == ["compose-up"]
