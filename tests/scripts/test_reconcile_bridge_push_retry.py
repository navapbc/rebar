"""Executable smoke tests for the two bridge workflow delivery adapters.

The contention/recovery algorithm lives in ``rebar._store.push`` and is covered by its
real-git suite.  These tests execute the exact workflow run blocks so the YAML-to-core
boundary cannot drift: argparse, synchronous override, strict exit status, authorship,
message, and persisted git state all participate.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
RECONCILER_WORKFLOW = REPO / ".github" / "workflows" / "reconcile-bridge.yml"
CANARY_WORKFLOW = REPO / ".github" / "workflows" / "reconcile-bridge-canary.yml"
CANARY_STEP = "Flush any unpushed ticket changes to origin/tickets"
RECONCILER_MESSAGE = "chore: sync events from rebar reconciler [run test-run-id]"


@dataclass(frozen=True)
class GitCase:
    root: Path
    tracker: Path
    origin: Path


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed


def _extract_step(path: Path, job: str, name: str) -> str:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    matches = [
        step["run"]
        for step in workflow["jobs"][job]["steps"]
        if isinstance(step, dict) and step.get("name") == name
    ]
    assert len(matches) == 1, f"expected one {name!r} step in {path.name}, found {len(matches)}"
    return matches[0]


def _core_invocation(block: str) -> list[str]:
    logical = block.replace("\\\n", " ")
    lines = logical.splitlines()
    matches = [line.strip() for line in lines if "python -m rebar._store.push" in line]
    assert len(matches) == 1, f"expected one core push invocation, found {matches}"
    tokens = shlex.split(matches[0])
    assert "|" not in tokens and "tee" not in tokens, (
        "the core process exit status must reach the workflow directly, not through a pipeline"
    )
    return tokens


def _assert_no_inline_git_mutation(block: str) -> None:
    forbidden = ("git add", "git commit", "git push", "git fetch", "git merge", "git rebase")
    hits = [needle for needle in forbidden if needle in block]
    assert not hits, f"workflow delivery block still contains raw git mutation: {hits}"


def _seed_case(base: Path, *, dirty: bool = False, ahead: bool = False) -> GitCase:
    base.mkdir()
    root = base / "runner"
    tracker = root / ".tickets-tracker"
    origin = base / "origin.git"
    root.mkdir()
    tracker.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Runner")
    _git(root, "config", "user.email", "runner@example.test")
    (root / "README").write_text("full history\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-q", "-m", "seed runner")
    _git(base, "init", "-q", "--bare", "-b", "tickets", str(origin))
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.name", "Ambient Author")
    _git(tracker, "config", "user.email", "ambient@example.test")
    _git(tracker, "remote", "add", "origin", str(origin))
    (tracker / "seed.json").write_text('{"seed": true}\n', encoding="utf-8")
    _git(tracker, "add", "seed.json")
    _git(tracker, "commit", "-q", "-m", "seed")
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")
    _git(
        tracker,
        "fetch",
        "-q",
        "origin",
        "+tickets:refs/remotes/origin/tickets",
    )
    (root / "rebar.toml").write_text("[sync]\npush = 'off'\n", encoding="utf-8")

    if ahead:
        (tracker / "ahead.json").write_text('{"ahead": true}\n', encoding="utf-8")
        _git(tracker, "add", "ahead.json")
        _git(tracker, "commit", "-q", "-m", "already committed")
    if dirty:
        (tracker / "inbound.json").write_text('{"inbound": true}\n', encoding="utf-8")
    return GitCase(root=root, tracker=tracker, origin=origin)


def _reject_all_pushes(origin: Path) -> None:
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\necho 'remote: policy declined by workflow smoke test' >&2\nexit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def _remote_head(origin: Path) -> str:
    return _git(origin, "-c", "safe.bareRepository=all", "rev-parse", "tickets").stdout.strip()


def _run_block(case: GitCase, block: str, label: str) -> subprocess.CompletedProcess[str]:
    script = case.root / f"{label}.sh"
    script.write_text(block, encoding="utf-8")
    env = subprocess_env()
    env.pop("REBAR_SYNC_PUSH", None)
    env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"
    env["BRIDGE_BOT_NAME"] = "Bridge Bot"
    env["BRIDGE_BOT_EMAIL"] = "bridge-bot@example.test"
    env["XDG_CONFIG_HOME"] = str(case.root / ".isolated-config")
    return subprocess.run(
        ["bash", str(script)],
        cwd=case.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _run_reconciler(case: GitCase, label: str) -> subprocess.CompletedProcess[str]:
    bin_dir = case.root / f"{label}-bin"
    bin_dir.mkdir()
    rebar = bin_dir / "rebar"
    rebar.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    rebar.chmod(0o755)
    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "MODE": "live",
            "BRIDGE_RUN_ID": "test-run-id",
            "BRIDGE_BOT_NAME": "Bridge Bot",
            "BRIDGE_BOT_EMAIL": "bridge-bot@example.test",
            "JIRA_URL": "https://jira.example.test",
            "JIRA_USER": "bridge@example.test",
            "JIRA_API_TOKEN": "secret",
            "JIRA_PROJECT": "RB",
            "REBAR_ENV_ID": "reconciler",
            "REBAR_ROOT": str(case.root),
            "XDG_CONFIG_HOME": str(case.root / ".isolated-config"),
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", "bridge", "run"],
        cwd=case.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_reconciler_workflow_commits_and_pushes_strictly(tmp_path: Path) -> None:
    """The shared runner preserves dirty-state, identity, and red failure outcomes."""
    workflow = yaml.safe_load(RECONCILER_WORKFLOW.read_text(encoding="utf-8"))
    invocations = [
        step.get("run")
        for step in workflow["jobs"]["reconcile"]["steps"]
        if step.get("name") == "Run reconciler"
    ]
    assert invocations == ["rebar bridge run"]

    success = _seed_case(tmp_path / "success", dirty=True)
    assert "?? inbound.json" in _git(success.tracker, "status", "--porcelain").stdout
    remote_before = _remote_head(success.origin)
    result = _run_reconciler(success, "reconciler-success")
    assert result.returncode == 0, result.stderr
    assert _remote_head(success.origin) != remote_before
    assert _remote_head(success.origin) == _git(success.tracker, "rev-parse", "HEAD").stdout.strip()
    assert _git(success.tracker, "log", "-1", "--format=%s").stdout.strip() == RECONCILER_MESSAGE
    assert _git(success.tracker, "log", "-1", "--format=%an <%ae>").stdout.strip() == (
        "Bridge Bot <bridge-bot@example.test>"
    )
    assert _git(success.tracker, "status", "--porcelain").stdout == ""
    landed_event = _git(
        success.origin,
        "-c",
        "safe.bareRepository=all",
        "show",
        "tickets:inbound.json",
    ).stdout
    assert landed_event == '{"inbound": true}\n'

    rejected = _seed_case(tmp_path / "rejected", dirty=True)
    remote_before = _remote_head(rejected.origin)
    _reject_all_pushes(rejected.origin)
    result = _run_reconciler(rejected, "reconciler-rejected")
    assert result.returncode != 0
    assert "push-policy-declined" in result.stderr
    local_head = _git(rejected.tracker, "rev-parse", "HEAD").stdout.strip()
    assert local_head != remote_before and _remote_head(rejected.origin) == remote_before
    assert _git(rejected.tracker, "cat-file", "-e", f"{local_head}^{{commit}}").returncode == 0
    assert _git(rejected.tracker, "status", "--porcelain").stdout == ""


def test_canary_workflow_pushes_ahead_state_and_fails_red_on_rejection(tmp_path: Path) -> None:
    """The exact canary block stays push-only and leaves rejected commits pending."""
    block = _extract_step(CANARY_WORKFLOW, "canary", CANARY_STEP)
    assert _core_invocation(block) == [
        "REBAR_SYNC_PUSH=always",
        "python",
        "-m",
        "rebar._store.push",
        "push",
        "--tracker",
        ".",
        "--strict",
    ]
    _assert_no_inline_git_mutation(block)

    success = _seed_case(tmp_path / "success", ahead=True)
    local_head = _git(success.tracker, "rev-parse", "HEAD").stdout.strip()
    assert local_head != _remote_head(success.origin)
    result = _run_block(success, block, "canary-success")
    assert result.returncode == 0, result.stderr
    assert _remote_head(success.origin) == local_head
    assert _git(success.tracker, "rev-parse", "HEAD").stdout.strip() == local_head

    rejected = _seed_case(tmp_path / "rejected", ahead=True)
    local_head = _git(rejected.tracker, "rev-parse", "HEAD").stdout.strip()
    remote_before = _remote_head(rejected.origin)
    _reject_all_pushes(rejected.origin)
    result = _run_block(rejected, block, "canary-rejected")
    assert result.returncode != 0
    assert "push-policy-declined" in result.stderr
    assert _git(rejected.tracker, "rev-parse", "HEAD").stdout.strip() == local_head
    assert _git(rejected.tracker, "cat-file", "-e", f"{local_head}^{{commit}}").returncode == 0
    assert _remote_head(rejected.origin) == remote_before
    assert _git(rejected.tracker, "status", "--porcelain").stdout == ""
