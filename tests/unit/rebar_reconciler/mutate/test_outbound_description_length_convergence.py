"""Over-length description guard + truncation-aware convergence (bug 626d follow-up).

Jira's description field shares the 32,767-char hard limit of comments. ACLI
surfaces an over-length description as a create/edit FAILURE that aborts the whole
reconciler pass (observed during the REB cutover: a 46,271-char epic description
killed a live pass after 154 successful creates).

The fix has two halves that MUST share one truncation function:

  (send path) acli create/update truncates the description so it lands; and
  (differ path) outbound_differ._diff_fields truncates the expected local value
    BEFORE comparison so the previously-truncated-then-landed Jira body matches and
    the diff stops re-emitting.

Hard constraint: the truncated description is NEVER written back to the local store.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
ENGINE = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"
OUTBOUND_DIFFER_PATH = ENGINE / "outbound_differ.py"
ADF_PATH = ENGINE / "adf.py"

# The budget the fit targets (must match adf._ADF_DESCRIPTION_LIMIT).
_ADF_DESCRIPTION_LIMIT = 32000


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load_module("outbound_differ_desc_conv", OUTBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def adf() -> ModuleType:
    return _load_module("adf_desc_conv", ADF_PATH)


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


# A MULTI-LINE oversize description: text_to_adf wraps each line in its own
# paragraph node, so the ADF inflates far past the plain-text length — the real
# failure mode (a 46k-char epic serialized to ~50k ADF). 1,500 short lines ⇒ ~46k
# plain but ADF well over the limit, so the fit must cut to far fewer plain chars.
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
    landed = adf.fit_text_to_adf_limit(_OVERSIZE_DESC)
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
