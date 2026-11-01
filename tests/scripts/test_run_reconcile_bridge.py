"""Happy-path contract for the runner-neutral reconcile bridge entrypoint.

Held-out oracles merged from test_run_reconcile_bridge_heldout: Held-out edge and recovery
oracle for the runner-neutral bridge entrypoint.
"""

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


# ── held-out oracles (merged from test_run_reconcile_bridge_heldout) ──


@pytest.mark.parametrize("state", ["in-flight", "legacy-gated", "reschedule"])
def test_benign_engine_states_pass_through_the_runner_and_deliver(
    tmp_path: Path, state: str
) -> None:
    # The engine child canonicalises once: a benign state arrives as exit 0 plus one
    # ``BRIDGE_STATE:`` marker, and the runner only names it.
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    env = runner_env(tmp_path, checkout, mode="dry-run")
    env["STUB_STDERR"] = f"BRIDGE_STATE: {state}\n"

    completed = run_bridge(checkout, env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert bare_git(origin, "show", "tickets:bridge-event.txt").stdout == "event from bridge\n"


@pytest.mark.parametrize("legacy_rc", [3, 4, 75])
def test_legacy_sentinels_are_not_translated_by_the_runner(tmp_path: Path, legacy_rc: int) -> None:
    # 3/4/75 belong to the argument-less direct-engine route; the runner never invokes it,
    # so a sentinel here is an out-of-contract exit, not a benign result to deliver.
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    before = bare_git(origin, "rev-parse", "tickets").stdout.strip()
    env = runner_env(tmp_path, checkout, mode="dry-run")
    env["STUB_RC"] = str(legacy_rc)

    completed = run_bridge(checkout, env)

    assert completed.returncode == 1
    assert f"Reconcile failed (exit {legacy_rc})" in completed.stderr
    assert bare_git(origin, "rev-parse", "tickets").stdout.strip() == before


@pytest.mark.parametrize("rc", [1, 2])
def test_operational_and_invocation_failures_are_preserved_without_delivery(
    tmp_path: Path, rc: int
) -> None:
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    before = bare_git(origin, "rev-parse", "tickets").stdout.strip()
    env = runner_env(tmp_path, checkout)
    env.update({"STUB_RC": str(rc), "STUB_STDERR": "specific subprocess diagnostic\n"})

    completed = run_bridge(checkout, env)

    assert completed.returncode == rc
    assert "specific subprocess diagnostic" in completed.stderr
    assert bare_git(origin, "rev-parse", "tickets").stdout.strip() == before


def test_exact_pause_marker_is_benign_and_skips_delivery(tmp_path: Path) -> None:
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    before = bare_git(origin, "rev-parse", "tickets").stdout.strip()
    env = runner_env(tmp_path, checkout)
    summary = tmp_path / "step-summary.md"
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    env["STUB_MUTATE"] = "0"
    env["STUB_STDERR"] = (
        'BRIDGE_PAUSED: {"paused":true,"reason":"cutover","who":"operator@example.com",'
        '"paused_at":"2026-08-10T12:00:00Z"}\n'
    )

    completed = run_bridge(checkout, env)

    assert completed.returncode == 0
    assert "Reconcile bridge is paused; skipping ticket commit and push." in completed.stdout
    assert bare_git(origin, "rev-parse", "tickets").stdout.strip() == before
    summary_text = summary.read_text(encoding="utf-8")
    assert "## :pause_button: Reconcile Bridge PAUSED" in summary_text
    assert "- **Reason:** `cutover`" in summary_text
    assert "- **Paused at:** `2026-08-10T12:00:00Z`" in summary_text
    assert "reconciled nothing" in summary_text
    assert "FAILED" not in summary_text


@pytest.mark.parametrize(
    "marker",
    [
        "BRIDGE_PAUSED: {not-json}\n",
        'prefix BRIDGE_PAUSED: {"paused":true}\n',
        'BRIDGE_PAUSED: {"paused":false,"reason":"r","who":"w","paused_at":"t"}\n',
        'BRIDGE_PAUSED: {"paused":true,"reason":"r","who":"w"}\n',
    ],
)
def test_malformed_or_inexact_pause_markers_do_not_suppress_delivery(
    tmp_path: Path, marker: str
) -> None:
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    env = runner_env(tmp_path, checkout)
    env["STUB_STDERR"] = marker

    completed = run_bridge(checkout, env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert bare_git(origin, "show", "tickets:bridge-event.txt").stdout == "event from bridge\n"


@pytest.mark.parametrize(
    "missing",
    [
        "MODE",
        "BRIDGE_RUN_ID",
        "BRIDGE_BOT_NAME",
        "BRIDGE_BOT_EMAIL",
        "JIRA_URL",
        "JIRA_USER",
        "JIRA_API_TOKEN",
        "JIRA_PROJECT",
        "REBAR_ENV_ID",
    ],
)
def test_required_delivery_identity_is_validated_before_reconciliation(
    tmp_path: Path, missing: str
) -> None:
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    before = bare_git(origin, "rev-parse", "tickets").stdout.strip()
    env = runner_env(tmp_path, checkout)
    del env[missing]

    completed = run_bridge(checkout, env)

    assert completed.returncode == 2
    assert missing in completed.stderr
    assert not Path(env["REBAR_ARGV_FILE"]).exists()
    assert bare_git(origin, "rev-parse", "tickets").stdout.strip() == before


def test_unknown_mode_is_invalid_before_reconciliation(tmp_path: Path) -> None:
    checkout, _tracker, _origin = bridge_workspace(tmp_path)
    env = runner_env(tmp_path, checkout, mode="surprise")

    completed = run_bridge(checkout, env)

    assert completed.returncode == 2
    assert "Unknown bridge profile: surprise" in completed.stderr
    assert not Path(env["REBAR_ARGV_FILE"]).exists()


def test_shallow_checkout_is_rejected_before_reconciliation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(source, "config", "user.name", "Seed")
    git(source, "config", "user.email", "seed@example.com")
    (source / "one").write_text("one\n", encoding="utf-8")
    git(source, "add", "one")
    git(source, "commit", "-m", "one")
    (source / "two").write_text("two\n", encoding="utf-8")
    git(source, "add", "two")
    git(source, "commit", "-m", "two")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth=1", source.as_uri(), str(shallow)],
        check=True,
        capture_output=True,
        text=True,
    )
    env = subprocess_env()
    env.pop("REBAR_ROOT", None)
    env.update(
        {
            "MODE": "live",
            "BRIDGE_RUN_ID": "run-123",
            "BRIDGE_BOT_NAME": "Bridge Bot",
            "BRIDGE_BOT_EMAIL": "bridge@example.com",
            "JIRA_URL": "https://jira.example.test",
            "JIRA_USER": "bridge@example.com",
            "JIRA_API_TOKEN": "secret",
            "JIRA_PROJECT": "RB",
            "REBAR_ENV_ID": "reconciler",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "bridge", "run"],
        cwd=shallow,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "shallow" in completed.stderr.lower()
    assert "full history" in completed.stderr.lower()


def test_strict_delivery_failure_is_fatal_and_preserves_diagnostic(tmp_path: Path) -> None:
    checkout, tracker, _origin = bridge_workspace(tmp_path)
    env = runner_env(tmp_path, checkout)
    git(tracker, "remote", "set-url", "origin", str(tmp_path / "missing-origin.git"))

    completed = run_bridge(checkout, env)

    assert completed.returncode == 1
    assert "missing-origin" in completed.stderr or "does not appear to be a git repository" in (
        completed.stderr
    )


def test_legacy_engine_dispositions_remain_visible_to_direct_callers() -> None:
    from rebar.reconciler.guarded_pass import _Disposition

    assert _Disposition.IN_FLIGHT.legacy_exit == 3
    assert _Disposition.PHASE_GATE.legacy_exit == 4
    assert _Disposition.RESCHEDULE.legacy_exit == 75
    assert {
        _Disposition.OPERATIONAL_FAILURE.canonical_exit,
        _Disposition.INVALID_INVOCATION.canonical_exit,
    } == {
        1,
        2,
    }
