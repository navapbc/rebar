"""autodeploy signals a pending MANUAL apply when a gerrit-side detect-only source changes.

Two sources in this class, both DETECT-ONLY (``reason=config_manual``) because applying
either touches the Gerrit container — an operator judgement call on a live review gate that
this unattended loop deliberately never makes (``BOT_SERVICE`` is documented "NEVER 'gerrit'"):

* ``infra/compose/gerrit.config`` (bug 1630-0279-85ba-4e15) — ``compose-up.sh`` re-seeds it
  into the Gerrit site etc dir, but only when compose-up runs; Gerrit reads it once at
  injector-creation time, so applying means restarting Gerrit.
* ``infra/gerrit/materialize-deploy-key.sh`` (bug 408c-9c78-c523-4d1c) — the SSM->file
  materializer that installs the replication deploy key into the gerrit user's dir, a direct
  sibling of ``infra/gerrit/materialize-g2p-config.sh`` which was ALREADY in ``CONFIG_PATHS``.
  The deploy-key materializer was overlooked, so a merged change to it rsynced to
  ``/opt/rebar`` and silently did nothing — the same drift class ``CONFIG_PATHS`` detect-only
  exists to make visible.

Each was (before its fix) absent from EVERY autodeploy path manifest, so a merged change
reached ``/opt/rebar`` and then silently did nothing, with NO signal at all — the same drift
class as the observability probe and the certbot timer, one layer over. This test drives the
REAL ``autodeploy.sh`` under a PATH shim where ``main`` has advanced and ``git diff`` reports
*only* one such source changed, and asserts autodeploy emits ``AUTODEPLOY_ERROR`` with
``reason=config_manual`` naming that file, so the pending manual apply is visible rather than
silent — for each source in the class.
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

# Every gerrit-side detect-only source that must be signalled with reason=config_manual.
# The second tuple element is a substring the operator-facing `detail` must name, or None
# when the generic config_manual message names representative artifacts rather than this exact
# file (the fix is manifest-membership only — it adds no per-file wording).
_CONFIG_MANUAL_SOURCES = [
    ("infra/compose/gerrit.config", "gerrit.config"),
    ("infra/gerrit/materialize-deploy-key.sh", None),
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
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    (deploy_repo / "infra" / "gerrit").mkdir(parents=True)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)  # so autodeploy skips the bootstrap clone
    (mirror / "infra" / "compose").mkdir(parents=True)
    (mirror / "infra" / "gerrit").mkdir(parents=True)

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


@pytest.mark.parametrize(("changed_path", "expected_detail"), _CONFIG_MANUAL_SOURCES)
def test_autodeploy_signals_config_manual(
    deploy_box: dict[str, object], changed_path: str, expected_detail: str | None
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
        f"a change to the gerrit-side detect-only source {changed_path} must emit an "
        "AUTODEPLOY_ERROR signal — otherwise it reaches /opt/rebar and silently never "
        "applies, since autodeploy never restarts Gerrit.\n"
        f"{context}"
    )
    payload = json.loads(match.group(1))
    assert payload["reason"] == "config_manual", (
        "the signal must use reason=config_manual (the detect-only class), got "
        f"{payload['reason']!r}.\n{context}"
    )
    if expected_detail is not None:
        assert expected_detail in payload["detail"], (
            "the operator-facing detail must name the changed source so the pending manual "
            f"apply is actionable, got {payload['detail']!r}.\n{context}"
        )
    else:
        assert "MANUAL operator apply" in payload["detail"], (
            "the config_manual detail must direct the operator to a manual apply, got "
            f"{payload['detail']!r}.\n{context}"
        )
