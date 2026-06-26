"""RED→GREEN: inbound comment sync must read the REAL Jira snapshot shape.

Bug 0ee6-58b5-f9bf-4531: the inbound differ read Jira comments from
``jira_fields["comments"]`` (a flat list under the key ``comments``), but the
production snapshot never carries that key. The fetcher enriches each snapshot
entry with the Jira REST ``comment`` field — a NESTED dict
``{"comments": [...], "total": N}`` under the singular key ``comment`` (see
``fetcher.py`` ``get_comment_map`` enrichment and ``outbound_differ`` which
reads ``jira_issue["comment"]["comments"]``). Because the inbound differ read
the wrong key, ``with_comments`` was structurally 0 fleet-wide and Jira-origin
comments were never mirrored inbound.

These tests feed the REAL Jira-REST snapshot shape (``comment`` nested) — the
shape the fetcher actually produces — rather than the fictional flat
``comments`` shape the legacy unit tests used. They exercise the mechanism end
to end through ``compute_inbound_mutations``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
INBOUND_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "inbound_differ.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def inbound():
    return _load("inbound_differ_comment_shape_test", INBOUND_PATH)


class _BindingStore:
    """Minimal BindingStore: maps a single Jira key to a local id."""

    def __init__(self, mapping: dict[str, str]):
        self._fwd = dict(mapping)

    def get_local_id(self, jira_key: str) -> str | None:
        return self._fwd.get(jira_key)


def _adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _adf_with_marker(text: str, marker: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]},
            {"type": "paragraph", "content": [{"type": "text", "text": marker}]},
        ],
    }


def _real_snapshot_entry(comments: list[dict]) -> dict:
    """Build a snapshot entry in the REAL Jira-REST shape the fetcher produces.

    The fetcher merges ``get_comment_map`` output into ``snapshot[key]["comment"]``
    as ``{"comments": [...], "total": N}`` — the singular ``comment`` key, nested.
    """
    return {"comment": {"comments": comments, "total": len(comments)}}


# --- differ-level (the mechanism) --------------------------------------------


def test_diff_reads_real_comment_snapshot_shape(inbound):
    """A Jira comment carried under the REAL ``comment`` key yields an inbound add."""
    entry = _real_snapshot_entry([{"id": "10001", "body": _adf("Human-written from Jira")}])
    local_ticket = {"comments": []}
    out = inbound._diff_comments_inbound(entry, local_ticket)
    assert len(out) == 1, (
        f"a Jira-origin comment in the real snapshot shape must be mirrored inbound; got {out!r}"
    )
    assert out[0]["action"] == "add"
    assert out[0]["body"] == "Human-written from Jira"
    assert out[0]["jira_comment_id"] == "10001"


def test_diff_real_shape_filters_echo(inbound):
    """An outbound echo (marker) in the real snapshot shape is NOT re-imported."""
    entry = _real_snapshot_entry(
        [{"id": "10002", "body": _adf_with_marker("our echo", inbound.RECONCILER_MARKER)}]
    )
    out = inbound._diff_comments_inbound(entry, {"comments": []})
    assert out == [], f"reconciler echo must not be re-imported; got {out!r}"


def test_diff_real_shape_skips_already_mirrored(inbound):
    """A Jira comment already mirrored (by jira_comment_id) is skipped."""
    entry = _real_snapshot_entry([{"id": "10003", "body": _adf("already here")}])
    local_ticket = {"comments": [{"body": "already here", "jira_comment_id": "10003"}]}
    out = inbound._diff_comments_inbound(entry, local_ticket)
    assert out == []


# --- full inbound path (compute_inbound_mutations) ---------------------------


def test_compute_inbound_mirrors_jira_origin_comment(inbound):
    """End to end: a Jira-origin comment in the real snapshot shape becomes an
    InboundMutation carrying the comment add — and the echo does not."""
    snapshot = {
        "REB-5": _real_snapshot_entry(
            [
                {"id": "20001", "body": _adf("jira-origin")},
                {"id": "20002", "body": _adf_with_marker("local echo", inbound.RECONCILER_MARKER)},
            ]
        )
    }
    binding = _BindingStore({"REB-5": "0241-c6c1-0a20-491b"})
    local_by_id = {
        "0241-c6c1-0a20-491b": {
            "ticket_id": "0241-c6c1-0a20-491b",
            "title": "bound ticket",
            "comments": [
                # the local-origin comment already mirrored to Jira (id 20002)
                {"body": "local echo", "jira_comment_id": "20002"},
            ],
        }
    }
    mutations, _suppressed = inbound.compute_inbound_mutations(snapshot, binding, local_by_id)
    assert len(mutations) == 1, f"expected one inbound mutation; got {mutations!r}"
    m = mutations[0]
    assert m.local_id == "0241-c6c1-0a20-491b"
    assert len(m.comments) == 1, (
        f"only the genuinely Jira-origin comment should be mirrored inbound "
        f"(echo filtered); got {m.comments!r}"
    )
    assert m.comments[0]["body"] == "jira-origin"
    assert m.comments[0]["jira_comment_id"] == "20001"
