"""Insignificant whitespace must not stale a signed plan review (bug 2be7).

The material fingerprint exists so an attestation cannot survive a *substantive* plan
change. Whitespace at the end of a line, or at the document's boundary, carries no plan
substance — yet before this change any such difference moved the composite hash and the gate
reported ``stale-material``. That was routinely hit by the gate's own required close
workflow: the 433c precheck requires editing the description to tick every AC box, and the
documented CLI path for that (``--description="$(cat file)"``) strips the trailing newline as
a shell artifact, forcing a full LLM re-review for a byte that renders as nothing.

The 330c precedent already establishes the shape — AC checkbox STATE is normalized out of the
fingerprint before hashing — so this is the same mechanism extended, not a parallel one.

These tests pin BOTH halves of the contract, because a canonicalization that is too wide
silently destroys the gate's guarantee:

* whitespace-only edits keep the fingerprint identical and the attestation ``certified``;
* every semantically meaningful edit — including a change of *leading* indentation, which
  restructures markdown list nesting — still moves the fingerprint and still reads
  ``stale-material``;
* the pre-330c legacy algorithm (``normalize_checkboxes=False``) still hashes the RAW
  description, so the 96d1 grandfather keeps reproducing it byte-exactly.
"""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import attest
from rebar.llm.plan_review.attest import compute_validity
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.pass1 import material_fingerprint

pytestmark = pytest.mark.unit

_DESC = (
    "## Approach\n"
    "Do the thing.\n"
    "\n"
    "## Acceptance Criteria\n"
    "- [ ] the thing is done\n"
    "  - [ ] the nested sub-item is done\n"
)

# Edits that change no word, no ordering and no markdown nesting. Each renders identically.
_WHITESPACE_ONLY = {
    "trailing newline stripped": _DESC.rstrip("\n"),
    "extra trailing newline": _DESC + "\n",
    "trailing blank lines": _DESC + "\n\n\n",
    "leading blank line": "\n" + _DESC,
    "trailing space on a line": _DESC.replace("Do the thing.", "Do the thing.  "),
    "trailing tab on a line": _DESC.replace("Do the thing.", "Do the thing.\t"),
    "CRLF line endings": _DESC.replace("\n", "\r\n"),
    "whitespace-only separator line": _DESC.replace("\n\n", "\n   \n"),
}

# Edits that DO change the plan's substance. Every one of these must keep invalidating, or
# the canonicalization has been widened past what it can prove.
_MEANINGFUL = {
    "a word changed": _DESC.replace("Do the thing.", "Do the other thing."),
    "a sentence appended": _DESC + "\nAlso rewrite the storage layer.\n",
    "an AC item's text changed": _DESC.replace("the thing is done", "the thing is skipped"),
    "an AC item removed": _DESC.replace("  - [ ] the nested sub-item is done\n", ""),
    "list nesting changed (indent removed)": _DESC.replace(
        "  - [ ] the nested sub-item is done", "- [ ] the nested sub-item is done"
    ),
    "list nesting changed (indent added)": _DESC.replace(
        "- [ ] the thing is done", "    - [ ] the thing is done"
    ),
    "interior blank line removed (paragraphs joined)": _DESC.replace(
        "Do the thing.\n\n##", "Do the thing.\n##"
    ),
    "interior space removed (words joined)": _DESC.replace("Do the thing", "Dothe thing"),
}


def _ctx(description: str, ticket_id: str = "t-2be7") -> PlanContext:
    return PlanContext(
        ticket_id=ticket_id,
        ticket_type="bug",
        title="whitespace fixture",
        description=description,
    )


def _wire_state(monkeypatch: pytest.MonkeyPatch, state: dict) -> None:
    """Stub only the store read; the real fingerprint pipeline still runs."""

    import rebar._reads as _reads
    from rebar.llm.plan_review import relation_snapshot

    monkeypatch.setattr(_reads, "show_ticket", lambda tid, repo_root=None: dict(state))
    monkeypatch.setattr(relation_snapshot, "live_material_children", lambda tid, repo_root=None: [])


def _plan_attestation(signed_material: str) -> dict:
    return {
        "manifest": ["plan-review: PASS", f"material: {signed_material}", "regver: rv0"],
        "head_sha": "headA",
        "signed_at": 100,
    }


@pytest.mark.parametrize("label", sorted(_WHITESPACE_ONLY))
def test_whitespace_only_edit_keeps_fingerprint(label: str) -> None:
    """The composite the gate decides on must not move for a whitespace-only edit."""

    assert material_fingerprint(_ctx(_WHITESPACE_ONLY[label])) == material_fingerprint(_ctx(_DESC))


@pytest.mark.parametrize("label", sorted(_MEANINGFUL))
def test_meaningful_edit_still_moves_fingerprint(label: str) -> None:
    """Negative control: the canonicalization must not launder a substantive change."""

    assert material_fingerprint(_ctx(_MEANINGFUL[label])) != material_fingerprint(_ctx(_DESC))


def test_whitespace_only_edit_leaves_attestation_certified(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate seam itself: a signed review survives a trailing-newline-only edit.

    ``compute_validity`` is the single dispatcher both the claim gate and the close gate
    read through, so a ``certified`` verdict here is what ``review-plan --status`` reports
    and what keeps the close path off ``stale-material``.
    """

    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv0")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")

    signed_state = {"ticket_id": "t-2be7", "status": "in_progress", "description": _DESC}
    _wire_state(monkeypatch, signed_state)
    signed = attest.current_material_fingerprint("t-2be7")
    assert signed is not None

    # The reported repro: `--description="$(cat file)"` drops the trailing newline.
    edited_state = dict(signed_state, description=_DESC.rstrip("\n"))
    _wire_state(monkeypatch, edited_state)

    result = compute_validity(_plan_attestation(signed), edited_state, "plan-review")

    assert result["valid"] is True, result
    assert result["verdict"] == "certified", result


def test_meaningful_edit_still_reads_stale_material(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control at the gate seam: a real edit must still be refused."""

    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv0")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")

    signed_state = {"ticket_id": "t-2be7", "status": "in_progress", "description": _DESC}
    _wire_state(monkeypatch, signed_state)
    signed = attest.current_material_fingerprint("t-2be7")
    assert signed is not None

    edited_state = dict(signed_state, description=_DESC + "\nAlso rewrite the storage layer.\n")
    _wire_state(monkeypatch, edited_state)

    result = compute_validity(_plan_attestation(signed), edited_state, "plan-review")

    assert result["valid"] is False, result
    assert result["verdict"] == "stale-material", result


def test_legacy_algorithm_still_hashes_the_raw_description() -> None:
    """The 96d1 grandfather reproduces the PRE-330c algorithm, which normalized nothing.

    If whitespace canonicalization leaked into that path it would stop matching manifests
    signed before 330c, silently re-breaking the bug the grandfather exists to fix.
    """

    trailing_ws = _DESC.replace("Do the thing.", "Do the thing.  ")

    assert material_fingerprint(
        _ctx(trailing_ws), normalize_checkboxes=False
    ) != material_fingerprint(_ctx(_DESC), normalize_checkboxes=False)
