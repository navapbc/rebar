"""autodeploy signals a pending manual apply when ``infra/nginx/rebar.conf.template``
changes (bug 1d1b-a719-b675-4a1f).

``infra/nginx/rebar.conf.template`` is the source of truth for the nginx edge
(``/etc/nginx/conf.d/rebar.conf``) — it is the only file in ``infra/`` carrying
``proxy_redirect``. But it was absent from EVERY component path manifest in
``autodeploy.sh`` (BOT/SECRETS/MCP/CONFIG/OBS/CERTBOT), so a merged change to the edge
could never be detected, never applied, and — unlike a Gerrit config change, which is
DETECT-ONLY but at least emits ``AUTODEPLOY_ERROR reason=config_manual`` — got NO signal
at all: it failed silently. This is the exact drift class as the observability probe and
the certbot timer, one layer over, and the same detect-and-signal shape the gerrit.config
fix used (bug 1630-0279-85ba-4e15).

Scope is Option A (operator-approved): detect-and-signal ONLY. Full auto-apply (render →
``nginx -t`` → reload → rollback) is deferred to epic sprucing-wise-dikkops
(6d60-2d0c-6ff7-444b) and is NOT exercised here.

This test drives ``autodeploy.sh`` under a PATH shim where ``main`` has advanced and
``git diff`` reports *only* ``infra/nginx/rebar.conf.template`` changed, and asserts
autodeploy emits ``AUTODEPLOY_ERROR`` with ``reason=nginx_edge_manual`` so the pending
manual render+reload is visible rather than silent.
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
    """A fake box where main advanced and ONLY infra/nginx/rebar.conf.template changed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "nginx").mkdir(parents=True)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)  # so autodeploy skips the bootstrap clone
    (mirror / "infra" / "nginx").mkdir(parents=True)

    # Seed deployed-sha so this is neither first-run nor up-to-date.
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")

    # git stub: only the rebar.conf.template diff reports a change. The BOT_PATHS/
    # SECRETS_PATHS/MCP_PATHS/CONFIG_PATHS/OBS_PATHS/CERTBOT_PATHS diffs name other files,
    # so they return empty and their heavy blocks are skipped.
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
              *rebar.conf.template*) echo "infra/nginx/rebar.conf.template"; exit 0 ;;
            esac
            exit 0 ;;
          *) exit 0 ;;
        esac
        """,
    )
    # flock/timeout are GNU/Linux-only and absent on macOS runners; without these stubs the
    # whole deploy short-circuits and the edge block never runs.
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


def test_autodeploy_signals_nginx_edge_manual_on_template_change(
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
        "a rebar.conf.template change must emit an AUTODEPLOY_ERROR signal — otherwise it "
        "reaches /opt/rebar and silently never applies, since autodeploy never re-renders "
        f"the nginx edge.\n{context}"
    )
    payload = json.loads(match.group(1))
    assert payload["reason"] == "nginx_edge_manual", (
        "the nginx-edge signal must use reason=nginx_edge_manual (the detect-only class, "
        f"parallel to config_manual), got {payload['reason']!r}.\n{context}"
    )
    assert "rebar.conf.template" in payload["detail"], (
        "the operator-facing detail must name rebar.conf.template so the pending manual "
        f"render+reload is actionable, got {payload['detail']!r}.\n{context}"
    )
