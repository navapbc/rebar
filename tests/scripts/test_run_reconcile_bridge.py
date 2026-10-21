"""Happy-path contract for the runner-neutral reconcile bridge entrypoint."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from _git_upkeep import init_bare_remote
from _sandbox_capabilities import scrub_ambient_git_config
from _subprocess_env import SubprocessEnv, subprocess_env

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _git_env() -> SubprocessEnv:
    env = subprocess_env(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    scrub_ambient_git_config(env)
    return env


MODE_ARGS = {
    "dry-run": ["preview"],
    "bootstrap-strict": ["sync", "--max-changes", "10"],
    "bootstrap-throttle": ["sync", "--max-changes", "100"],
    "live": ["sync"],
}


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=_git_env(),
    )
    assert completed.returncode == 0, completed.stderr
    return completed


def bare_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=_git_env(),
    )
    assert completed.returncode == 0, completed.stderr
    return completed


def bridge_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a full-history checkout plus a real tickets remote/worktree."""
    origin = tmp_path / "tickets-origin.git"
    init_bare_remote(origin, initial_branch="tickets", env=_git_env())

    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "init", "--initial-branch=tickets", str(seed)],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
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
        env=_git_env(),
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
        env=_git_env(),
    )
    return checkout, tracker, origin


def runner_env(tmp_path: Path, checkout: Path, *, mode: str = "live") -> dict[str, str]:
    """The bridge environment plus the engine stub's controls (``engine_stub``).

    ``STUB_RC``/``STUB_STDOUT``/``STUB_STDERR`` script the stubbed engine's result,
    ``STUB_MUTATE=0`` stops it writing a ticket event, and it records its argv and the
    ``REBAR_ROOT`` it was handed in ``REBAR_ARGV_FILE``/``REBAR_ROOT_FILE``.
    """
    (tmp_path / "bin").mkdir(exist_ok=True)
    env = subprocess_env()
    scrub_ambient_git_config(env)
    env.update(
        {
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
            "REBAR_ARGV_FILE": str(tmp_path / "rebar-argv"),
            "REBAR_ROOT_FILE": str(tmp_path / "rebar-root"),
        }
    )
    return env


def engine_stub(real_spawn: Callable[..., subprocess.CompletedProcess[str]]):
    """Wrap the runner's ``_spawn_bounded``: stub the engine child, run every other child.

    The runner spawns ``sys.executable -m rebar.reconciler`` directly, so a ``rebar``
    stub on ``PATH`` can no longer intercept it; the stub sits on the spawn seam instead.
    The delivery child (the real ticket-store commit and push) goes to *real_spawn*.
    """

    def spawn(command, *, root, environ):
        argv = list(command)
        if argv[1:3] != ["-m", "rebar.reconciler"]:
            return real_spawn(command, root=root, environ=environ)
        if "REBAR_ARGV_FILE" in environ:
            Path(environ["REBAR_ARGV_FILE"]).write_text("\n".join(argv) + "\n", encoding="utf-8")
        if "REBAR_ROOT_FILE" in environ:
            Path(environ["REBAR_ROOT_FILE"]).write_text(
                environ.get("REBAR_ROOT", "") + "\n", encoding="utf-8"
            )
        if environ.get("STUB_MUTATE", "1") == "1":
            (Path(root) / ".tickets-tracker" / "bridge-event.txt").write_text(
                "event from bridge\n", encoding="utf-8"
            )
        return subprocess.CompletedProcess(
            argv,
            int(environ.get("STUB_RC", "0")),
            environ.get("STUB_STDOUT", "reconcile stdout"),
            environ.get("STUB_STDERR", "reconcile stderr"),
        )

    return spawn


# Runs the real ``rebar bridge run`` CLI in a child interpreter with ``engine_stub``
# installed on the runner's spawn seam; argv[1] is this harness file.
_CLI_DRIVER = """
import importlib.util, sys
from pathlib import Path
harness_path = Path(sys.argv[1])
sys.path[:0] = [str(harness_path.parents[1]), str(harness_path.parent)]
spec = importlib.util.spec_from_file_location("bridge_cli_harness", harness_path)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
from rebar import _bridge_runner
_bridge_runner._spawn_bounded = harness.engine_stub(_bridge_runner._spawn_bounded)
from rebar.cli import main
sys.argv = ["rebar", "bridge", "run"]
main()
"""


def run_bridge(
    checkout: Path,
    env: dict[str, str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``rebar bridge run`` in a child process with the engine stubbed."""
    return subprocess.run(
        [sys.executable, "-c", _CLI_DRIVER, str(Path(__file__).resolve())],
        cwd=checkout if cwd is None else cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _engine_argv(checkout: Path, expected: list[str]) -> list[str]:
    return [sys.executable, "-m", "rebar.reconciler", *expected, "--repo-root", str(checkout)]


def _assert_delivered(origin: Path, run_id: str = "run-123") -> None:
    assert bare_git(origin, "show", "tickets:bridge-event.txt").stdout == "event from bridge\n"
    assert (
        bare_git(origin, "log", "-1", "--format=%s", "tickets").stdout.strip()
        == f"chore: sync events from rebar reconciler [run {run_id}]"
    )
    assert bare_git(origin, "log", "-1", "--format=%an <%ae>", "tickets").stdout.strip() == (
        "Bridge Bot <bridge@example.com>"
    )


@pytest.mark.parametrize(("mode", "expected"), MODE_ARGS.items())
def test_every_mode_routes_and_delivers_through_one_runner(
    tmp_path: Path, mode: str, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each profile spawns its engine route directly, then the runner strictly delivers."""
    from rebar import _bridge_runner

    checkout, _tracker, origin = bridge_workspace(tmp_path)
    env = runner_env(tmp_path, checkout, mode=mode)
    env["STUB_STDERR"] = "reconcile stderr\nBRIDGE_STATE: converged\n"
    monkeypatch.setattr(
        _bridge_runner, "_spawn_bounded", engine_stub(_bridge_runner._spawn_bounded)
    )

    result = _bridge_runner.run_bridge(environ=env)

    assert (result["state"], result["returncode"]) == ("converged", 0), result
    argv = Path(env["REBAR_ARGV_FILE"]).read_text(encoding="utf-8").splitlines()
    assert argv == _engine_argv(checkout, expected)
    assert Path(env["REBAR_ROOT_FILE"]).read_text(encoding="utf-8").strip() == str(checkout)
    details = result["details"]
    assert "reconcile stdout" in details["stdout"]
    assert "reconcile stderr" in details["stderr"]
    assert "Reconcile converged." in details["stdout"]
    _assert_delivered(origin)


def test_the_cli_renders_the_runner_result_and_delivers(tmp_path: Path) -> None:
    """``rebar bridge run`` renders the captured streams once and exits with the result."""
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    env = runner_env(tmp_path, checkout)

    completed = run_bridge(checkout, env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    argv = Path(env["REBAR_ARGV_FILE"]).read_text(encoding="utf-8").splitlines()
    assert argv == _engine_argv(checkout, MODE_ARGS["live"])
    assert "reconcile stdout" in completed.stdout
    assert "reconcile stderr" in completed.stderr
    assert "Reconcile converged." in completed.stdout
    _assert_delivered(origin)
