"""autodeploy signals a pending manual apply when ``infra/compose/gerrit.config`` changes
(bug 1630-0279-85ba-4e15).

``compose-up.sh`` re-seeds ``infra/compose/gerrit.config`` into the Gerrit site etc dir,
but ONLY when compose-up runs — and this loop deliberately never touches the Gerrit
container (``BOT_SERVICE`` is documented "NEVER 'gerrit'"). Gerrit also reads
``gerrit.config`` once at injector-creation time, so applying a change means restarting
Gerrit, which is an operator judgement call on a live review gate.

Before the fix, ``infra/compose/gerrit.config`` was in NO trigger path at all: a change
rsynced to the box's ``/opt/rebar`` copy and then silently did nothing, with no signal —
the same drift class as the observability probe (dying-verastile-quelea) and the certbot
timer, one layer over. This test drives ``autodeploy.sh`` under a PATH shim where ``main``
has advanced and ``git diff`` reports *only* ``infra/compose/gerrit.config`` changed, and
asserts autodeploy emits ``AUTODEPLOY_ERROR`` with ``reason=config_manual`` so the pending
apply is visible rather than silent.
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


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


@pytest.fixture
def deploy_box(tmp_path: Path) -> dict[str, object]:
    """A fake box where main advanced and ONLY infra/compose/gerrit.config changed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)  # so autodeploy skips the bootstrap clone
    (mirror / "infra" / "compose").mkdir(parents=True)

    # Seed deployed-sha so this is neither first-run nor up-to-date.
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")

    # git stub: only the gerrit.config diff reports a change. The BOT_PATHS/SECRETS_PATHS/
    # OBS_PATHS/CERTBOT_PATHS diffs name other files, so they return empty and their heavy
    # blocks are skipped.
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
            case "$*" in *gerrit.config*) echo "infra/compose/gerrit.config"; exit 0 ;; esac
            exit 0 ;;
          *) exit 0 ;;
        esac
        """,
    )
    # flock/timeout are GNU/Linux-only and absent on macOS runners; without these stubs the
    # whole deploy short-circuits and the config block never runs.
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


def test_autodeploy_signals_config_manual_on_gerrit_config_change(
    deploy_box: dict[str, object],
) -> None:
    env = deploy_box["env"]
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=env,  # type: ignore[arg-type]
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    context = f"rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    match = re.search(r"^AUTODEPLOY_ERROR (\{.*\})$", result.stderr, re.MULTILINE)
    assert match, (
        "a gerrit.config change must emit an AUTODEPLOY_ERROR signal — otherwise it "
        "reaches /opt/rebar and silently never applies, since autodeploy never restarts "
        f"Gerrit.\n{context}"
    )
    payload = json.loads(match.group(1))
    assert payload["reason"] == "config_manual", (
        "the gerrit.config signal must use reason=config_manual (the detect-only class), "
        f"got {payload['reason']!r}.\n{context}"
    )
    assert "gerrit.config" in payload["detail"], (
        "the operator-facing detail must name gerrit.config so the pending manual apply "
        f"is actionable, got {payload['detail']!r}.\n{context}"
    )
