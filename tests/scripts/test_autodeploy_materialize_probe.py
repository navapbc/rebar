"""autodeploy re-materializes the host observability probe on a probe-source change
(ticket dying-verastile-quelea).

The systemd timer runs ``/usr/local/bin/rebar-observability.sh`` — a COPY that only
``install-observability.sh`` ever writes. ``infra/scripts/`` is in no autodeploy trigger
path (not BOT_PATHS, not CONFIG_PATHS), so a change to the probe reached the box's
``/opt/rebar`` copy at best via rsync but NEVER refreshed the installed ``/usr/local/bin``
copy — the probe silently went stale for 10 days.

This test drives autodeploy.sh under a PATH shim where ``main`` has advanced and ``git
diff`` reports *only* ``infra/scripts/observability.sh`` changed (so the heavy review-bot
rebuild block is skipped). It asserts that autodeploy re-runs the idempotent
``install-observability.sh`` — proved by a marker the stubbed installer writes.
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
    """A fake box where main advanced and ONLY the observability probe source changed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)  # so autodeploy skips bootstrap clone
    (mirror / "infra" / "scripts").mkdir(parents=True)

    # The installer the box runs: a stub that records it was invoked (the materialize proof),
    # placed at the exact path autodeploy invokes ($MIRROR_DIR/infra/scripts/...).
    marker = tmp_path / "installer-ran"
    installer = mirror / "infra" / "scripts" / "install-observability.sh"
    installer.write_text(f'#!/usr/bin/env bash\necho ran > "{marker}"\nexit 0\n')
    installer.chmod(0o755)

    # Seed deployed-sha so this is neither first-run nor up-to-date.
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")

    # git stub: only `diff … observability.sh` reports a change; BOT/CONFIG diffs are empty.
    _stub(
        bin_dir,
        "git",
        f"""
        # find the subcommand (skip the leading -C <dir>)
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
            # transient object markers: report a change ONLY for the observability probe.
            case "$*" in *observability.sh*) echo "infra/scripts/observability.sh"; exit 0 ;; esac
            exit 0 ;;
          *) exit 0 ;;
        esac
        """,
    )
    # Nothing below the OBS path is reached, but stub the heavy tools defensively.
    for tool in ("docker", "curl", "rsync", "chown", "stat"):
        _stub(bin_dir, tool, "exit 0")

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
    return {"env": env, "marker": marker}


def test_autodeploy_rematerializes_probe_on_source_change(
    deploy_box: dict[str, object],
) -> None:
    env = deploy_box["env"]
    marker = deploy_box["marker"]
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=env,  # type: ignore[arg-type]
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert marker.exists(), (  # type: ignore[union-attr]
        "autodeploy must re-run install-observability.sh when the probe source changed, "
        f"so /usr/local/bin/rebar-observability.sh is refreshed. rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
