"""Create fixture bare remotes with background Git upkeep disabled.

A push may return while detached maintenance still rewrites the object store. Tests that
copy or locally clone that remote can then race pruning. Pin autoDetach and disable
fixture upkeep before any push. A push failure still propagates.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

# The upkeep posture a fixture bare remote is pinned to BEFORE anything is pushed into
# it. This dict is the SINGLE definition of those keys across ``tests/``; consume it,
# never restate it, so a posture change cannot half-land.
BARE_REMOTE_UPKEEP_PINS: dict[str, str] = {
    "gc.autoDetach": "false",
    "maintenance.autoDetach": "false",
    "receive.autogc": "false",
}


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env=None if env is None else dict(env),
    )


def apply_upkeep_pins(remote: Path, *, env: Mapping[str, str] | None = None) -> Path:
    """Pin *remote* to a posture that leaves no background upkeep behind a push."""
    for key, value in BARE_REMOTE_UPKEEP_PINS.items():
        _git(remote, "config", key, value, env=env)
    return remote


def init_bare_remote(
    remote: Path,
    *,
    initial_branch: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Initialize a bare remote and pin upkeep before its first push.

    initial_branch selects its HEAD branch, including ticket-store fixtures that need tickets.
    """
    remote.parent.mkdir(parents=True, exist_ok=True)
    branch = [f"--initial-branch={initial_branch}"] if initial_branch else []
    subprocess.run(
        ["git", "init", "-q", "--bare", *branch, str(remote)],
        check=True,
        capture_output=True,
        text=True,
        env=None if env is None else dict(env),
    )
    return apply_upkeep_pins(remote, env=env)


def missing_upkeep_pins(repo: Path) -> list[str]:
    """Which pins *repo* does not carry, in ``BARE_REMOTE_UPKEEP_PINS`` order."""
    missing = []
    for key, value in BARE_REMOTE_UPKEEP_PINS.items():
        found = _git(repo, "config", "--get", key, check=False)
        if found.returncode != 0 or found.stdout.strip() != value:
            missing.append(key)
    return missing


def is_bare_repository(path: Path) -> bool:
    """Whether *path* is itself the git directory of a BARE repository."""
    probe = _git(path, "rev-parse", "--is-bare-repository", check=False)
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def unpinned_bare_repositories(tree: Path) -> list[tuple[Path, list[str]]]:
    """Every bare repository under *tree* missing pins, as ``(path, missing_keys)``.

    Descent stops at each git directory: a repository's own internals hold no further
    repositories, so this walks the topology rather than every loose-object fanout.
    """
    found: list[tuple[Path, list[str]]] = []
    for dirpath, dirnames, _filenames in os.walk(tree):
        current = Path(dirpath)
        if not ((current / "HEAD").is_file() and (current / "objects").is_dir()):
            continue
        dirnames[:] = []
        if not is_bare_repository(current):
            continue
        missing = missing_upkeep_pins(current)
        if missing:
            found.append((current, missing))
    return sorted(found)


def _trace_events(trace: Path) -> Iterator[dict[str, object]]:
    """Every well-formed event in a ``GIT_TRACE2_EVENT`` log, or none if it is absent.

    A trace git never wrote is not an error here: it is the vacuous case the positive
    control in ``assert_no_detached_upkeep`` exists to catch, and it must reach that
    assertion rather than raising ``FileNotFoundError`` first.
    """
    if not trace.exists():
        return
    for line in trace.read_text(encoding="utf-8").splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:  # pragma: no cover - trace2 writes well-formed JSON
            continue


def maintenance_children(trace: Path) -> list[list[str]]:
    """Every ``git maintenance run`` child recorded in a ``GIT_TRACE2_EVENT`` log."""
    children: list[list[str]] = []
    for event in _trace_events(trace):
        argv = event.get("argv") or []
        if event.get("event") != "child_start" or len(argv) < 3:
            continue
        # argv[0] is the git executable, which trace2 may record resolved to a full path.
        if Path(argv[0]).stem == "git" and argv[1:3] == ["maintenance", "run"]:
            children.append(argv)
    return children


def receive_pack_targets(trace: Path) -> list[str]:
    """Every repository a recorded ``git-receive-pack`` child was invoked against.

    A local push spawns ``git-receive-pack '<path>'`` as a single argv[0] string, so the
    receiving repository is named in the trace even though a bare repository has no
    worktree for trace2 to report.
    """
    targets: list[str] = []
    for event in _trace_events(trace):
        argv = event.get("argv") or []
        if event.get("event") == "child_start" and argv and "git-receive-pack" in argv[0]:
            targets.append(argv[0])
    return targets


def assert_no_detached_upkeep(trace: Path, remote: Path) -> None:
    """Fail unless *trace* proves the pushes it recorded left no detached upkeep.

    The assertion is a content fact about an argv list. It has no timing bound, polls
    nothing, and does not care how long maintenance takes — only that none of it is
    left running once the pushing command has returned.

    Scope the trace to the pushes you mean to judge. A trace spanning a whole fixture
    build also records upkeep spawned by that fixture's OTHER repositories, which this
    cannot tell apart: trace2 reports no worktree for a bare repository, so a
    maintenance child cannot be attributed to the repository that spawned it.
    """
    # Positive control: the trace must actually cover a push into THIS remote, which is
    # where a detached maintenance child would be recorded. Without this the assertion
    # below would pass just as happily against an empty or unwritten trace file, or
    # against a trace that only ever saw some other repository.
    candidates = (str(remote), str(remote.resolve()))
    pushed_here = [
        target for target in receive_pack_targets(trace) if any(c in target for c in candidates)
    ]
    assert pushed_here, (
        f"trace2 at {trace} recorded no git-receive-pack against {remote}, so it cannot "
        f"witness upkeep there. Recorded instead: {receive_pack_targets(trace) or 'nothing'}"
    )
    detached = [argv for argv in maintenance_children(trace) if "--detach" in argv]
    assert not detached, (
        f"a push left detached background upkeep running against {remote}: {detached}. "
        f"Build the remote with init_bare_remote() so it carries "
        f"{sorted(BARE_REMOTE_UPKEEP_PINS)}."
    )
