"""WS5a: full-text search (replay-derived), across library / CLI / MCP."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import rebar


def _cli_search(repo: Path, *args: str) -> list:
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "search", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**_env(repo)},
    )
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


def _env(repo: Path) -> dict:
    import os

    e = dict(os.environ)
    e["REBAR_ROOT"] = str(repo)
    return e


def _ids(results) -> set:
    return {t["ticket_id"] for t in results}


def test_search_matches_title_description_comments_tags(rebar_repo: Path) -> None:
    hit_title = rebar.create_ticket("task", "Implement zephyr login")
    hit_desc = rebar.create_ticket("task", "other", description="needs zephyr handling")
    hit_comment = rebar.create_ticket("task", "third")
    rebar.comment(hit_comment, "this mentions zephyr in a comment")
    hit_tag = rebar.create_ticket("task", "fourth")
    rebar.tag(hit_tag, "zephyr")
    miss = rebar.create_ticket("task", "unrelated work")

    results = rebar.search("zephyr")
    ids = _ids(results)
    assert {hit_title, hit_desc, hit_comment, hit_tag} <= ids
    assert miss not in ids


def test_search_matches_ticket_id_and_alias(rebar_repo: Path) -> None:
    # The two identifiers `show` accepts directly must also be discoverable by
    # `search` (the exact footgun: a user pastes an id/alias and gets nothing).
    tid = rebar.create_ticket("task", "some work", return_alias=True)
    ticket_id, alias = tid["id"], tid["alias"]
    rebar.create_ticket("task", "noise")

    assert ticket_id in _ids(rebar.search(ticket_id))
    assert ticket_id in _ids(rebar.search(alias))
    # CLI parity for the id path.
    assert ticket_id in _ids(_cli_search(rebar_repo, ticket_id))


def test_search_matches_bound_jira_key(rebar_repo: Path) -> None:
    # A ticket bound to a Jira key is discoverable by `rebar search <JIRA-KEY>`,
    # mirroring `rebar show <JIRA-KEY>` resolving it. The binding lives in the
    # reconciler's binding store reverse index, not in the reduced state — the
    # enrichment seam under test.
    tid = rebar.create_ticket("task", "jira-bound work")
    rebar.create_ticket("task", "noise")

    tracker = Path(rebar.config.tracker_dir(rebar_repo))
    bridge = tracker / ".bridge_state"
    bridge.mkdir(parents=True, exist_ok=True)
    (bridge / "bindings.json").write_text(
        json.dumps({"reverse": {"REB-1654": tid}}), encoding="utf-8"
    )

    # Both `show` and `search` resolve the same input, case-insensitively.
    assert tid in _ids(rebar.search("REB-1654"))
    assert tid in _ids(rebar.search("reb-1654"))
    assert tid in _ids(_cli_search(rebar_repo, "REB-1654"))


def test_search_filters_and_and_terms(rebar_repo: Path) -> None:
    a = rebar.create_ticket("bug", "alpha beta gamma")
    b = rebar.create_ticket("task", "alpha only")
    # AND semantics: both terms must be present.
    assert _ids(rebar.search("alpha gamma")) == {a}
    # type filter.
    assert _ids(rebar.search("alpha", ticket_type="task")) == {b}


def test_search_parity_library_cli(rebar_repo: Path) -> None:
    """Library and CLI return identical search results (MCP parity is covered by
    the adapter-driven test in test_parity.py)."""
    tid = rebar.create_ticket("task", "parity widget search")
    rebar.create_ticket("task", "noise")
    lib = _ids(rebar.search("widget"))
    cli = _ids(_cli_search(rebar_repo, "widget"))
    assert lib == cli == {tid}
