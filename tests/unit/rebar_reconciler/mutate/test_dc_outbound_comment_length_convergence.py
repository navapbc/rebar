"""DC over-length COMMENT convergence (story 79d5, epic 3e73).

Cloud has a two-pass comment-convergence suite
(``test_outbound_comment_length_convergence.py``); Data Center had none. This is
DC's, as a SEPARATE file rather than a parametrization of Cloud's, because
Cloud's suite hardcodes ``comment_limits.truncate_comment_body`` — a Cloud-only
module DC has no equivalent of (DC fits via ``WikiTextCodec.fit_outbound``).
Cloud's incident-hardened file stays BYTE-UNCHANGED.

Convergence is a TWO-PASS property: the truncated body the adapter sends must
compare equal to the body it reads back, or the differ re-emits the same add on
every pass. A single-pass assertion cannot establish that, so each test here runs
two passes and asserts on the PLAN the second produces.

WHY THIS SUITE CALLS ``_diff_comments`` DIRECTLY. ``compute_outbound_mutations``
accepts no ``sanitizer`` at all, and forwards none when it calls
``_diff_comments`` — so ``_resolve_sanitizer(None)`` falls back to
``select_backend(load_config()).sanitizer``, i.e. CLOUD's, no matter which mapper
a caller injects. A DC comment-convergence test driven through
``compute_outbound_mutations`` would therefore converge against Cloud's
truncation while reporting DC convergence. ``_diff_comments`` already accepts
``sanitizer=`` (the ticket-21ca injection seam), so DC's path is reachable with NO
production change; threading a sanitizer through ``compute_outbound_mutations`` is
a separate question and deliberately not answered here.

WHY THE PROOF-OF-PATH IS AN IDENTITY/RECORDING CHECK AND NOT A VALUE CHECK. A
value-based discriminator is IMPOSSIBLE for comments: DC's
``WikiTextCodec.fit_outbound`` and Cloud's ``comment_limits.truncate_comment_body``
truncate at the SAME 32767 limit, compute ``keep`` the same way, and append a
BYTE-IDENTICAL suffix (" … [truncated by reconciler]"). Any assertion on the
resulting string passes under EITHER sanitizer — a test that cannot fail. So this
suite wraps the injected DC sanitizer in a recorder and asserts the recorder
observed the call, and that the object it delegates to IS the DC backend's own
sanitizer. That is what goes red when the injection is removed.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler import outbound_comments
from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend
from rebar_reconciler.adapters.jira_family.rich_text import (
    _WIKI_TRUNCATION_SUFFIX,
    WIKI_DESCRIPTION_LIMIT,
)

from ..backend_support import FakeTransport

# Jira Server/Data Center's REAL text-field ceiling, spelled as a LITERAL rather
# than imported. ``jira.text.field.character.limit`` governs Description,
# Environment, COMMENTS and text custom fields as ONE property, and has defaulted
# to 32767 since Jira 7.0.0 (JRASERVER-28519). This models what the INSTANCE
# enforces, which does not move when rebar's own constant does — asserting against
# the import instead would leave the suite self-consistent under a loosened limit.
_JIRA_TEXT_FIELD_CHARACTER_LIMIT = 32767

# A non-excluded (i.e. human-class, NOT a machine marker) over-length body.
_OVERSIZE_BODY = "X" * 38015


def _as_jira_would_store(body: str) -> str:
    """Model the body the DC instance actually persists.

    Jira enforces its own ceiling on the way in whatever rebar believes the limit
    to be. Routing the send path's output through this before pass 2 is what makes
    convergence a real property: if rebar's fit is loosened, rebar keeps posting a
    body Jira silently shortens, the two never compare equal, and the differ
    re-emits the add forever.
    """
    return body[:_JIRA_TEXT_FIELD_CHARACTER_LIMIT]


class RecordingSanitizer:
    """Delegating wrapper around a real ``FieldSanitizer`` that records the
    comment-fit calls it saw.

    This is the suite's proof-of-path. ``_diff_comments`` compares each local body
    against Jira's via ``sanitizer.fit_comment``; if the explicit ``sanitizer=``
    injection is dropped, ``_resolve_sanitizer(None)`` silently substitutes the
    CONFIGURED backend's sanitizer and this recorder never runs — so an empty
    ``fit_comment_calls`` is exactly the signal that the test was exercising the
    wrong deployment.
    """

    def __init__(self, wrapped: Any) -> None:
        self.wrapped = wrapped
        self.fit_comment_calls: list[str] = []

    def fit_comment(self, body: str) -> str:
        self.fit_comment_calls.append(body)
        return self.wrapped.fit_comment(body)

    def __getattr__(self, name: str) -> Any:
        # Everything else (sanitize_label/summary/description/comment) delegates
        # untouched, so the wrapper cannot change behaviour — only observe it.
        return getattr(self.wrapped, name)


@pytest.fixture
def dc_backend() -> JiraDataCenterBackend:
    """A real ``JiraDataCenterBackend`` over a stub transport — the source of the
    DC sanitizer and inbound mapper this suite injects."""
    return JiraDataCenterBackend(transport=FakeTransport(), instance="dc.example.internal")


def _make_jira_snapshot_with_comments(jira_key: str, comment_bodies: list[str]) -> dict[str, Any]:
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


def _make_ticket_with_comments(ticket_id: str, comment_bodies: list[str]) -> dict[str, Any]:
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


def _diff(
    backend: JiraDataCenterBackend,
    recorder: RecordingSanitizer,
    ticket: dict[str, Any],
    jira_key: str,
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    """One comment-diff pass with DC's sanitizer and inbound mapper injected.

    BOTH are explicit: each has its own ``_resolve_*`` fallback to
    ``select_backend(load_config())``, so neither may be left to config.
    """
    return outbound_comments._diff_comments(
        ticket,
        jira_key,
        snapshot,
        inbound_mapper=backend.inbound,
        sanitizer=recorder,
    )


# ---------------------------------------------------------------------------
# The fit itself, and the ceiling's provenance.
# ---------------------------------------------------------------------------


def test_dc_comment_fit_targets_the_documented_instance_ceiling(
    dc_backend: JiraDataCenterBackend,
) -> None:
    """DC binds comments and descriptions to ONE constant, which is faithful to
    DC: ``jira.text.field.character.limit`` governs both as a single property."""
    assert WIKI_DESCRIPTION_LIMIT == _JIRA_TEXT_FIELD_CHARACTER_LIMIT

    sanitizer = dc_backend.sanitizer
    assert sanitizer.fit_comment("hello") == "hello"  # under-limit: unchanged

    fitted = sanitizer.fit_comment(_OVERSIZE_BODY)
    assert len(fitted) == _JIRA_TEXT_FIELD_CHARACTER_LIMIT
    assert fitted.endswith(_WIKI_TRUNCATION_SUFFIX)
    # Idempotent — the fixed point convergence depends on.
    assert sanitizer.fit_comment(fitted) == fitted


# ---------------------------------------------------------------------------
# Convergence: pass 1 emits one truncated add; pass 2 over the landed Jira state
# emits ZERO. Local store keeps the full body.
# ---------------------------------------------------------------------------


def test_oversize_dc_comment_converges_over_two_passes(
    dc_backend: JiraDataCenterBackend,
) -> None:
    jira_key = "DIG-9102"
    ticket = _make_ticket_with_comments("local-dc-conv-1", [_OVERSIZE_BODY])
    recorder = RecordingSanitizer(dc_backend.sanitizer)

    # Pass 1: Jira has no comments yet.
    snap_1 = _make_jira_snapshot_with_comments(jira_key, [])
    mutations_1 = _diff(dc_backend, recorder, ticket, jira_key, snap_1)
    assert len(mutations_1) == 1, "Pass 1 must emit exactly one comment add"

    # IDENTITY / RECORDING PROOF that DC's sanitizer is the one that ran. A value
    # assertion cannot establish this — DC's and Cloud's comment fits produce
    # byte-identical output — so the proof is that THIS object was consulted, and
    # that it delegates to the DC backend's own sanitizer.
    assert recorder.wrapped is dc_backend.sanitizer
    assert recorder.fit_comment_calls, (
        "The injected DC sanitizer was never consulted: _diff_comments fell back to "
        "select_backend(load_config()).sanitizer, so this pass compared bodies with "
        "CLOUD's fit while claiming to test DC."
    )

    # The applier hands the emitted (marker-decorated) body to the DC transport,
    # which fits it before it lands; then the instance applies its own ceiling.
    emitted_body = mutations_1[0]["body"]
    landed_body = _as_jira_would_store(dc_backend.sanitizer.fit_comment(emitted_body))
    assert len(landed_body) <= _JIRA_TEXT_FIELD_CHARACTER_LIMIT
    # The truncation MARKER must survive the round trip, not merely the length.
    assert landed_body.endswith(_WIKI_TRUNCATION_SUFFIX), (
        "The landed comment must carry the visible truncation marker so a Jira "
        "reader can tell the body was shortened by the reconciler."
    )

    # Pass 2: Jira now carries exactly the body that landed in pass 1.
    calls_before = len(recorder.fit_comment_calls)
    snap_2 = _make_jira_snapshot_with_comments(jira_key, [landed_body])
    mutations_2 = _diff(dc_backend, recorder, ticket, jira_key, snap_2)
    assert mutations_2 == [], (
        "Pass 2 must emit ZERO comment mutations (convergence). The differ must "
        "apply the SAME DC fit to the expected local body before the membership "
        f"test, or the bridge re-posts forever. Got: {mutations_2}"
    )
    assert len(recorder.fit_comment_calls) > calls_before, (
        "Pass 2's zero-mutation result must come from the INJECTED DC sanitizer "
        "having run, not from it being bypassed."
    )

    # Hard constraint: the local store still holds the FULL untruncated body.
    assert ticket["comments"][0]["body"] == _OVERSIZE_BODY, (
        "Truncation must NEVER be written back to the local ticket store; the "
        "local comment must retain its full untruncated body."
    )
