"""RED tests for _diff_comments ADF body normalization.

Historical bug (bug 85a1-f581-2252-4a21): Jira comments are returned with
``body`` as an Atlassian Document Format (ADF) dict, while local comments
store ``body`` as a plain string. The outbound differ's ``_diff_comments``
added Jira bodies to ``set[str]`` without normalization, producing two
defects:

1. **Phase 3+ reconciler crashes** — ``set.add({adf_dict})`` raises
   ``TypeError: unhashable type: 'dict'``. Probe Phase 4 / Phase 5 status &
   delete tests both failed with "cannot use 'dict' as a dict key".
2. **Spurious duplicate comment pushes** — even when the strings would have
   matched, the dict-vs-string equality check always reported them as
   different. Probe Phase 2 ``verify-no-duplicate-comments — found 2 copies``
   regressed because the differ emitted re-add mutations for every comment
   on every pass.

The fix normalizes both sides through ``adf.adf_to_text`` so canonical
plain-text comparison drives both the set membership and the diff verdict.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DIFFER_PATH = (
    REPO_ROOT
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "outbound_differ.py"
)


def _load_differ():
    spec = importlib.util.spec_from_file_location(
        "outbound_differ_comments_test", DIFFER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["outbound_differ_comments_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def differ():
    if not DIFFER_PATH.exists():
        pytest.fail(f"outbound_differ.py not found at {DIFFER_PATH}")
    return _load_differ()


def _adf_paragraph(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
        ],
    }


def _jira_snapshot_with_comments(jira_key: str, comment_bodies_or_dicts) -> dict:
    """Build a jira_snapshot with the correct Jira REST API comment shape.

    Jira REST API places comments at fields["comment"]["comments"] — the outer
    key is "comment", not "comments". Updated from the old incorrect shape to
    match the fix for bug 4572.
    """
    jira_comments = [
        (c if isinstance(c, dict) else {"body": c}) for c in comment_bodies_or_dicts
    ]
    return {
        jira_key: {
            "comment": {"comments": jira_comments, "total": len(jira_comments)},
        }
    }


def test_diff_comments_does_not_crash_on_adf_jira_body(differ):
    """Pre-fix: set.add(adf_dict) raised TypeError. After fix: ADF normalized."""
    ticket = {"comments": [{"body": "Already mirrored"}]}
    jira_snapshot = _jira_snapshot_with_comments(
        "DIG-1", [{"body": _adf_paragraph("Already mirrored")}]
    )
    # Pre-fix this would raise; assertion is that it now succeeds and returns
    # zero mutations (the bodies match after normalization).
    out = differ._diff_comments(ticket, "DIG-1", jira_snapshot)
    assert isinstance(out, list)


def test_diff_comments_dedup_matches_adf_to_plain(differ):
    """When local plain body matches Jira ADF body, no duplicate-push mutation is emitted."""
    ticket = {"comments": [{"body": "Probe outbound comment"}]}
    jira_snapshot = _jira_snapshot_with_comments(
        "DIG-1", [{"body": _adf_paragraph("Probe outbound comment")}]
    )
    out = differ._diff_comments(ticket, "DIG-1", jira_snapshot)
    assert out == [], (
        f"local body matches Jira ADF body after adf_to_text — no diff expected; "
        f"got: {out!r}"
    )


def test_diff_comments_emits_only_genuinely_new(differ):
    """When local has 2 comments and Jira has 1 (ADF), only the new one is emitted.

    Note (bug 85a1, Gap 1): outbound bodies now carry the reconciler marker
    token appended after a paragraph break, so the emitted body is the user
    content plus the marker.
    """
    ticket = {
        "comments": [
            {"body": "Probe outbound comment"},
            {"body": "Second probe comment"},
        ]
    }
    jira_snapshot = _jira_snapshot_with_comments(
        "DIG-1", [{"body": _adf_paragraph("Probe outbound comment")}]
    )
    out = differ._diff_comments(ticket, "DIG-1", jira_snapshot)
    assert len(out) == 1
    assert "Second probe comment" in out[0].get("body", "")
    assert differ.RECONCILER_MARKER in out[0].get("body", "")
    assert out[0].get("action") == "add"


def test_normalize_comment_body_unit(differ):
    """Direct unit test on the normalizer."""
    assert differ._normalize_comment_body("hello") == "hello"
    assert differ._normalize_comment_body(None) == ""
    assert differ._normalize_comment_body(_adf_paragraph("hello")) == "hello"
    assert differ._normalize_comment_body(123) == "123"  # legacy non-string/dict
