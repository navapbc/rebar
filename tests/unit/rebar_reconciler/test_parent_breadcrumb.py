"""S7 (2c66-205d-92e1-4419): parent-drift breadcrumb comment — held-out oracle.

When a local parent cannot be represented in Jira (the parent is a non-epic and
this project permits only Epic parents, so the outbound differ suppresses the
parent field), the child lands as an under-defined leaf. This oracle pins the
echo-safe breadcrumb comment that the OUTBOUND comment differ
(``outbound_comments._diff_comments``) emits to point the Jira user at the
nearest ancestor that *is* represented in Jira.

Everything asserted here is an OBSERVABLE output — the returned mutation list
from ``_diff_comments`` and the returned mutation list from
``inbound_collection_diffs._diff_comments_inbound``. Nothing reads a private
symbol name or greps source, so a behaviour-preserving refactor (renaming the
internal ``_build_parent_breadcrumb`` helper, restructuring the walk) cannot turn
these red. The breadcrumb is identified among mutations solely by the stable
identity tag it must carry.

Contracts pinned (ticket ACs):
  * emitted — a bound child with a type-collapsed, bound parent gets exactly one
    breadcrumb naming the nearest represented ancestor's Jira key, carrying both
    the ``<!-- rebar:parent-breadcrumb -->`` identity tag and RECONCILER_MARKER;
  * default no-op — with the ancestor maps absent, every existing caller is
    unchanged (no breadcrumb);
  * append-once — an already-present breadcrumb (found by the stable tag, even
    when it names a DIFFERENT ancestor) suppresses a second one;
  * intervening — an unbound intervening ancestor is skipped to reach a bound
    one, and the body says so;
  * omitted — no ancestor with a bound Jira key ⇒ no breadcrumb;
  * drift-gated — a child whose direct parent IS an epic (the parent field is
    NOT suppressed) or whose parent type is absent gets NO breadcrumb;
  * echo-safe — the emitted body is suppressed by the inbound comment differ
    (it carries RECONCILER_MARKER), never re-ingested as local content.
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler import inbound_collection_diffs as icd
from rebar_reconciler import outbound_comments as oc

# The stable identity tag the breadcrumb must carry so dedup keys on the tag, not
# on the variable ancestor key (ticket "Body content contract" / "Append-once").
PARENT_BREADCRUMB_TAG = "<!-- rebar:parent-breadcrumb -->"


class _FakeBinding:
    """Minimal binding-store stand-in: local_id -> bound Jira key (or None)."""

    def __init__(self, keys: dict[str, str | None]) -> None:
        self._keys = keys

    def get_jira_key(self, local_id: str) -> str | None:
        return self._keys.get(local_id)

    def is_comment_mapped(self, local_comment_key: str) -> bool:
        return False


def _snapshot(jira_key: str, bodies: tuple[str, ...] = ()) -> dict[str, Any]:
    """Jira REST shape: fields["comment"]["comments"] (outer key "comment").

    Carrying the ``comment`` field selects the snapshot/synthetic path, so the
    client is never consulted (no live fetch).
    """
    comments = [{"id": str(100 + i), "body": b} for i, b in enumerate(bodies)]
    return {jira_key: {"comment": {"comments": comments, "total": len(comments)}}}


def _breadcrumbs(mutations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The breadcrumb mutations among a _diff_comments result (by stable tag)."""
    return [m for m in mutations if PARENT_BREADCRUMB_TAG in (m.get("body") or "")]


# --------------------------------------------------------------------------- #
# Happy path (visible to the implementer)
# --------------------------------------------------------------------------- #
def test_breadcrumb_emitted() -> None:
    """A bound child whose type-collapsed direct parent is itself bound gets one
    breadcrumb naming the parent's Jira key, carrying the identity tag + marker.
    """
    bs = _FakeBinding({"C": "PROJ-1", "P1": "PROJ-42"})
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}

    out = oc._diff_comments(
        ticket,
        "PROJ-1",
        _snapshot("PROJ-1"),
        binding_store=bs,
        local_parents={"C": "P1"},
        local_ticket_types={"P1": "task"},  # non-epic => parent field suppressed
    )

    bc = _breadcrumbs(out)
    assert len(bc) == 1, f"expected exactly one breadcrumb, got {len(bc)}"
    body = bc[0]["body"]
    assert bc[0]["action"] == "add"
    assert "PROJ-42" in body, "breadcrumb must name the nearest represented ancestor's key"
    assert PARENT_BREADCRUMB_TAG in body, "breadcrumb must carry the stable identity tag"
    assert oc.RECONCILER_MARKER in body, "breadcrumb must carry the echo marker"


def test_no_breadcrumb_when_ancestor_maps_absent() -> None:
    """Counter-regression: with the ancestor maps absent, behaviour is unchanged.

    Every existing caller/test omits the new maps, so the breadcrumb path must be
    a strict no-op then — a child with a drift-shaped parent gets NO breadcrumb.
    """
    bs = _FakeBinding({"C": "PROJ-1", "P1": "PROJ-42"})
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}

    out = oc._diff_comments(ticket, "PROJ-1", _snapshot("PROJ-1"), binding_store=bs)

    assert out == [], "no breadcrumb (and no other mutation) when the ancestor maps are absent"


def test_no_breadcrumb_when_parent_is_epic() -> None:
    """Drift gate: an Epic direct parent IS representable in Jira, so the parent
    field is NOT suppressed — no drift, therefore no breadcrumb even with the
    ancestor maps present and the parent bound.
    """
    bs = _FakeBinding({"C": "PROJ-1", "P1": "PROJ-42"})
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}

    out = oc._diff_comments(
        ticket,
        "PROJ-1",
        _snapshot("PROJ-1"),
        binding_store=bs,
        local_parents={"C": "P1"},
        local_ticket_types={"P1": "epic"},  # epic parent => NOT suppressed => no drift
    )

    assert _breadcrumbs(out) == [], "an Epic parent is representable ⇒ no breadcrumb"


def test_no_breadcrumb_when_parent_type_absent() -> None:
    """Drift gate: with the direct parent's type absent from local_ticket_types,
    the suppression trigger cannot be established, so no breadcrumb is emitted.
    """
    bs = _FakeBinding({"C": "PROJ-1", "P1": "PROJ-42"})
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}

    out = oc._diff_comments(
        ticket,
        "PROJ-1",
        _snapshot("PROJ-1"),
        binding_store=bs,
        local_parents={"C": "P1"},
        local_ticket_types={},  # parent type unknown => no drift determination => no breadcrumb
    )

    assert _breadcrumbs(out) == [], "absent parent type ⇒ no breadcrumb"


# --------------------------------------------------------------------------- #
# Held-out oracle (edge / E2E)
# --------------------------------------------------------------------------- #
def test_breadcrumb_idempotent_across_ancestor_change() -> None:
    """Append-once via the STABLE tag: an already-landed breadcrumb — even one
    that names a DIFFERENT (older) ancestor — suppresses a second one.

    Dedup keys on the ``<!-- rebar:parent-breadcrumb -->`` tag, never on the
    ancestor key, so a changed nearest ancestor never appends a conflicting
    second breadcrumb (first-writer-wins).
    """
    bs = _FakeBinding({"C": "PROJ-1", "P1": "PROJ-42"})
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}
    existing = (
        "This ticket's parent hierarchy could not be fully represented in Jira. "
        "Nearest tracked ancestor: PROJ-OLD. Full parent context is maintained in rebar.\n"
        + PARENT_BREADCRUMB_TAG
        + "\n\n"
        + oc.RECONCILER_MARKER
    )

    out = oc._diff_comments(
        ticket,
        "PROJ-1",
        _snapshot("PROJ-1", (existing,)),
        binding_store=bs,
        local_parents={"C": "P1"},
        local_ticket_types={"P1": "task"},
    )

    assert _breadcrumbs(out) == [], "an existing tagged breadcrumb must suppress a second one"


def test_unrepresented_intervening_ancestor_noted() -> None:
    """The walk skips an UNBOUND intervening ancestor to reach a bound one, names
    the bound ancestor, and states that intervening levels are not represented.
    """
    bs = _FakeBinding({"C": "PROJ-1", "P1": None, "P2": "PROJ-9"})  # P1 unbound, P2 bound
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}

    out = oc._diff_comments(
        ticket,
        "PROJ-1",
        _snapshot("PROJ-1"),
        binding_store=bs,
        local_parents={"C": "P1", "P1": "P2"},
        local_ticket_types={"P1": "task"},  # direct parent type-collapsed => triggers
    )

    bc = _breadcrumbs(out)
    assert len(bc) == 1, f"expected exactly one breadcrumb, got {len(bc)}"
    body = bc[0]["body"]
    assert "PROJ-9" in body, "breadcrumb must name the nearest BOUND ancestor"
    assert "intervening" in body.lower(), (
        "breadcrumb must state that one or more intervening parent levels are not represented"
    )


def test_breadcrumb_omitted_when_no_bound_ancestor() -> None:
    """No ancestor in the chain has a bound Jira key ⇒ no breadcrumb (nothing to
    point at).
    """
    bs = _FakeBinding({"C": "PROJ-1", "P1": None, "P2": None})
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}

    out = oc._diff_comments(
        ticket,
        "PROJ-1",
        _snapshot("PROJ-1"),
        binding_store=bs,
        local_parents={"C": "P1", "P1": "P2"},
        local_ticket_types={"P1": "task"},
    )

    assert _breadcrumbs(out) == [], "no bound ancestor ⇒ no breadcrumb"


def test_breadcrumb_echo_safe() -> None:
    """E2E loop-safety: the breadcrumb the OUTBOUND differ emits is suppressed by
    the INBOUND comment differ (it carries RECONCILER_MARKER) — never pulled back
    in as local content.
    """
    bs = _FakeBinding({"C": "PROJ-1", "P1": "PROJ-42"})
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}

    out = oc._diff_comments(
        ticket,
        "PROJ-1",
        _snapshot("PROJ-1"),
        binding_store=bs,
        local_parents={"C": "P1"},
        local_ticket_types={"P1": "task"},
    )
    bc = _breadcrumbs(out)
    assert len(bc) == 1
    breadcrumb_body = bc[0]["body"]

    # Feed the emitted breadcrumb back as a Jira-side comment to the inbound differ.
    jira_fields = {"comment": {"comments": [{"id": "500", "body": breadcrumb_body}]}}
    inbound = icd._diff_comments_inbound(jira_fields, {"comments": []})

    assert inbound == [], "the breadcrumb must be suppressed inbound (RECONCILER_MARKER echo)"
