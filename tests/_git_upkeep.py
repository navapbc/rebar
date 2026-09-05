"""Shared test helper: fixture bare remotes that leave no background git upkeep.

At git's DEFAULTS a bare repository's ``receive-pack`` runs ``run_auto_maintenance()``
after every push, spawning ``git maintenance run --auto --quiet --detach`` — a child
process that OUTLIVES the push. ``subprocess.run([... "push" ...])`` returning is
therefore NOT a happens-before edge for that repository's object database. A fixture
that pushes into a bare remote and then COPIES or byte-walks it races a concurrent
repack: entries vanish mid-walk and the copy raises.

Measured on git 2.55, replaying a fixture's own construction sequence under
``GIT_TRACE2_EVENT`` (bug b394-6198-6010-42f7):

===========================  ==========================  ===================
remote posture               maintenance children        detached children
===========================  ==========================  ===================
git defaults                 1 (``--detach``)            1
``autoDetach`` pins only     1 (``--no-detach``)         0
all of the pins below        0                           0
===========================  ==========================  ===================

The mutation is real, not theoretical. With the remote holding 880 loose objects,
12 runs at git's defaults left 22 loose objects behind by the time the last push
returned — whole fanout directories packed away and pruned — and one run was caught
mid-repack at 154, a different value at the same sampling point. Pinned, all 12 runs
left the object database untouched at 880. That is the race the ticket store's own CI
caught as a vanished ``objects/maintenance.lock`` and as pruned ``objects/2c``
directories (bugs 5b74-5d8f-a6b4-4674 and dca1-f641-caeb-4df4).

rebar pins the two ``autoDetach`` keys on every store it OWNS (``_commands/init.py``'s
gc-config ensure unit; bug 88eb, ADR 0051), which is why maintenance children against
rebar-owned trackers already run ``--no-detach``. A bare remote built by a test fixture
is owned by no rebar code, so it must be pinned here.

The third key is the direct fix: a throwaway fixture remote has no reason to run upkeep
at all, and it drops a push's maintenance children to ZERO. The two ``autoDetach`` pins
are defence in depth for any other trigger — they do not suppress maintenance, they force
it into the FOREGROUND, so the git command that triggered it cannot return while it is
still running. Either way no mutator survives the push.

This REMOVES the concurrent mutator rather than tolerating it: no retry, no sleep, no
timing bound, no ignore predicate, and no assertion weakened. A push that genuinely
cannot succeed still fails.

**The second amplifier: a LOCAL ``git clone``** (bug 57d2-e356-7eb4-4bf5). A clone whose
source is a local path does not use the transport; it hardlinks or copies every entry it
finds under the source's ``objects/``, exactly the wholesale walk ``copytree`` performs.
Entries the detached maintenance child prunes between readdir and copy make git die with
exit 128 -- ``failed to copy file to '<dst>/.git/objects/<sha>'``, ``unable to read sha1
file``, or ``unable to parse commit``, one race with three faces. Measured on git 2.55,
push-then-local-clone in a loop against one bare remote seeded like a ticket store, 8
concurrent workers x 120 iterations on a loaded host: **unpinned 5/960 clones exited 128;
pinned 0/960**. ``tests/unit/test_bare_clone_source_upkeep_sweep.py`` is the tree sweep
that keeps a new fixture from cloning an unpinned remote.

Import it bare (``from _git_upkeep import init_bare_remote``) like the other shared root
helpers — ``tests/`` is on ``sys.path`` (see tests/conftest.py).
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
    """Create a bare repository at *remote*, pinned before anything can be pushed.

    The pins are applied in the same call that creates the repository, so there is no
    window in which a push could reach an unpinned remote.

    *initial_branch* names the repository's initial ``HEAD`` branch, the
    ``--initial-branch=tickets`` that several ticket-store fixtures need. It is a
    parameter rather than a reason to hand-roll ``git init --bare`` beside this
    helper, because hand-rolling is what leaves a remote unpinned.
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
