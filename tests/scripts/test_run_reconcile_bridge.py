"""Happy-path contract for the runner-neutral reconcile bridge entrypoint."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

MODE_COMMANDS = {
    "reconcile-check": ["reconcile", "--mode", "reconcile-check"],
    "dry-run": ["bridge", "preview"],
    "bootstrap-strict": ["bridge", "sync", "--max-changes", "10"],
    "bootstrap-throttle": ["bridge", "sync", "--max-changes", "100"],
    "live": ["bridge", "sync"],
}


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed


def bridge_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a full-history checkout plus a real tickets remote/worktree."""
    origin = tmp_path / "tickets-origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=tickets", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )

    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "init", "--initial-branch=tickets", str(seed)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(seed, "config", "user.name", "Seed")
    git(seed, "config", "user.email", "seed@example.com")
    (seed / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(seed, "add", "seed.txt")
    git(seed, "commit", "-m", "seed tickets")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-u", "origin", "tickets")

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(checkout, "config", "user.name", "Checkout")
    git(checkout, "config", "user.email", "checkout@example.com")
    (checkout / "README").write_text("full history\n", encoding="utf-8")
    git(checkout, "add", "README")
    git(checkout, "commit", "-m", "seed checkout")

    tracker = checkout / ".tickets-tracker"
    subprocess.run(
        ["git", "clone", "--branch", "tickets", str(origin), str(tracker)],
        check=True,
        capture_output=True,
        text=True,
    )
    return checkout, tracker, origin


def runner_env(tmp_path: Path, checkout: Path, *, mode: str = "live") -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    argv_file = tmp_path / "rebar-argv"
    rebar = bin_dir / "rebar"
    rebar.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" > "$REBAR_ARGV_FILE"\n'
        'printf \'%s\\n\' "${REBAR_ROOT:-}" > "$REBAR_ROOT_FILE"\n'
        'if [ "${STUB_MUTATE:-1}" = 1 ]; then\n'
        "  printf 'event from bridge\\n' > .tickets-tracker/bridge-event.txt\n"
        "fi\n"
        "printf '%s' \"${STUB_STDOUT:-reconcile stdout}\"\n"
        "printf '%s' \"${STUB_STDERR:-reconcile stderr}\" >&2\n"
        'exit "${STUB_RC:-0}"\n',
        encoding="utf-8",
    )
    rebar.chmod(0o755)

    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "MODE": mode,
            "BRIDGE_RUN_ID": "run-123",
            "BRIDGE_BOT_NAME": "Bridge Bot",
            "BRIDGE_BOT_EMAIL": "bridge@example.com",
            "JIRA_URL": "https://jira.example.test",
            "JIRA_USER": "bridge@example.com",
            "JIRA_API_TOKEN": "secret",
            "JIRA_PROJECT": "RB",
            "REBAR_ENV_ID": "reconciler",
            "REBAR_ROOT": str(checkout),
            "REBAR_ARGV_FILE": str(argv_file),
            "REBAR_ROOT_FILE": str(tmp_path / "rebar-root"),
        }
    )
    return env


def run_bridge(checkout: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", "bridge", "run"],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(("mode", "expected"), MODE_COMMANDS.items())
def test_every_legacy_mode_routes_and_delivers_through_one_runner(
    tmp_path: Path, mode: str, expected: list[str]
) -> None:
    """The installed CLI selects each compatibility route and strictly delivers."""
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    env = runner_env(tmp_path, checkout, mode=mode)

    completed = run_bridge(checkout, env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert Path(env["REBAR_ARGV_FILE"]).read_text(encoding="utf-8").splitlines() == expected
    assert "reconcile stdout" in completed.stdout
    assert "reconcile stderr" in completed.stderr
    assert "Reconcile converged." in completed.stdout
    assert git(origin, "show", "tickets:bridge-event.txt").stdout == "event from bridge\n"
    assert (
        git(origin, "log", "-1", "--format=%s", "tickets").stdout.strip()
        == "chore: sync events from rebar reconciler [run run-123]"
    )
    assert git(origin, "log", "-1", "--format=%an <%ae>", "tickets").stdout.strip() == (
        "Bridge Bot <bridge@example.com>"
    )
