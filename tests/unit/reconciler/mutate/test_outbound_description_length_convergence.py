"""Sending and diffing descriptions must share the 32,767-character fit so an over-length
value lands and the next pass converges while the local full description remains intact."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
ENGINE = REPO_ROOT / "src" / "rebar" / "reconciler"
OUTBOUND_DIFFER_PATH = ENGINE / "outbound_differ.py"
ADF_PATH = ENGINE / "adapters" / "jira" / "adf.py"

# The budget the fit targets (must match adf._ADF_DESCRIPTION_LIMIT).
_ADF_DESCRIPTION_LIMIT = 32000


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return importlib.import_module("rebar.reconciler.outbound_differ")


@pytest.fixture(scope="module")
def adf() -> ModuleType:
    return importlib.import_module("rebar.reconciler.adapters.jira.adf")


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


def _make_jira_snapshot(jira_key: str, description: str) -> dict:
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


def _make_ticket(ticket_id: str, description: str) -> dict:
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


# Soft-wrap rejoin shrinks serialized ADF, but this 46k body still exceeds Jira's
# limit, exercising the fitter and two-pass convergence.
_OVERSIZE_DESC = ("X" * 30 + "\n") * 1500


# ---------------------------------------------------------------------------
# Pure ADF-aware fit helper
# ---------------------------------------------------------------------------


def test_fit_description_contract(adf: ModuleType) -> None:
    short = "hello"
    assert adf.fit_text_to_adf_limit(short) == short  # under-limit: unchanged

    out = adf.fit_text_to_adf_limit(_OVERSIZE_DESC)
    # The fit is on the ADF representation, not the plain text.
    assert len(json.dumps(adf.text_to_adf(out))) <= _ADF_DESCRIPTION_LIMIT
    assert len(out) < len(_OVERSIZE_DESC)  # actually truncated
    # idempotent: fitting the result again is a no-op (fixed point — load-bearing
    # for convergence).
    assert adf.fit_text_to_adf_limit(out) == out


# ---------------------------------------------------------------------------
# Convergence: pass 1 emits a description update; pass 2 over the truncated Jira
# state emits ZERO — and the local store keeps the full description.
# ---------------------------------------------------------------------------


def test_oversize_description_converges_over_two_passes(
    outbound_differ: ModuleType, adf: ModuleType
) -> None:
    jira_key = "DIG-8001"
    ticket = _make_ticket("local-desc-1", _OVERSIZE_DESC)
    store = StubBindingStore({"local-desc-1": jira_key})

    def _desc_mutations(result):
        return [m for m in result if getattr(m, "fields", None) and "description" in m.fields]

    # Pass 1: Jira holds a different (short) description -> a description update fires.
    snap_1 = _make_jira_snapshot(jira_key, "stale short desc")
    result_1, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket], jira_snapshot=snap_1, binding_store=store
    )
    assert _desc_mutations(result_1), "Pass 1 must emit a description update"

    # The send path fits the description to the ADF limit before it lands; model
    # that with the shared helper.
    # Model the body Jira actually stores by composing the SAME two transforms the
    # send path applies (fit to the ADF limit, then soft-wrap normalization), rather
    # than hardcoding one of them -- otherwise this fixture silently drifts out of
    # step with the send path and the convergence assertion below stops meaning
    # anything. Equivalent to decoding what Jira would return: the encoder rejoins
    # hard-wrapped prose, so the landed body comes back normalized.
    landed = adf.normalize_description(adf.fit_text_to_adf_limit(_OVERSIZE_DESC))
    assert len(json.dumps(adf.text_to_adf(landed))) <= _ADF_DESCRIPTION_LIMIT

    # Pass 2: Jira now carries the truncated body that actually landed.
    snap_2 = _make_jira_snapshot(jira_key, landed)
    result_2, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket], jira_snapshot=snap_2, binding_store=store
    )
    assert _desc_mutations(result_2) == [], (
        "Pass 2 must emit ZERO description updates (convergence): the differ must "
        "apply the SAME truncation to the local description before comparing."
    )

    # Hard constraint: the local ticket keeps its FULL untruncated description.
    assert ticket["description"] == _OVERSIZE_DESC
