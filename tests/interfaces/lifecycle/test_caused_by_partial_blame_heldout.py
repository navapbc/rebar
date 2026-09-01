"""Held-out close-path contract for a partial blame failure (ticket 42fa)."""

from __future__ import annotations

import json
from pathlib import Path

import rebar
from rebar._store.ticket_layout import ticket_dir as layout_ticket_dir
from rebar.metrics import blame


def test_partial_blame_failure_writes_no_caused_by_event(rebar_repo, monkeypatch) -> None:
    repo = str(rebar_repo)
    culprit = rebar.create_ticket("task", "culprit change", repo_root=repo)
    bug = rebar.create_ticket("bug", "regression bug", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)

    rebar.set_file_impact(
        bug,
        [
            {"path": "known.py", "reason": "changed"},
            {"path": "missing.py", "reason": "recorded but unavailable"},
        ],
        repo_root=repo,
    )

    monkeypatch.setattr(blame, "_find_fixing_commit", lambda *args: "fix-sha")
    monkeypatch.setattr(blame, "_commit_ticket", lambda *args: culprit)

    def blame_git(repo_root: str, *args: str) -> str | None:
        assert args[0] == "blame"
        return "introducing-sha source line\n" if args[-1] == "known.py" else None

    # Fault injection is at the real Git boundary: one path succeeds and the
    # next fails. The close path, derivation, and event persistence remain real.
    monkeypatch.setattr(blame, "_git", blame_git)

    rebar.transition(
        bug,
        "in_progress",
        "closed",
        close_class="regression",
        repo_root=repo,
    )
    assert rebar.show_ticket(bug, repo_root=repo)["status"] == "closed"
    caused_by_deps = [
        dep
        for dep in rebar.show_ticket(bug, repo_root=repo)["deps"]
        if dep["relation"] == "caused_by"
    ]
    assert caused_by_deps == []

    ticket_dir = Path(layout_ticket_dir(rebar_repo / ".tickets-tracker", bug))
    caused_by_events = []
    for path in ticket_dir.glob("*-LINK.json"):
        event = json.loads(path.read_text(encoding="utf-8"))
        if event.get("data", {}).get("relation") == "caused_by":
            caused_by_events.append(event)
    assert caused_by_events == []
