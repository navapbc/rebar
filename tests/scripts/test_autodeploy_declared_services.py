"""Autodeploy must not report a no-op while declared services are down."""

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


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


@pytest.fixture
def no_op_box_with_dead_review_bot(tmp_path: Path) -> dict[str, object]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    (deploy_repo / "infra" / "compose" / "docker-compose.yml").write_text("services: {}\n")
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")

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
          rev-parse) echo "{_DEPLOYED}"; exit 0 ;;
          *) exit 0 ;;
        esac
        """,
    )
    _stub(
        bin_dir,
        "docker",
        """
        case "$*" in
          *"compose ps -q gerrit"*) echo cid-gerrit; exit 0 ;;
          *"compose ps -q review-bot"*) echo cid-review; exit 0 ;;
          *"compose ps -q opcert"*) echo cid-opcert; exit 0 ;;
          *"inspect -f {{.State.Status}} cid-review"*) echo exited; exit 0 ;;
          *"inspect -f {{.State.Status}}"*) echo running; exit 0 ;;
          *" ps "*) exit 0 ;;
        esac
        exit 0
        """,
    )
    _stub(bin_dir, "flock", "exit 0")
    _stub(bin_dir, "timeout", 'shift; exec "$@"')
    _stub(bin_dir, "curl", "exit 0")

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_DIR": str(state),
        "DEPLOY_REPO": str(deploy_repo),
        "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
        "MIRROR_DIR": str(mirror),
    }
    return {"env": env}


def test_no_op_tick_reports_declared_compose_service_that_is_not_running(
    no_op_box_with_dead_review_bot: dict[str, object],
) -> None:
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=no_op_box_with_dead_review_bot["env"],  # type: ignore[arg-type]
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    journal = result.stdout + result.stderr
    match = re.search(r"^AUTODEPLOY_ERROR (\{.*\})$", result.stderr, re.MULTILINE)
    assert match, f"dead declared service must be alarmed\n{journal}"
    payload = json.loads(match.group(1))
    assert payload["reason"] == "declared-service-down"
    assert "review-bot" in payload["detail"]
    assert "up to date" not in journal
