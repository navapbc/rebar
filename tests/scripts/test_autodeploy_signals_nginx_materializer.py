"""Signal manual application when host nginx materializer sources change.

Boot-time scripts write installed include files outside the compose build context.
Autodeploy does not rerun them, so changes to either materializer, the upstream
seed, or compose-up must emit nginx_materializer_manual naming the source.
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

# Every host-nginx materializer source + seed + orchestrator that must be signalled.
_MATERIALIZER_SOURCES = [
    "infra/scripts/materialize-opcert-guard.sh",
    "infra/scripts/materialize-mcp-upstream.sh",
    "infra/nginx/mcp-upstream.conf",
    "infra/scripts/compose-up.sh",
]


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


@pytest.fixture
def deploy_box(tmp_path: Path) -> dict[str, object]:
    """A fake box where main advanced; the changed source is injected per-test via CHANGED_PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "scripts").mkdir(parents=True)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)  # so autodeploy skips the bootstrap clone
    (mirror / "infra" / "scripts").mkdir(parents=True)

    # Seed deployed-sha so this is neither first-run nor up-to-date.
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")

    # git stub: the diff whose `--` pathspec names $CHANGED_PATH reports THAT path changed;
    # every other manifest's diff names other files, returns empty, and its block is skipped.
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
            case "$*" in
              *"$CHANGED_PATH"*) echo "$CHANGED_PATH"; exit 0 ;;
            esac
            exit 0 ;;
          *) exit 0 ;;
        esac
        """,
    )
    # flock/timeout are GNU/Linux-only and absent on macOS runners; without these stubs the
    # whole deploy short-circuits and the materializer block never runs.
    _stub(bin_dir, "flock", "exit 0")
    _stub(bin_dir, "timeout", 'shift; exec "$@"')
    for tool in ("docker", "curl", "rsync", "chown", "stat"):
        _stub(bin_dir, tool, "exit 0")

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_DIR": str(state),
        "DEPLOY_REPO": str(deploy_repo),
        "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
        "MIRROR_DIR": str(mirror),
    }
    return {"env": env}


@pytest.mark.parametrize("changed_path", _MATERIALIZER_SOURCES)
def test_autodeploy_signals_nginx_materializer_manual(
    deploy_box: dict[str, object], changed_path: str
) -> None:
    env = dict(deploy_box["env"])  # type: ignore[arg-type]
    env["CHANGED_PATH"] = changed_path
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    context = (
        f"changed={changed_path}\nrc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    match = re.search(r"^AUTODEPLOY_ERROR (\{.*\})$", result.stderr, re.MULTILINE)
    assert match, (
        f"a change to the host-nginx materializer source {changed_path} must emit an "
        "AUTODEPLOY_ERROR signal — otherwise it reaches /opt/rebar and silently never "
        "applies, since autodeploy never re-runs the compose-up materializers.\n"
        f"{context}"
    )
    payload = json.loads(match.group(1))
    assert payload["reason"] == "nginx_materializer_manual", (
        "the materializer signal must use reason=nginx_materializer_manual (the detect-only "
        f"class, parallel to config_manual/nginx_edge_manual), got {payload['reason']!r}.\n"
        f"{context}"
    )
    assert changed_path.split("/")[-1] in payload["detail"], (
        "the operator-facing detail must name the changed materializer file so the pending "
        f"manual re-materialize is actionable, got {payload['detail']!r}.\n{context}"
    )
