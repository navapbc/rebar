"""The per-ticket reducer cache (.cache.json) must never be committed.

Faithful in-process port of tests/scripts/test-ticket-cache-gitignored.sh (the
bash engine is being deleted). A committed cache would create cross-client merge
conflicts (Concurrency Doctrine §0 I3/I6), so:

  1. ``.cache.json`` is listed in the committed tracker ``.gitignore`` (on the
     ``tickets`` branch).
  2. ``git add -A`` in the tracker never stages a ``.cache.json`` — neither one at
     the tracker root nor one sitting inside a ticket directory alongside real
     event files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import rebar


def _git_out(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def test_cache_json_in_committed_tracker_gitignore(rebar_repo: Path) -> None:
    tracker = rebar_repo / ".tickets-tracker"
    gitignore = _git_out("show", "tickets:.gitignore", cwd=tracker)
    # The bash test used `grep -qFx '.cache.json'` — an exact full-line match.
    assert ".cache.json" in gitignore.splitlines()


def test_git_add_never_stages_cache_json(rebar_repo: Path) -> None:
    tracker = rebar_repo / ".tickets-tracker"

    # A real ticket dir with event files (so the in-ticket cache has a home).
    tid = rebar.create_ticket("task", "cache gitignore test", repo_root=str(rebar_repo))
    ticket_dir = tracker / tid
    assert ticket_dir.is_dir()

    # Stray caches: one at the tracker root, one inside the ticket dir.
    (tracker / ".cache.json").write_text('{"stale": true}')
    (ticket_dir / ".cache.json").write_text('{"stale": true}')

    subprocess.run(["git", "add", "-A"], cwd=tracker, capture_output=True, text=True)
    staged = _git_out("diff", "--cached", "--name-only", cwd=tracker).splitlines()

    cache_staged = [p for p in staged if ".cache.json" in p]
    assert cache_staged == [], f"git add -A staged cache files: {cache_staged}"

    # Restore the index (parity with the bash test's `git reset -q`).
    subprocess.run(["git", "reset", "-q"], cwd=tracker, capture_output=True, text=True)
