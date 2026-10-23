"""Sending and diffing over-length comments must share one fitter so the landed Jira body
matches the next-pass dedup key while the local full body remains untruncated."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
OUTBOUND_DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "outbound_differ.py"

_JIRA_COMMENT_MAX_CHARS = 32767


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return importlib.import_module("rebar.reconciler.outbound_differ")


class StubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_baseline(self, local_id):
        # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
        return None

    def is_pending(self, local_id):
        return False

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
# up the sys.path needed to import rebar.reconciler/acli.py).
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
    result_1, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot_1,
        binding_store=store,
    )
    comment_mutations_1 = [m for m in result_1 if m.comments]
    assert len(comment_mutations_1) == 1, "Pass 1 must emit exactly one comment add"
    emitted = comment_mutations_1[0].comments
    assert len(emitted) == 1
    emitted_body = emitted[0]["body"]
    # Model the landed decorated body with the Cloud send path's serialized-ADF
    # fitter and marker budget. Plain truncation would falsely pass convergence.
    from rebar.reconciler.adapters.jira.rich_text_codec import AdfCodec
    from rebar.reconciler.adapters.jira_family.rich_text import cutover_clients
    from rebar.reconciler.outbound_comments import fit_preserving_marker

    codec = AdfCodec(rich="cloud" in cutover_clients())
    landed_body = fit_preserving_marker(emitted_body, codec.fit_outbound)
    assert len(landed_body) <= _JIRA_COMMENT_MAX_CHARS, (
        f"Landed body ({len(landed_body)} chars) must be within Jira's limit"
    )

    # Pass 2: Jira now carries exactly the body that landed in pass 1.
    snapshot_2 = _make_jira_snapshot_with_comments(jira_key, [landed_body])
    result_2, _ = outbound_differ.compute_outbound_mutations(
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
