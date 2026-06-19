"""Tag write-surface over TAG_DELTA (epic P2.3 / WU-2).

The CLI/library/MCP tag surface emits convergent TAG_DELTA deltas instead of a
whole-field EDIT.tags clobber. Pinned here at the library + CLI tier:

  * leaf tag/untag and edit --add-tag/--remove-tag emit TAG_DELTA (no EDIT.tags);
  * --set-tags is compiled to a delta (add-wins) vs observed state;
  * mutual-exclusion + same-tag + --tags-removed errors;
  * the deprecated edit(tags=) alias still works (as a set);
  * tag-name validation; no-op suppression.
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

import pytest

import rebar
from rebar._commands import composer


def _seed(repo: Path, tags=None) -> str:
    return rebar.create_ticket(
        "task",
        "Tag surface",
        description="Body.\n\n## Acceptance Criteria\n- [ ] a",
        tags=tags,
        repo_root=str(repo),
    )


def _tags(repo: Path, tid: str) -> list[str]:
    return sorted(rebar.show_ticket(tid, repo_root=str(repo))["tags"])


def _event_types(repo: Path, tid: str) -> set[str]:
    types = set()
    for f in glob.glob(os.path.join(str(repo), ".tickets-tracker", tid, "*.json")):
        with open(f) as fh:
            types.add(json.load(fh).get("event_type"))
    return types


def _edit(repo: Path, tid: str, *flags: str) -> int:
    """Drive the CLI parse layer for `edit` directly; returns the exit code."""
    return composer.edit_cli([tid, *flags], repo_root=str(repo))


# ── leaf tag/untag emit TAG_DELTA, idempotent ────────────────────────────────
def test_tag_untag_emit_tag_delta(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    rebar.tag(tid, "blue", repo_root=str(rebar_repo))
    rebar.tag(tid, "blue", repo_root=str(rebar_repo))  # idempotent: no second event
    rebar.tag(tid, "red", repo_root=str(rebar_repo))
    rebar.untag(tid, "blue", repo_root=str(rebar_repo))
    assert _tags(rebar_repo, tid) == ["red"]
    types = _event_types(rebar_repo, tid)
    assert "TAG_DELTA" in types
    # No whole-field EDIT was written for the tag mutations.
    assert "EDIT" not in types


# ── edit --add-tag / --remove-tag ────────────────────────────────────────────
def test_edit_add_remove_tag(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo, tags=["keep"])
    assert _edit(rebar_repo, tid, "--add-tag=a,b", "--remove-tag=keep") == 0
    assert _tags(rebar_repo, tid) == ["a", "b"]


# ── --set-tags compiles to a delta (add-wins) ────────────────────────────────
def test_set_tags_compiles_to_delta(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo, tags=["a", "b", "c"])
    assert _edit(rebar_repo, tid, "--set-tags=a,x") == 0
    assert _tags(rebar_repo, tid) == ["a", "x"]
    # set is delta-driven, never a whole-field EDIT.tags
    assert "EDIT" not in _event_types(rebar_repo, tid)


def test_set_tags_empty_clears_observed(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo, tags=["a", "b"])
    assert _edit(rebar_repo, tid, "--set-tags=") == 0
    assert _tags(rebar_repo, tid) == []


# ── mutual exclusion + same-tag + dropped --tags ─────────────────────────────
def test_set_with_add_is_error(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    assert _edit(rebar_repo, tid, "--set-tags=a", "--add-tag=b") == 1


def test_same_tag_add_and_remove_is_error(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    assert _edit(rebar_repo, tid, "--add-tag=x", "--remove-tag=x") == 1


def test_tags_flag_removed_from_edit(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    assert _edit(rebar_repo, tid, "--tags=foo") == 1
    # and it did not silently set anything
    assert _tags(rebar_repo, tid) == []


def test_control_char_tag_rejected(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    assert _edit(rebar_repo, tid, "--add-tag=ok\x01bad") == 1


# ── deprecated edit(tags=) alias behaves as a (convergent) set ────────────────
def test_deprecated_tags_alias_sets(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo, tags=["old"])
    rebar.edit_ticket(tid, tags=["only", "two"], repo_root=str(rebar_repo))
    assert _tags(rebar_repo, tid) == ["only", "two"]
    assert "EDIT" not in _event_types(rebar_repo, tid)


# ── a tag-only edit emits only TAG_DELTA; a mixed edit emits EDIT + TAG_DELTA ──
def test_mixed_edit_emits_both_events(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    assert _edit(rebar_repo, tid, "--title=New", "--add-tag=t") == 0
    types = _event_types(rebar_repo, tid)
    assert {"EDIT", "TAG_DELTA"} <= types
    st = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert st["title"] == "New" and "t" in st["tags"]
