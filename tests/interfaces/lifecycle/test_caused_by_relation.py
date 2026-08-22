"""Happy-path contract for the additive `caused_by` link relation (ticket 0b96).

Tier: interface (library facade over a real temp store). This is the happy-path
oracle — it pins the one new capability: `caused_by` is an accepted 7th relation
that round-trips into a ticket's deps. Directionality / non-cycle / back-compat
contracts live in the held-out companion.
"""

from __future__ import annotations

import subprocess

import pytest

import rebar

pytestmark = pytest.mark.interface


def _rels(tid: str, repo: str) -> list[tuple[str, str]]:
    deps = rebar.show_ticket(tid, repo_root=repo)["deps"]
    return [(d["target_id"], d["relation"]) for d in deps]


def test_caused_by_link_round_trips(rebar_repo) -> None:
    repo = str(rebar_repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    culprit = rebar.create_ticket("task", "the culprit change", repo_root=repo)
    # The culprit must have an introducing commit: an evidence-less caused_by
    # target is refused at link time (story dormant-fibre-pterosaurs).
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            f"the culprit change\n\nrebar-ticket: {culprit}",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    rebar.link(bug, culprit, "caused_by", repo_root=repo)

    # The relation is accepted and appears verbatim on the source ticket's deps.
    assert (culprit, "caused_by") in _rels(bug, repo)
