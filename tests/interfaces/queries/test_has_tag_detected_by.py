"""``list --has-tag`` and the ``detected_by:`` namespace ∩ bug-type rule.

In-process port of tests/scripts/test-ticket-list-has-tag.sh (the bash engine is
being deleted). The general --has-tag filtering is covered by test_list_filters.py;
this pins the special rule that suite uniquely guarded:

  * A ``detected_by:*`` tag filter AUTO-INTERSECTS with ticket_type == bug — a
    non-bug carrying ``detected_by:tests`` does NOT appear in
    ``--has-tag=detected_by:tests`` results.
  * A non-``detected_by`` tag filter has NO type intersection — a story carrying
    ``regression`` DOES appear in ``--has-tag=regression``.
  * ``--has-tag`` with no matching tickets exits 0 with an empty result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rebar
from rebar import _cli


def _seed(repo: Path) -> tuple[str, str, str]:
    """bug[detected_by:tests, regression], story[detected_by:tests], story[regression]."""
    bug = rebar.create_ticket("bug", "bug detected", repo_root=str(repo))
    rebar.tag(bug, "detected_by:tests", repo_root=str(repo))
    rebar.tag(bug, "regression", repo_root=str(repo))
    story_d = rebar.create_ticket("story", "story detected", repo_root=str(repo))
    rebar.tag(story_d, "detected_by:tests", repo_root=str(repo))
    story_r = rebar.create_ticket("story", "story reg", repo_root=str(repo))
    rebar.tag(story_r, "regression", repo_root=str(repo))
    return bug, story_d, story_r


def _ids(tickets) -> set[str]:
    return {t["ticket_id"] for t in tickets}


def test_bug_with_detected_by_tag_appears(rebar_repo: Path) -> None:
    bug, _story_d, _story_r = _seed(rebar_repo)
    got = _ids(rebar.list_tickets(has_tag="detected_by:tests", repo_root=str(rebar_repo)))
    assert bug in got


def test_non_bug_excluded_from_detected_by_filter(rebar_repo: Path) -> None:
    bug, story_d, _story_r = _seed(rebar_repo)
    got = _ids(rebar.list_tickets(has_tag="detected_by:tests", repo_root=str(rebar_repo)))
    # detected_by:* auto-intersects with bug type: the story is excluded.
    assert story_d not in got
    assert got == {bug}


def test_non_bug_included_for_non_detected_by_tag(rebar_repo: Path) -> None:
    bug, _story_d, story_r = _seed(rebar_repo)
    got = _ids(rebar.list_tickets(has_tag="regression", repo_root=str(rebar_repo)))
    # No type intersection for non-detected_by tags: bug + story both match.
    assert got == {bug, story_r}


def test_no_matching_tickets_exits_0_empty(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(rebar_repo)
    capsys.readouterr()
    rc = _cli.main(["list", "--has-tag=no-such-tag", "--output", "json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == []
