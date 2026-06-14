"""Axis 2 — over-length comment length guard + truncation-aware convergence.

Bug 6afc-20ee-84e5-4dd5. Jira Cloud's comment body limit is 32,767 chars.
``acli ... comment create`` exits 0 even when Jira rejects an over-length body,
so the add fails silently and the differ re-emits the comment on every pass
(the outbound comment-sync loop).

The fix has two halves that MUST share one truncation function:

  (send path) acli.add_comment truncates the body so it lands; and
  (differ path) outbound_differ._diff_comments truncates the expected local body
    BEFORE the membership test so the previously-truncated-then-landed Jira body
    matches and the diff stops re-emitting.

Hard constraint: the truncated body is NEVER written back to the local store —
the local comment keeps its FULL untruncated body.

This module exercises OBSERVABLE behaviour:
  - the body actually handed to ACLI is within the 32,767-char limit;
  - a second differ pass over the resulting Jira state emits ZERO comment adds
    (convergence);
  - the local ticket's comment still holds the full untruncated body.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"
)

_JIRA_COMMENT_MAX_CHARS = 32767


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load_module("outbound_differ_length_conv", OUTBOUND_DIFFER_PATH)


class StubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


def _make_jira_snapshot_with_comments(jira_key: str, comment_bodies: list[str]) -> dict:
    jira_comments = [{"id": str(100 + i), "body": body} for i, body in enumerate(comment_bodies)]
    return {
        jira_key: {
            "summary": "Some issue",
            "description": "desc",
            "issuetype": "Bug",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
            "comment": {"comments": jira_comments, "total": len(jira_comments)},
        }
    }


def _make_ticket_with_comments(ticket_id: str, comment_bodies: list[str]) -> dict:
    return {
        "ticket_id": ticket_id,
        "title": "Some issue",
        "description": "desc",
        "status": "open",
        "priority": 2,
        "ticket_type": "bug",
        "assignee": "alice",
        "tags": [],
        "comments": [{"body": body} for body in comment_bodies],
        "deps": [],
    }


# A non-excluded (i.e. human-class, NOT a machine marker) over-length body.
# Send-path (add_comment) truncation is covered in
# tests/scripts/test_acli_comment_length_guard.py (that suite's conftest sets
# up the sys.path needed to import rebar_reconciler/acli.py).
_OVERSIZE_BODY = "X" * 38015


# ---------------------------------------------------------------------------
# Convergence (the load-bearing test): pass 1 emits one truncated add; pass 2
# over the resulting Jira state emits ZERO. Local store keeps the full body.
# ---------------------------------------------------------------------------


def test_oversize_comment_converges_over_two_passes(
    outbound_differ: ModuleType,
) -> None:
    """An over-length local comment must converge: pass 1 emits one add, pass 2
    over the resulting Jira state emits zero — and the local store is untouched.
    """
    jira_key = "DIG-7002"
    ticket = _make_ticket_with_comments("local-conv-1", [_OVERSIZE_BODY])
    store = StubBindingStore({"local-conv-1": jira_key})

    # Pass 1: Jira has no comments yet.
    snapshot_1 = _make_jira_snapshot_with_comments(jira_key, [])
    result_1 = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot_1,
        binding_store=store,
    )
    comment_mutations_1 = [m for m in result_1 if m.comments]
    assert len(comment_mutations_1) == 1, "Pass 1 must emit exactly one comment add"
    emitted = comment_mutations_1[0].comments
    assert len(emitted) == 1
    emitted_body = emitted[0]["body"]
    # The applier hands the emitted (decorated) body to add_comment, which
    # truncates it to Jira's limit before it lands. Reproduce that send-path
    # transform here using the SAME shared helper to model what Jira stores.
    comment_limits = _load_module(
        "comment_limits_conv_test",
        REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "comment_limits.py",
    )
    landed_body = comment_limits.truncate_comment_body(emitted_body)
    assert len(landed_body) <= _JIRA_COMMENT_MAX_CHARS, (
        f"Landed body ({len(landed_body)} chars) must be within Jira's limit"
    )

    # Pass 2: Jira now carries exactly the body that landed in pass 1.
    snapshot_2 = _make_jira_snapshot_with_comments(jira_key, [landed_body])
    result_2 = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot_2,
        binding_store=store,
    )
    comment_mutations_2 = [m for m in result_2 if m.comments]
    assert comment_mutations_2 == [], (
        "Pass 2 must emit ZERO comment mutations (convergence). The differ must "
        "apply the SAME truncation to the expected local body before the "
        f"membership test. Got: {[m.comments for m in comment_mutations_2]}"
    )

    # Hard constraint: the local store still holds the FULL untruncated body.
    assert ticket["comments"][0]["body"] == _OVERSIZE_BODY, (
        "Truncation must NEVER be written back to the local ticket store; the "
        "local comment must retain its full untruncated body."
    )
