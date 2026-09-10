"""autodeploy re-materializes the host certbot renew timer on an installer-source change
(ticket c593-f058-c488-406c).

The systemd timer runs ``certbot renew`` from unit files at
``/etc/systemd/system/certbot-renew.{service,timer}`` — files that only
``install-certbot-timer.sh`` ever writes. ``infra/scripts/`` is in no autodeploy trigger
path (not BOT_PATHS, not CONFIG_PATHS), so a change to the installer reached the box's
``/opt/rebar`` copy at best via rsync but NEVER refreshed the installed units — the same
drift class the observability sibling fixed (commit ffcf2c662 / ticket 1d63).

This test drives autodeploy.sh under a PATH shim where ``main`` has advanced and ``git
diff`` reports *only* ``infra/scripts/install-certbot-timer.sh`` changed (so the heavy
review-bot rebuild block is skipped). It asserts autodeploy re-runs the idempotent
``install-certbot-timer.sh`` — proved by a marker the stubbed installer writes — and that
an unrelated-only diff does NOT trigger the certbot block.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from _subprocess_env import subprocess_env

AUTODEPLOY = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "autodeploy.sh"
_DEPLOYED = "d" * 40
_TARGET = "e" * 40


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


def _make_box(tmp_path: Path, diff_pathspec: str) -> dict[str, object]:
    """A fake box where main advanced; the git stub reports a change only when the
    diff pathspec contains ``diff_pathspec`` (e.g. ``install-certbot-timer.sh`` for the
    positive case, or ``README.md`` for the unrelated negative case)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)  # so autodeploy skips bootstrap clone
    (mirror / "infra" / "scripts").mkdir(parents=True)

    # The installer the box runs: a stub that records it was invoked (the materialize
    # proof), placed at the exact path autodeploy invokes ($MIRROR_DIR/infra/scripts/...).
    marker = tmp_path / "installer-ran"
    installer = mirror / "infra" / "scripts" / "install-certbot-timer.sh"
    installer.write_text(f'#!/usr/bin/env bash\necho ran > "{marker}"\nexit 0\n')
    installer.chmod(0o755)

    # Seed deployed-sha so this is neither first-run nor up-to-date.
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")

    # git stub: only `diff … <diff_pathspec>` reports a change; BOT/CONFIG diffs are empty.
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
            # report a change ONLY for the configured pathspec of this box.
            case "$*" in *{diff_pathspec}*) echo "infra/scripts/{diff_pathspec}"; exit 0 ;; esac
            exit 0 ;;
          *) exit 0 ;;
        esac
        """,
    )
    # flock is the singleton guard (`flock -n 9 || exit 0`); it is a Linux-only tool
    # absent on macOS/BSD runners, so stub it to "lock acquired" — otherwise the whole
    # deploy is skipped and the certbot block never runs. timeout is likewise GNU-only.
    _stub(bin_dir, "flock", "exit 0")
    _stub(bin_dir, "timeout", 'shift; exec "$@"')  # `timeout <dur> cmd …` -> run cmd
    # Nothing below the CERTBOT path is reached, but stub the heavy tools defensively.
    for tool in ("docker", "curl", "rsync", "chown", "stat"):
        _stub(bin_dir, tool, "exit 0")

    env = subprocess_env()
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


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=env,  # type: ignore[arg-type]
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_autodeploy_rematerializes_certbot_on_source_change(tmp_path: Path) -> None:
    box = _make_box(tmp_path, "install-certbot-timer.sh")
    env = box["env"]
    marker = box["marker"]
    result = _run(env)  # type: ignore[arg-type]
    assert marker.exists(), (  # type: ignore[union-attr]
        "autodeploy must re-run install-certbot-timer.sh when its source changed, so the "
        f"host certbot-renew units are refreshed. rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_autodeploy_skips_certbot_on_unrelated_change(tmp_path: Path) -> None:
    box = _make_box(tmp_path, "README.md")
    env = box["env"]
    marker = box["marker"]
    result = _run(env)  # type: ignore[arg-type]
    assert not marker.exists(), (  # type: ignore[union-attr]
        "autodeploy must NOT re-materialize the certbot timer when only an unrelated file "
        f"changed. rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_autodeploy_bootstrap_clone_is_bounded(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    mirror = tmp_path / "mirror"
    timeout_log = tmp_path / "timeout-log"

    _stub(bin_dir, "flock", "exit 0")
    _stub(
        bin_dir,
        "timeout",
        f"""
        echo "$@" >> "{timeout_log}"
        exit 124
        """,
    )
    _stub(
        bin_dir,
        "git",
        """
        if [ "$1" = "clone" ]; then
          sleep 30
        fi
        exit 0
        """,
    )

    env = subprocess_env()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.update(
        {
            "STATE_DIR": str(state),
            "DEPLOY_REPO": str(deploy_repo),
            "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
            "MIRROR_DIR": str(mirror),
            "FETCH_TIMEOUT": "7",
        }
    )
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 1
    assert "AUTODEPLOY_ERROR" in result.stderr
    assert "mirror-clone-failed" in result.stderr
    assert timeout_log.read_text().strip().startswith("7 git clone -q")


def test_certbot_renew_service_renders_finite_start_timeout(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    webroot = tmp_path / "webroot"

    for tool in ("certbot", "nginx", "systemctl"):
        _stub(bin_dir, tool, "exit 0")

    env = subprocess_env()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["UNIT_DIR"] = str(unit_dir)
    env["WEBROOT"] = str(webroot)

    result = subprocess.run(
        ["bash", str(AUTODEPLOY.parents[0] / "install-certbot-timer.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    service = (unit_dir / "certbot-renew.service").read_text()
    assert result.returncode == 0, result.stderr
    assert "Type=oneshot" in service
    assert "TimeoutStartSec=" in service
