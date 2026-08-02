"""DC over-length DESCRIPTION convergence (story 79d5, epic 3e73).

Cloud has a two-pass description-convergence suite
(``test_outbound_description_length_convergence.py``); Data Center had none. This
is DC's, as a SEPARATE file rather than a parametrization of Cloud's, because
Cloud's suite hardcodes Cloud-only internals with no DC equivalent — ``ADF_PATH``
and ``_ADF_DESCRIPTION_LIMIT = 32000`` — while DC fits plain characters via
``WikiTextCodec.fit_outbound`` at ``WIKI_DESCRIPTION_LIMIT`` with no ADF
serialization step at all. Cloud's incident-hardened file stays BYTE-UNCHANGED.

Convergence is a TWO-PASS property: the truncated description the adapter sends
must compare equal to the body it reads back, or the differ re-emits the same
update forever. A single-pass assertion cannot establish that — it passes against
a bridge that re-sends the same truncated value on every pass. So each test here
drives the oversized value through two passes and asserts on the PLAN produced by
the second, not on the absence of an exception.

THE BACKEND IS INJECTED EXPLICITLY, AND THAT IS LOAD-BEARING.
``compute_outbound_mutations``' fallback guard is an OR —
``if outbound_mapper is None or inbound_mapper is None or links is None:`` — so
leaving ANY of the three unset fires the block that resolves ALL THREE from
``select_backend(load_config())``, i.e. CLOUD. A DC test driven through that
fallback would fit the description with Cloud's ADF-size fit while reporting DC
convergence. All three are therefore bound to a real ``JiraDataCenterBackend``.

The proof that DC's fit actually ran is VALUE-BASED, which is legitimate here (it
is not for comments — see the sibling comment suite): DC fits PLAIN characters at
32767, Cloud fits ADF-SERIALIZED size at 32000, and ADF's envelope overhead means
Cloud's fit keeps strictly FEWER plain characters. Asserting the landed body is
exactly the instance ceiling therefore FAILS if the config fallback supplied
Cloud's mapper.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler import outbound_differ
from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend
from rebar_reconciler.adapters.jira_family.rich_text import (
    _WIKI_TRUNCATION_SUFFIX,
    WIKI_DESCRIPTION_LIMIT,
)

from ..backend_support import FakeTransport

# Jira Server/Data Center's REAL text-field ceiling, spelled as a LITERAL rather
# than imported from ``rich_text``. That is deliberate: this number models what the
# Jira INSTANCE enforces (``jira.text.field.character.limit``, default 32767 since
# 7.0.0 — JRASERVER-28519), which is a fact about the remote and does not move when
# rebar's own constant does. Asserting against the import instead would make the
# suite self-consistent under a mutated constant and stop detecting the one defect
# it exists to catch: a rebar limit raised above what Jira will actually store.
_JIRA_TEXT_FIELD_CHARACTER_LIMIT = 32767

# Comfortably over DC's ceiling, and a SINGLE line: Cloud's ADF encoder would wrap
# it in one paragraph whose JSON envelope still pushes the serialized size past
# ``_ADF_DESCRIPTION_LIMIT`` (32000) well before 32767 plain chars, so the two
# deployments' fits produce measurably different lengths.
_OVERSIZE_DESC = "X" * 40000


def _as_jira_would_store(body: str) -> str:
    """Model the body the DC instance actually persists.

    Jira enforces its own ceiling on the way in, whatever rebar believes the limit
    to be. Routing the send path's output through this before the second pass is
    what makes convergence a real property rather than a tautology: if rebar's fit
    is loosened, rebar keeps sending a body Jira silently shortens, the two never
    compare equal, and the differ re-emits forever — which is exactly the second
    pass going red below.
    """
    return body[:_JIRA_TEXT_FIELD_CHARACTER_LIMIT]


class StubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_baseline(self, local_id: str) -> None:
        # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
        return None

    def is_pending(self, local_id: str) -> bool:
        return False

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


@pytest.fixture
def dc_backend() -> JiraDataCenterBackend:
    """A real ``JiraDataCenterBackend`` over a stub transport — the DC roles this
    suite injects at all three of ``compute_outbound_mutations``' seams."""
    return JiraDataCenterBackend(transport=FakeTransport(), instance="dc.example.internal")


def _make_jira_snapshot(jira_key: str, description: str) -> dict[str, Any]:
    return {
        jira_key: {
            "summary": "Some issue",
            "description": description,
            "issuetype": "Bug",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
            "comment": {"comments": [], "total": 0},
        }
    }


def _make_ticket(ticket_id: str, description: str) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "title": "Some issue",
        "description": description,
        "status": "open",
        "priority": 2,
        "ticket_type": "bug",
        "assignee": "alice",
        "tags": [],
        "comments": [],
        "deps": [],
    }


def _desc_mutations(result: list[Any]) -> list[Any]:
    return [m for m in result if getattr(m, "fields", None) and "description" in m.fields]


def _compute(
    backend: JiraDataCenterBackend, ticket: dict[str, Any], snapshot: dict[str, Any], store: Any
) -> list[Any]:
    """Run one outbound pass with ALL THREE DC roles injected.

    Omitting any one of them re-arms the OR-guarded fallback in
    ``compute_outbound_mutations``, which resolves the backend from config (Cloud)
    and overwrites the others — so this helper exists to make the three-way
    injection impossible to forget at a call site.
    """
    mutations, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
        outbound_mapper=backend.outbound,
        inbound_mapper=backend.inbound,
        links=backend,
    )
    return mutations


# ---------------------------------------------------------------------------
# The fit itself: DC's plain-character truncation at WIKI_DESCRIPTION_LIMIT.
# ---------------------------------------------------------------------------


def test_dc_description_fit_is_a_plain_character_truncation(
    dc_backend: JiraDataCenterBackend,
) -> None:
    short = "hello"
    fitted_short = dc_backend.outbound.map_fields_to_remote({"description": short})["description"]
    assert fitted_short == short  # under-limit: unchanged

    fitted = dc_backend.outbound.map_fields_to_remote({"description": _OVERSIZE_DESC})[
        "description"
    ]
    # rebar's constant must BE the instance ceiling — not merely self-consistent.
    assert WIKI_DESCRIPTION_LIMIT == _JIRA_TEXT_FIELD_CHARACTER_LIMIT
    # PLAIN characters at DC's ceiling, not ADF-serialized size at Cloud's 32000.
    assert len(fitted) == _JIRA_TEXT_FIELD_CHARACTER_LIMIT
    assert fitted.endswith(_WIKI_TRUNCATION_SUFFIX)
    # Idempotent — a fixed point, which is what makes convergence possible at all.
    assert (
        dc_backend.outbound.map_fields_to_remote({"description": fitted})["description"] == fitted
    )


# ---------------------------------------------------------------------------
# Convergence: pass 1 emits a description update; pass 2 over the landed
# (truncated) Jira state emits ZERO — and the local store keeps the full text.
# ---------------------------------------------------------------------------


def test_oversize_dc_description_converges_over_two_passes(
    dc_backend: JiraDataCenterBackend,
) -> None:
    jira_key = "DIG-9101"
    ticket = _make_ticket("local-dc-desc-1", _OVERSIZE_DESC)
    store = StubBindingStore({"local-dc-desc-1": jira_key})

    # Pass 1: Jira holds a different (short) description -> an update fires.
    snap_1 = _make_jira_snapshot(jira_key, "stale short desc")
    result_1 = _compute(dc_backend, ticket, snap_1, store)
    emitted = _desc_mutations(result_1)
    assert emitted, "Pass 1 must emit a description update"

    landed = emitted[0].fields["description"]

    # VALUE-BASED PROOF that DC's fit ran, not Cloud's. DC keeps exactly the
    # instance's plain-character ceiling; Cloud's ADF fit (32000 SERIALIZED chars)
    # keeps strictly fewer, so this assertion goes red under the config fallback.
    # Compared against the LITERAL ceiling, so it also goes red if rebar's own
    # constant is raised past what Jira will store.
    assert len(landed) == _JIRA_TEXT_FIELD_CHARACTER_LIMIT, (
        "DC must fit the description to Jira's plain-character ceiling; a shorter "
        "value means Cloud's ADF-size fit ran instead of DC's, and a longer one "
        "means rebar's limit was raised above what the instance accepts."
    )
    # The truncation MARKER must survive the round trip, not merely the length.
    assert landed.endswith(_WIKI_TRUNCATION_SUFFIX), (
        "The landed description must carry the visible truncation marker so a Jira "
        "reader can tell the body was shortened by the reconciler."
    )
    assert len(landed) < len(_OVERSIZE_DESC)  # actually truncated

    # Pass 2: Jira now carries the body that actually landed — the emitted value
    # after the INSTANCE's own ceiling has been applied. DC's ``normalize_outbound``
    # is the identity, so no other round-trip transform intervenes.
    snap_2 = _make_jira_snapshot(jira_key, _as_jira_would_store(landed))
    result_2 = _compute(dc_backend, ticket, snap_2, store)
    assert _desc_mutations(result_2) == [], (
        "Pass 2 must emit ZERO description updates (convergence): the differ must "
        "apply the SAME DC fit to the local description before comparing, or the "
        "bridge re-sends the identical truncated body on every pass forever."
    )

    # Hard constraint: the local ticket keeps its FULL untruncated description.
    assert ticket["description"] == _OVERSIZE_DESC
