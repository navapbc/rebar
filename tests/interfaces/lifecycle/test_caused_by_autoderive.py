"""Happy-path contract for blame-derived caused_by on bug close (ticket 555e).

Tier: interface (library/CLI over a real temp store). Pins the deterministic core:
an explicit `--caused-by <id>` on a bug close draws a caused_by link from the
(now-closed) bug to the culprit — proving the flag threads through the close path
AND that the link is written via the direct-writer that bypasses the closed-source
guard. Blame auto-derivation, the library facade, and ambiguity are held out.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import rebar

pytestmark = pytest.mark.interface


def _cli(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args], capture_output=True, text=True, cwd=cwd
    )


def _caused_by_targets(tid: str, repo: str) -> list[str]:
    deps = rebar.show_ticket(tid, repo_root=repo)["deps"]
    return [d["target_id"] for d in deps if d["relation"] == "caused_by"]


def test_explicit_caused_by_on_bug_close(rebar_repo) -> None:
    repo = str(rebar_repo)
    culprit = rebar.create_ticket("task", "the culprit change", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)

    p = _cli(
        "transition",
        bug,
        "in_progress",
        "closed",
        "--class=regression",
        f"--caused-by={culprit}",
        cwd=repo,
    )
    assert p.returncode == 0, p.stderr

    # The bug (closed source) has a caused_by edge to the culprit — written via the
    # direct-writer path (add_dependency would reject a closed source).
    assert culprit in _caused_by_targets(bug, repo)
    assert rebar.show_ticket(bug, repo_root=repo)["status"] == "closed"
