"""Tests for Gap 1: inbound comment propagation.

The design (validated by ``probe_gap1_inbound_comments.sh`` against live
Jira earlier in the session):

1. **Outbound emits a marker token** — ``<!-- rebar:reconciler-echo -->`` is
   appended to every outbound comment body so the inbound pass can
   identify (and filter) our own echoes.
2. **Inbound set-diff by Jira comment id** — local comments carry an
   optional ``jira_comment_id`` field; the inbound differ skips any Jira
   comment whose id is already in the local set.
3. **ADF→text normalization** — Jira returns comment bodies as ADF dicts;
   the differ converts via ``adf.adf_to_text`` so the marker filter works
   on plain text.
4. **Emit add mutations carrying jira_comment_id** — the applier writes
   the body locally AND persists the binding so future passes recognise
   the comment as already-mirrored.

These tests assert each behavior in isolation. End-to-end probe coverage
lands separately when the applier-side write path is wired.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INBOUND_PATH = (
    REPO_ROOT
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "inbound_differ.py"
)
OUTBOUND_PATH = (
    REPO_ROOT
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "outbound_differ.py"
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def inbound():
    return _load("inbound_differ_gap1_test", INBOUND_PATH)


@pytest.fixture(scope="module")
def outbound():
    return _load("outbound_differ_gap1_test", OUTBOUND_PATH)


def _adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
        ],
    }


def _adf_with_marker(text: str, marker: str) -> dict:
    """ADF with two paragraphs: body, then the marker."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]},
            {"type": "paragraph", "content": [{"type": "text", "text": marker}]},
        ],
    }


# --- inbound side -------------------------------------------------------


def test_inbound_emits_new_jira_comment(inbound):
    """A Jira comment whose id is not known locally is emitted as add."""
    jira_fields = {
        "comments": [
            {"id": "10001", "body": _adf("Human-written comment from Jira")},
        ]
    }
    local_ticket = {"comments": []}
    out = inbound._diff_comments_inbound(jira_fields, local_ticket)
    assert len(out) == 1
    assert out[0]["action"] == "add"
    assert out[0]["body"] == "Human-written comment from Jira"
    assert out[0]["jira_comment_id"] == "10001"


def test_inbound_filters_outbound_echo_by_marker(inbound):
    """A Jira comment containing the reconciler marker is OUR echo — skip."""
    jira_fields = {
        "comments": [
            {
                "id": "10002",
                "body": _adf_with_marker(
                    "Probe outbound comment", inbound.RECONCILER_MARKER
                ),
            },
        ]
    }
    local_ticket = {"comments": [{"body": "Probe outbound comment"}]}  # no jira_comment_id yet
    out = inbound._diff_comments_inbound(jira_fields, local_ticket)
    assert out == [], (
        f"comment with reconciler marker is an outbound echo — must NOT be "
        f"emitted as inbound; got {out!r}"
    )


def test_inbound_skips_already_mirrored_by_id(inbound):
    """A Jira comment whose id is in local ticket's jira_comment_id set is skipped."""
    jira_fields = {
        "comments": [
            {"id": "10003", "body": _adf("This was already mirrored")},
        ]
    }
    local_ticket = {
        "comments": [
            {"body": "This was already mirrored", "jira_comment_id": "10003"},
        ]
    }
    out = inbound._diff_comments_inbound(jira_fields, local_ticket)
    assert out == []


def test_inbound_emits_only_genuinely_new(inbound):
    """Mixed jira comments: 1 known + 1 echo + 1 truly new → emit only the new one."""
    jira_fields = {
        "comments": [
            {"id": "10010", "body": _adf("Already mirrored body")},
            {
                "id": "10011",
                "body": _adf_with_marker("Our outbound", inbound.RECONCILER_MARKER),
            },
            {"id": "10012", "body": _adf("Fresh Jira comment")},
        ]
    }
    local_ticket = {
        "comments": [
            {"body": "Already mirrored body", "jira_comment_id": "10010"},
        ]
    }
    out = inbound._diff_comments_inbound(jira_fields, local_ticket)
    assert len(out) == 1
    assert out[0]["body"] == "Fresh Jira comment"
    assert out[0]["jira_comment_id"] == "10012"


def test_inbound_handles_string_body_legacy(inbound):
    """Legacy Jira snapshots may carry comment body as plain string (not ADF)."""
    jira_fields = {
        "comments": [
            {"id": "20001", "body": "Plain string body"},
        ]
    }
    local_ticket = {"comments": []}
    out = inbound._diff_comments_inbound(jira_fields, local_ticket)
    assert out == [
        {"action": "add", "body": "Plain string body", "jira_comment_id": "20001"}
    ]


def test_inbound_skips_blank_bodies(inbound):
    """Empty/whitespace-only Jira comments are skipped (no signal)."""
    jira_fields = {
        "comments": [
            {"id": "30001", "body": _adf("")},
            {"id": "30002", "body": _adf("   ")},
        ]
    }
    out = inbound._diff_comments_inbound(jira_fields, {"comments": []})
    assert out == []


# --- outbound side ------------------------------------------------------


def test_outbound_decorates_with_marker(outbound):
    """_decorate_outbound_comment appends the marker on a separate paragraph."""
    decorated = outbound._decorate_outbound_comment("Hello world")
    assert decorated.endswith(outbound.RECONCILER_MARKER)
    assert "Hello world" in decorated
    assert "\n\n" in decorated  # paragraph break for ADF round-trip


def test_outbound_diff_emits_decorated_body(outbound):
    """When the differ emits a new outbound comment, the body carries the marker."""
    ticket = {"comments": [{"body": "First probe comment"}]}
    # Use the correct Jira snapshot shape: comment field with nested comments list
    # (bug 4572 fix: key is "comment" not "comments").
    jira_snapshot = {"DIG-1": {"comment": {"comments": [], "total": 0}}}
    out = outbound._diff_comments(ticket, "DIG-1", jira_snapshot)
    assert len(out) == 1
    assert outbound.RECONCILER_MARKER in out[0]["body"]
    assert "First probe comment" in out[0]["body"]


def test_outbound_normalize_strips_marker_for_dedup(outbound):
    """A Jira comment with our marker normalizes to the user body — dedup catches it."""
    ticket = {"comments": [{"body": "First probe comment"}]}
    # Use the correct Jira snapshot shape: comment field with nested comments list
    # (bug 4572 fix: key is "comment" not "comments").
    jira_snapshot = {
        "DIG-1": {
            "comment": {
                "comments": [
                    {
                        "body": {
                            "type": "doc",
                            "version": 1,
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": "First probe comment"}
                                    ],
                                },
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": outbound.RECONCILER_MARKER}
                                    ],
                                },
                            ],
                        }
                    }
                ],
                "total": 1,
            }
        }
    }
    out = outbound._diff_comments(ticket, "DIG-1", jira_snapshot)
    assert out == [], (
        f"local body matches Jira (echo stripped) — no diff expected; got {out!r}"
    )
