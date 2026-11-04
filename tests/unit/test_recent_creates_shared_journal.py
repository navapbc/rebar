"""The recent-creates journal belongs to the STORE; the HLC cache may stay beside it.

Bug soot-ammonitic-redpoll. In the deployed MCP container the tracker
(``/var/gerrit/site/mcp-tickets``) is a volume shared by the blue and green containers, but its
sibling ``.rebar/`` sits on each container's own writable layer. Keyed on ``.rebar``, the
journal behind the create-time ``duplicate_warning`` was emptied by every deploy and split in
two during the overlap, so a same-title create on the other container (or a create retried
across a deploy) went unflagged. Each child below gets its own ``.rebar`` for one store, which
is exactly what two containers see.

The HLC cache is the contrast case: its loss is harmless because every tick is floored by the
ticket's committed event prefixes, which live in the shared tracker. One test pins that.

No autouse fixtures. Every child is started through ``_subprocess_env.subprocess_env``
and bounded by a ``communicate`` timeout, then killed and reaped on every path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar._commands import recent_creates
from rebar._store.paths import StorePaths

pytestmark = pytest.mark.unit

_CHILD_BOUND_S = 60
_TITLE = "Fix the widget"

# One process standing in for one container: its OWN `.rebar` (the writable layer) over the
# shared tracker (the volume). It runs one action and prints the result as JSON.
_CHILD = textwrap.dedent(
    """
    import json, os, sys
    from rebar._store import paths

    tracker, own_rebar, action, arg = sys.argv[1:5]
    paths._rebar_dir = lambda _tracker: own_rebar  # this "container's" writable-layer .rebar
    if action == "create":
        from rebar._commands.recent_creates import duplicate_create_warning

        ticket_id, title = arg.split("|", 1)
        print(json.dumps(duplicate_create_warning(
            tracker, ticket_id=ticket_id, alias=ticket_id, title=title, cfg_root=tracker
        )))
    elif action == "tick_write":
        from rebar._store import hlc

        tick = hlc.next_tick(tracker, arg)
        os.makedirs(os.path.join(tracker, arg), exist_ok=True)
        with open(os.path.join(tracker, arg, f"{tick}-{os.getpid()}-STATUS.json"), "w") as fh:
            fh.write("{}")
        print(json.dumps(tick))
    """
)


def _git_store(tmp_path: Path) -> str:
    """A git-backed tracker, as every rebar store is. Returns its (canonical) path."""
    tracker = Path(os.path.realpath(tmp_path)) / "site" / "mcp-tickets"
    tracker.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=tracker, check=True, capture_output=True)
    return str(tracker)


def _container_rebar(tmp_path: Path, name: str) -> str:
    own = Path(os.path.realpath(tmp_path)) / name / ".rebar"
    own.mkdir(parents=True)
    return str(own)


def _run(tracker: str, own_rebar: str, action: str, arg: str) -> object:
    root = str(Path(__file__).resolve().parents[2])
    env = subprocess_env(
        PYTHONPATH=os.pathsep.join([root, os.path.join(root, "src")]),
        REBAR_VERIFY_SUGGEST_DUPLICATE_TICKETS="1",
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, tracker, own_rebar, action, arg],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        out, err = proc.communicate(timeout=_CHILD_BOUND_S)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


def test_a_create_on_one_container_is_seen_by_a_same_title_create_on_the_other(
    tmp_path: Path,
) -> None:
    """The blue-green overlap, and a create retried across a deploy: one store, two `.rebar`."""
    tracker = _git_store(tmp_path)
    blue = _container_rebar(tmp_path, "blue")
    green = _container_rebar(tmp_path, "green")

    assert _run(tracker, blue, "create", f"id-1|{_TITLE}") is None, "precondition: first create"
    warning = _run(tracker, green, "create", f"id-2|{_TITLE}")

    assert isinstance(warning, str) and "id-1" in warning, (
        "a same-title create from a process with a fresh `.rebar` missed the store's recent create"
    )


def test_the_journal_and_its_lock_live_in_the_trackers_git_dir(tmp_path: Path) -> None:
    """On the store itself, beside the compaction sidecars; nothing beside the tracker."""
    tracker = _git_store(tmp_path)
    git_dir = os.path.join(tracker, ".git")
    sibling = Path(tracker).parent / ".rebar"

    _run(tracker, str(sibling), "create", f"id-1|{_TITLE}")

    assert os.path.dirname(recent_creates._journal_path(tracker)) == git_dir
    assert os.path.isfile(os.path.join(git_dir, "rebar-recent-creates.json"))
    assert os.path.isfile(os.path.join(git_dir, "rebar-recent-creates.lock"))
    assert not (sibling / "recent-creates.json").exists()


def test_a_linked_worktree_tracker_keeps_the_journal_in_its_own_git_dir(tmp_path: Path) -> None:
    """A local tracker is a linked worktree (``.git`` is a FILE): the journal follows the
    pointer to that worktree's git dir, which is per-store and outlives code worktrees."""
    base = Path(os.path.realpath(tmp_path)) / "repo"
    base.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "i"),
        ("git", "worktree", "add", "-q", "--detach", str(base / ".tickets-tracker")),
    ):  # fmt: skip
        subprocess.run(args, cwd=base, check=True, capture_output=True)
    tracker = str(base / ".tickets-tracker")
    git_dir = StorePaths(tracker).git_dir

    assert os.path.isdir(git_dir) and git_dir != os.path.join(tracker, ".git")
    assert os.path.dirname(recent_creates._journal_path(tracker)) == git_dir


def test_a_fresh_hlc_cache_still_ticks_above_the_tickets_committed_events(tmp_path: Path) -> None:
    """Why ``hlc.state`` may stay on the writable layer: the floor is in the shared tracker.

    Blue's cache runs an hour ahead of the wall clock (a skewed peer witness does that), and
    blue writes an event at that value. Green, with an empty cache, must still tick above it."""
    tracker = _git_store(tmp_path)
    blue = _container_rebar(tmp_path, "blue")
    green = _container_rebar(tmp_path, "green")
    ahead = time.time_ns() + 3_600 * 1_000_000_000
    Path(blue, "hlc.state").write_text(str(ahead), encoding="utf-8")

    blue_tick = _run(tracker, blue, "tick_write", "tkt-x")
    green_tick = _run(tracker, green, "tick_write", "tkt-x")

    assert blue_tick == ahead + 1, "precondition: blue ticked from its skewed cache"
    assert isinstance(green_tick, int) and green_tick > blue_tick, (
        "a process with a fresh HLC cache ordered its event before the ticket's committed one"
    )
