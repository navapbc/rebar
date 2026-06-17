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
