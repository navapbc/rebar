"""The store's only git maintenance is the explicit, write-locked step (bug 51e0-5038).

The production tickets clone went two months without maintenance. ``git fetch`` and
``git merge`` start a foreground ``git maintenance run --auto``; on a large store that is a
full gc, a rebar bound killed the parent, and the orphaned gc ran outside the write lock until
a hard kill stranded ``objects/maintenance.lock``, which git never expires and which makes
``maintenance run --auto`` a silent no-op. These tests pin the three parts of the fix:
bounded git children never start implicit maintenance, the explicit step runs git's
incremental tasks (no reachability walk), and a timed-out step is reaped as a process group
with SIGTERM so git can release its own lock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _git_upkeep import init_bare_remote

from rebar._store import event_commit_git, gitutil

pytestmark = pytest.mark.unit


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    """Inherited environment with the host's git config files out of the picture."""
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=str(empty),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="T",
        GIT_AUTHOR_EMAIL="t@e",
        GIT_COMMITTER_NAME="T",
        GIT_COMMITTER_EMAIL="t@e",
    )
    return env


def _git(env: dict[str, str], cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], env=env, check=True, capture_output=True, text=True
    ).stdout


def _subcommand(argv: list[str]) -> list[str]:
    """``argv`` from the git subcommand on, skipping ``-C <dir>`` / ``-c <k=v>`` pairs."""
    i = 1
    while i < len(argv) and argv[i] in ("-C", "-c"):
        i += 2
    return argv[i:]


def _maintenance_starts(trace: Path) -> list[list[str]]:
    """Every traced ``git maintenance`` or ``git gc`` process, from its subcommand on."""
    starts: list[list[str]] = []
    if not trace.exists():
        return starts
    for line in trace.read_text().splitlines():
        event = json.loads(line)
        sub = _subcommand([str(a) for a in event.get("argv") or []])
        if event.get("event") == "start" and sub[:1] in (["maintenance"], ["gc"]):
            starts.append(sub)
    return starts


def test_bounded_git_child_disables_implicit_maintenance_after_inherited_config() -> None:
    seen: dict[str, object] = {}

    def runner(cwd, *args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    inherited = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c"}
    gitutil.run_git_bounded("/x", "fetch", "origin", timeout=5, env=inherited, runner=runner)

    env = seen["env"]
    assert isinstance(env, dict)
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(3)] == [
        ("a.b", "c"),
        ("maintenance.auto", "false"),
        ("gc.auto", "0"),
    ]
    assert inherited["GIT_CONFIG_COUNT"] == "1", "the caller's mapping must not be mutated"


def test_git_config_reads_see_only_stored_settings() -> None:
    """``git config --get gc.auto`` must not report the injected suppression as stored config,
    or the ``gc-config`` ensure unit sees drift on every converged store."""
    seen: dict[str, object] = {}

    def runner(cwd, *args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(["git", *args], 1, "", "")

    gitutil.run_git_bounded("/x", "config", "--get", "gc.auto", timeout=5, env=None, runner=runner)
    assert seen["env"] is None


@pytest.fixture
def fetch_pair(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """A clone one commit behind its upstream, so the next fetch transfers objects."""
    env = _isolated_env(tmp_path)
    upstream, writer, reader = tmp_path / "up.git", tmp_path / "w", tmp_path / "r"
    init_bare_remote(upstream, env=env)
    subprocess.run(["git", "clone", "-q", str(upstream), str(writer)], env=env, check=True)
    _git(env, writer, "commit", "-q", "--allow-empty", "-m", "one")
    _git(env, writer, "push", "-q", "origin", "HEAD:main")
    subprocess.run(["git", "clone", "-q", str(upstream), str(reader)], env=env, check=True)
    _git(env, writer, "commit", "-q", "--allow-empty", "-m", "two")
    _git(env, writer, "push", "-q", "origin", "HEAD:main")
    return env, reader


def test_plain_fetch_starts_auto_maintenance(
    fetch_pair: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    """Control: the trace check can fail, because unsuppressed fetch does start it."""
    env, reader = fetch_pair
    trace = tmp_path / "plain.json"
    _git({**env, "GIT_TRACE2_EVENT": str(trace)}, reader, "fetch", "-q", "origin")
    assert any(argv[:3] == ["maintenance", "run", "--auto"] for argv in _maintenance_starts(trace))


def test_bounded_fetch_starts_no_auto_maintenance(
    fetch_pair: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    env, reader = fetch_pair
    trace = tmp_path / "bounded.json"
    result = gitutil.run_git_bounded(
        reader, "fetch", "-q", "origin", timeout=60, env={**env, "GIT_TRACE2_EVENT": str(trace)}
    )
    assert result.returncode == 0, result.stderr
    assert trace.exists(), "the fetch must have been traced"
    assert _maintenance_starts(trace) == []


def test_explicit_step_packs_loose_objects_without_gc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _isolated_env(tmp_path)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    _git(env, repo, "commit", "-q", "--allow-empty", "-m", "root")
    blobs = "".join(f"blob {i}\n" for i in range(300)).splitlines()
    paths = []
    for i, text in enumerate(blobs):
        p = tmp_path / f"b{i}"
        p.write_text(text)
        paths.append(str(p))
    subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin-paths"],
        input="\n".join(paths) + "\n",
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    trace = tmp_path / "explicit.json"
    for name in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(name)
    for name, value in {**env, "GIT_TRACE2_EVENT": str(trace)}.items():
        monkeypatch.setenv(name, value)

    result = event_commit_git.run_auto_maintenance(repo)

    assert result is not None and result.returncode == 0, result
    stats = dict(line.split(": ") for line in _git(env, repo, "count-objects", "-v").splitlines())
    assert int(stats["in-pack"]) >= 300, stats
    assert int(stats["prune-packable"]) == int(stats["count"]), stats
    started = [argv[0] for argv in _maintenance_starts(trace)]
    assert "maintenance" in started
    assert "gc" not in started, "the explicit step must not run gc's reachability walk"


_FAKE_GIT = """#!/bin/sh
# Stand-in for a maintenance run that outlives its bound: take a lock the way git does,
# release it only on a catchable signal, and keep a grandchild busy.
dir="$2"
touch "$dir/held.lock"
trap 'rm -f "$dir/held.lock"; exit 143' TERM
sleep 60 &
echo $! > "$dir/child.pid"
wait
"""


@pytest.mark.skipif(sys.platform == "win32", reason="process-group reaping is POSIX")
def test_timed_out_step_reaps_group_and_lets_git_release_its_lock(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "git"
    fake.write_text(_FAKE_GIT)
    fake.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    # Restored before the test returns, not at fixture teardown: other fixtures' teardown
    # runs real git and must not find this stand-in first on PATH.
    real_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bindir}{os.pathsep}{real_path}"
    try:
        result = event_commit_git.run_auto_maintenance(work, timeout=1.0)
    finally:
        os.environ["PATH"] = real_path

    assert result is not None and result.returncode == 124
    assert not (work / "held.lock").exists(), "SIGTERM must reach git so it drops its lock"
    child = int((work / "child.pid").read_text())
    deadline = time.monotonic() + 10
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)  # an orphaned zombie lingers until init reaps it
    assert not _alive(child), "no process of the maintenance group may outlive the step"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_explicit_step_runs_exactly_the_incremental_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    def fake_reaped(cwd, *args, timeout):
        seen.append(args)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(event_commit_git, "run_git_reaped", fake_reaped)
    # The once-a-day reflog pass is due here; its throttle is tested in test_store_reflog_expiry.
    monkeypatch.setattr(event_commit_git, "_reflog_expiry_due", lambda _common: True)
    event_commit_git.run_auto_maintenance("/x")
    assert seen == [
        (
            "maintenance",
            "run",
            "--auto",
            "--quiet",
            "--task=commit-graph",
            "--task=loose-objects",
            "--task=incremental-repack",
        ),
        ("reflog", "expire", "--all"),
    ]


def test_malformed_inherited_count_is_passed_through_for_git_to_report() -> None:
    seen: dict[str, object] = {}

    def runner(cwd, *args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    gitutil.run_git_bounded("/x", "fetch", timeout=5, env={"GIT_CONFIG_COUNT": "x"}, runner=runner)
    assert seen["env"] == {"GIT_CONFIG_COUNT": "x"}


def test_container_startup_fetch_disables_implicit_maintenance() -> None:
    script = Path(__file__).resolve().parents[2] / "infra/scripts/reviewbot-ensure-tickets.sh"
    text = script.read_text()
    fetch_at = text.index("fetch --quiet origin")
    assert "-c maintenance.auto=false -c gc.auto=0" in text[text.rindex("git -C", 0, fetch_at) :]
