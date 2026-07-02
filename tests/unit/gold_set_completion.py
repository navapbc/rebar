"""The completion-floor CALIBRATION gold set (epic 66ac / story 77cf).

A frozen, labelled corpus for the Pass-2 completion sub-call (attribution / containment / layer) and
the Pass-3 completion floor. Each :class:`GoldCase` carries a synthetic finding, the **correct**
sub-answers (``gold``), whether its attributed child is delivered-now, and the **expected floor
outcome** (``expect_drop``). It has two consumers:

* the DETERMINISTIC e2e test (``test_completion_floor_e2e.py``) feeds ``gold`` through the floor and
  asserts ``expect_drop`` — pinning the floor's drop/keep logic against every anchor;
* the LIVE calibration (``scripts/calibrate_completion_floor.py``) feeds each finding to the REAL
  model and scores the model's answers against ``gold`` — the agreement recorded under
  ``docs/calibration/``.

The five anchor categories (the must-never-suppress set is everything but ``DROP``):

* ``DROP`` — pure re-litigation of a delivered child's settled plan text → the ONLY drop.
* ``DELIVERED_FUNC`` — about the delivered *functionality* (mechanism/contract), not plan → keep.
* ``SECURITY_CONTRACT`` — a security (T5c) / contract (T10) criterion → keep (preserve-set veto).
* ``CROSS_SIBLING`` — spans an OPEN sibling / the system, not limited to closed work → keep.
* ``FORCE_CLOSED`` — attributed to a force-closed (unverified, not delivered-now) child → keep.

Provenance ∈ {``G3G4`` (structural container finding — carries ``_container_child``), ``coherence``
(the COH criterion), ``overlay`` (the T-series overlay criteria)}.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The delivered-now children the gold set references (closed + attested). The force-closed child
# `fc1` is deliberately NOT here — findings about it must never drop.
DELIVERED_CHILD_IDS = frozenset({"del-a", "del-b", "del-c"})
FORCE_CLOSED_CHILD_ID = "fc1"

CONTAINMENT_CLOSED = "limited-to-closed"
CONTAINMENT_SPANS = "spans-open-or-system"
LAYER_PLAN = "plan-semantics"
LAYER_FUNC = "delivered-functionality"


@dataclass(frozen=True)
class GoldCase:
    id: str
    category: str  # DROP | DELIVERED_FUNC | SECURITY_CONTRACT | CROSS_SIBLING | FORCE_CLOSED
    provenance: str  # G3G4 | coherence | overlay
    finding: dict  # {finding, criteria, location, _container_child?}
    gold: dict  # the CORRECT {attribution, containment, layer}
    expect_drop: bool
    delivered: bool = True  # is the attributed child in DELIVERED_CHILD_IDS?
    _tags: tuple = field(default=())


def _finding(text: str, criteria: list[str], *, child: str | None = None) -> dict:
    f = {"finding": text, "criteria": criteria, "location": f"child {child}" if child else "plan"}
    if child is not None:
        f["_container_child"] = child
    return f


def _case(
    cid,
    category,
    provenance,
    text,
    criteria,
    *,
    child,
    containment,
    layer,
    expect_drop,
    delivered=True,
) -> GoldCase:
    return GoldCase(
        id=cid,
        category=category,
        provenance=provenance,
        finding=_finding(text, criteria, child=child if provenance == "G3G4" else None),
        gold={"attribution": child, "containment": containment, "layer": layer},
        expect_drop=expect_drop,
        delivered=delivered,
    )


# ── DROP: pure re-litigation of delivered, settled plan text (the ONLY drop category) ───────────
_DROP = [
    _case(
        "drop-g3-1",
        "DROP",
        "G3G4",
        "del-a's AC step 3 wording is ambiguous about ordering",
        ["G3"],
        child="del-a",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=True,
    ),
    _case(
        "drop-g4-1",
        "DROP",
        "G3G4",
        "del-b's success criteria could be sized into two tasks",
        ["G4"],
        child="del-b",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=True,
    ),
    _case(
        "drop-coh-1",
        "DROP",
        "coherence",
        "del-a's scope paragraph repeats the Why section",
        ["COH"],
        child="del-a",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=True,
    ),
    _case(
        "drop-coh-2",
        "DROP",
        "coherence",
        "del-c's plan phrasing mixes goal and mechanism",
        ["COH"],
        child="del-c",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=True,
    ),
    _case(
        "drop-ovl-1",
        "DROP",
        "overlay",
        "del-b's acceptance list is not numbered (style)",
        ["T8"],
        child="del-b",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=True,
    ),
    _case(
        "drop-g3-2",
        "DROP",
        "G3G4",
        "del-c's description could state the non-goals explicitly",
        ["G3"],
        child="del-c",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=True,
    ),
]

# ── DELIVERED_FUNC: about the delivered functionality (mechanism/contract) → keep ───────────────
_DELIVERED_FUNC = [
    _case(
        "func-g4-1",
        "DELIVERED_FUNC",
        "G3G4",
        "del-a's delivered retry loop has no backoff cap",
        ["G4"],
        child="del-a",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_FUNC,
        expect_drop=False,
    ),
    _case(
        "func-g3-1",
        "DELIVERED_FUNC",
        "G3G4",
        "del-b's delivered parser drops trailing records",
        ["G3"],
        child="del-b",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_FUNC,
        expect_drop=False,
    ),
    _case(
        "func-coh-1",
        "DELIVERED_FUNC",
        "coherence",
        "del-c's shipped API returns the wrong unit",
        ["COH"],
        child="del-c",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_FUNC,
        expect_drop=False,
    ),
    _case(
        "func-ovl-1",
        "DELIVERED_FUNC",
        "overlay",
        "del-a's delivered migration is not idempotent",
        ["T3"],
        child="del-a",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_FUNC,
        expect_drop=False,
    ),
    _case(
        "func-ovl-2",
        "DELIVERED_FUNC",
        "overlay",
        "del-b's shipped job has no failure alerting",
        ["T11"],
        child="del-b",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_FUNC,
        expect_drop=False,
    ),
    _case(
        "func-g4-2",
        "DELIVERED_FUNC",
        "G3G4",
        "del-c's delivered cache never invalidates",
        ["G4"],
        child="del-c",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_FUNC,
        expect_drop=False,
    ),
]

# ── SECURITY_CONTRACT: T5c (security) / T10 (contract) criteria → keep (preserve-set veto) ───────
_SECURITY_CONTRACT = [
    _case(
        "sec-1",
        "SECURITY_CONTRACT",
        "overlay",
        "del-a's delivered endpoint has no authz check",
        ["T5c"],
        child="del-a",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=False,
    ),
    _case(
        "sec-2",
        "SECURITY_CONTRACT",
        "overlay",
        "del-b writes a secret to a world-readable log",
        ["T5c"],
        child="del-b",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_FUNC,
        expect_drop=False,
    ),
    _case(
        "contract-1",
        "SECURITY_CONTRACT",
        "overlay",
        "del-c's delivered contract omits an error field",
        ["T10"],
        child="del-c",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_FUNC,
        expect_drop=False,
    ),
    _case(
        "contract-2",
        "SECURITY_CONTRACT",
        "overlay",
        "del-a's response schema drops a required key",
        ["T10"],
        child="del-a",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=False,
    ),
    _case(
        "sec-g3-1",
        "SECURITY_CONTRACT",
        "G3G4",
        "del-b's delivered flow trusts an unvalidated header",
        ["G3", "T5c"],
        child="del-b",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=False,
    ),
    _case(
        "contract-g4-1",
        "SECURITY_CONTRACT",
        "G3G4",
        "del-c breaks the interface two siblings consume",
        ["G4", "T10"],
        child="del-c",
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=False,
    ),
]

# ── CROSS_SIBLING: spans an OPEN sibling / the system → keep ─────────────────────────────────────
_CROSS_SIBLING = [
    _case(
        "span-g3-1",
        "CROSS_SIBLING",
        "G3G4",
        "del-a's step-3 mechanism conflicts with open sibling op-x",
        ["G3"],
        child="del-a",
        containment=CONTAINMENT_SPANS,
        layer=LAYER_PLAN,
        expect_drop=False,
    ),
    _case(
        "span-g4-1",
        "CROSS_SIBLING",
        "G3G4",
        "del-b's data shape is incompatible with open sibling op-y",
        ["G4"],
        child="del-b",
        containment=CONTAINMENT_SPANS,
        layer=LAYER_FUNC,
        expect_drop=False,
    ),
    _case(
        "span-coh-1",
        "CROSS_SIBLING",
        "coherence",
        "del-c's plan assumes an ordering the open plan reverses",
        ["COH"],
        child="del-c",
        containment=CONTAINMENT_SPANS,
        layer=LAYER_PLAN,
        expect_drop=False,
    ),
    _case(
        "span-ovl-1",
        "CROSS_SIBLING",
        "overlay",
        "del-a's delivered schema forces a system-wide migration",
        ["T3"],
        child="del-a",
        containment=CONTAINMENT_SPANS,
        layer=LAYER_FUNC,
        expect_drop=False,
    ),
    _case(
        "span-coh-2",
        "CROSS_SIBLING",
        "coherence",
        "del-b's naming clashes with the epic-wide convention",
        ["COH"],
        child="del-b",
        containment=CONTAINMENT_SPANS,
        layer=LAYER_PLAN,
        expect_drop=False,
    ),
    _case(
        "span-g3-2",
        "CROSS_SIBLING",
        "G3G4",
        "del-c's approach duplicates work planned in open sibling op-z",
        ["G3"],
        child="del-c",
        containment=CONTAINMENT_SPANS,
        layer=LAYER_PLAN,
        expect_drop=False,
    ),
]

# ── FORCE_CLOSED: attributed to a force-closed (unverified, not delivered-now) child → keep ──────
_FORCE_CLOSED = [
    _case(
        "fc-g3-1",
        "FORCE_CLOSED",
        "G3G4",
        "fc1's AC step 2 wording is ambiguous",
        ["G3"],
        child=FORCE_CLOSED_CHILD_ID,
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=False,
        delivered=False,
    ),
    _case(
        "fc-g4-1",
        "FORCE_CLOSED",
        "G3G4",
        "fc1's success criteria could be split",
        ["G4"],
        child=FORCE_CLOSED_CHILD_ID,
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=False,
        delivered=False,
    ),
    _case(
        "fc-coh-1",
        "FORCE_CLOSED",
        "coherence",
        "fc1's scope section is redundant",
        ["COH"],
        child=FORCE_CLOSED_CHILD_ID,
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=False,
        delivered=False,
    ),
    _case(
        "fc-ovl-1",
        "FORCE_CLOSED",
        "overlay",
        "fc1's checklist is unnumbered (style)",
        ["T8"],
        child=FORCE_CLOSED_CHILD_ID,
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=False,
        delivered=False,
    ),
    _case(
        "fc-coh-2",
        "FORCE_CLOSED",
        "coherence",
        "fc1's plan phrasing mixes goal and mechanism",
        ["COH"],
        child=FORCE_CLOSED_CHILD_ID,
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=False,
        delivered=False,
    ),
    _case(
        "fc-g3-2",
        "FORCE_CLOSED",
        "G3G4",
        "fc1's description could state non-goals",
        ["G3"],
        child=FORCE_CLOSED_CHILD_ID,
        containment=CONTAINMENT_CLOSED,
        layer=LAYER_PLAN,
        expect_drop=False,
        delivered=False,
    ),
]

GOLD_SET: list[GoldCase] = [
    *_DROP,
    *_DELIVERED_FUNC,
    *_SECURITY_CONTRACT,
    *_CROSS_SIBLING,
    *_FORCE_CLOSED,
]

CATEGORIES = ("DROP", "DELIVERED_FUNC", "SECURITY_CONTRACT", "CROSS_SIBLING", "FORCE_CLOSED")
PROVENANCES = ("G3G4", "coherence", "overlay")
