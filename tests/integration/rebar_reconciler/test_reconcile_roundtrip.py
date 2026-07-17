"""Bidirectional field ROUND-TRIP convergence over the real pure differs.

Every field mapping in the reconciler is exercised one-directionally by the
unit tier (outbound mock vs. mock, inbound mock vs. mock). What was never
covered is the *chained* property that actually matters in production: a value
pushed local->Jira, encoded into the shape Jira stores and returns, then read
back Jira->local, must converge to ZERO spurious mutation. The historical churn
sources (description ADF normalisation, lossy status mapping, label echoes,
parent name skew, comment self-echo) are all round-trip defects: each half is
individually correct yet the composition oscillates.

These tests chain the REAL pure differs end to end against mock fixtures (no
live Jira). The "Jira shape" between the halves is produced exactly as the live
applier produces it:

  * description: outbound maps to plain text, the send path ADF-encodes via
    ``adf.text_to_adf`` (Jira REST v3 requires ADF), the fetcher returns the ADF
    verbatim, and the inbound differ decodes via ``adf.adf_to_text``. The
    round-trip snapshot therefore carries the description as an ADF ``doc``.
  * status: a lossy local status (blocked/cancelled) maps to a live workflow
    status PLUS a ``rebar-status:`` annotation label; the inbound side restores
    the original local status from that label.
  * labels / parent / comments: identity round-trips through the binding store
    and the reconciler marker.

The convergence assertion is the same in every case: after the round-trip the
inbound differ emits NO change for the field under test (and, where relevant,
the outbound differ emits none either), so the bridge reaches a fixed point.

Loaded via ``spec_from_file_location`` per the reconciler test-tree convention.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILER_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def adf() -> ModuleType:
    return _load_module("rebar_reconciler.adf", RECONCILER_DIR / "adf.py")


@pytest.fixture(scope="module")
def outbound() -> ModuleType:
    return _load_module("outbound_differ", RECONCILER_DIR / "outbound_differ.py")


@pytest.fixture(scope="module")
def inbound() -> ModuleType:
    return _load_module("inbound_differ", RECONCILER_DIR / "inbound_differ.py")


# ---------------------------------------------------------------------------
# Stub binding store — serves BOTH directions over one local_id<->jira_key map.
# ---------------------------------------------------------------------------


class StubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        # bindings: {local_id: jira_key}
        self._l2j: dict[str, str] = bindings or {}
        self._j2l: dict[str, str] = {v: k for k, v in self._l2j.items()}

    # outbound surface
    def get_jira_key(self, local_id: str) -> str | None:
        return self._l2j.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._l2j

    # inbound surface
    def get_local_id(self, jira_key: str) -> str | None:
        return self._j2l.get(jira_key)

    # baseline arbitration surface (always-on since story d6bd)
    def is_pending(self, local_id: str) -> bool:
        return False

    def get_baseline(self, local_id: str) -> dict | None:
        return None


def _make_ticket(
    ticket_id: str = "abc-1234",
    title: str = "Fix the widget",
    description: str = "It is broken",
    status: str = "open",
    priority: int = 2,
    ticket_type: str = "task",
    assignee: str = "alice",
    tags: list[str] | None = None,
    comments: list[dict] | None = None,
    parent_id: str | None = None,
) -> dict:
    return {
        "ticket_id": ticket_id,
        "title": title,
        "description": description,
        "status": status,
        "priority": priority,
        "ticket_type": ticket_type,
        "assignee": assignee,
        "tags": tags or [],
        "comments": comments or [],
        "deps": [],
        "parent_id": parent_id,
    }


def _outbound_jira_shape(ticket: dict, *, adf, outbound, jira_status: str) -> dict:
    """Build the Jira ``fields`` snapshot that results from pushing ``ticket``.

    Reproduces the live applier's send transform: scalar fields land as the
    Jira-side names (summary/status.name/...), the description is ADF-encoded
    (Jira REST v3), and the assignee comes back as the displayName object Jira
    stores. ``jira_status`` is the WORKFLOW status the push lands on (lossy
    statuses land on their nearest live state; the annotation label preserves
    the original).
    """
    mapped = outbound._map_local_to_jira_fields(ticket)
    return {
        "summary": mapped["summary"],
        # Jira stores/returns description as ADF (the applier encodes via text_to_adf).
        "description": adf.text_to_adf(mapped["description"]),
        "issuetype": {"name": mapped["issuetype"]},
        "priority": {"name": mapped["priority"]},
        "status": {"name": jira_status},
        "assignee": {"displayName": ticket["assignee"]} if ticket["assignee"] else None,
        "labels": [],
    }


# ===========================================================================
# 1. description ADF round-trip (outbound ADF-encode -> inbound decode)
# ===========================================================================


def test_description_survives_outbound_adf_inbound_unchanged(adf, outbound, inbound):
    """rebar -> Jira-ADF -> rebar: a pushed description must NOT re-emit inbound.

    The top historical churn source: outbound maps the description to plain
    text, the applier ADF-encodes it, and the inbound differ decodes the ADF.
    A faithful round-trip yields the original text (modulo the trailing-newline
    normalisation both sides rstrip-tolerate), so the inbound differ must emit
    NO ``description`` change for an otherwise-unchanged ticket.
    """
    bind = StubBindingStore({"loc-1": "DIG-1"})
    desc = "Multi-line body\n\n- bullet one\n- bullet two\n\nClosing paragraph."
    ticket = _make_ticket("loc-1", description=desc, status="in_progress")

    # Outbound encodes local -> Jira ADF; inbound decodes Jira ADF -> local.
    jira_fields = _outbound_jira_shape(
        ticket, adf=adf, outbound=outbound, jira_status="In Progress"
    )

    # Sanity: the snapshot really carries ADF, not the plain string.
    assert isinstance(jira_fields["description"], dict)
    assert jira_fields["description"]["type"] == "doc"

    inbound_muts, _ = inbound.compute_inbound_mutations(
        {"DIG-1": jira_fields}, bind, {"loc-1": ticket}
    )
    changed_fields = {f for m in inbound_muts for f in m.fields}
    assert "description" not in changed_fields, (
        f"description re-emitted inbound after an ADF round-trip (spurious churn): "
        f"{[m.fields for m in inbound_muts]}"
    )

    # And the outbound differ over the round-tripped snapshot is also a no-op
    # for the description (the rstrip-tolerant compare absorbs ADF's trailing
    # newline normalisation).
    out_muts, _ = outbound.compute_outbound_mutations([ticket], {"DIG-1": jira_fields}, bind)
    out_changed = {f for m in out_muts for f in m.fields}
    assert "description" not in out_changed, (
        f"description re-emitted outbound after the round-trip: {[m.fields for m in out_muts]}"
    )


def test_oversize_description_truncated_by_jira_not_pulled_back(adf, outbound, inbound):
    """rebar -> Jira-TRUNCATED-ADF -> rebar: the truncated body must NOT clobber local.

    Bug tarry-amble-bugle. When a local description is too long for Jira's ADF
    limit, the send path truncates it (``adf.fit_text_to_adf_limit``) so it lands
    — send-side only, the local store is never mutated. Jira then stores/returns
    the TRUNCATED body. This chains the full round-trip and asserts the fixed
    point:

      * the OUTBOUND differ over the truncated Jira snapshot emits NO description
        change (it fits the local value before comparing — long-standing);
      * the INBOUND differ emits NO description change either (the fix — it must
        apply the SAME fit to the local value, else it pulls Jira's truncated
        body back into local, clobbering the full description and invalidating
        the ticket's plan-review fingerprint/signature);
      * the local ticket keeps its FULL, untruncated description.
    """
    bind = StubBindingStore({"loc-trunc": "DIG-99"})
    # Multi-line oversize body: text_to_adf wraps each line in its own paragraph,
    # so the ADF inflates well past the plain-text length and the fit must cut.
    oversize = ("X" * 30 + "\n") * 1500
    ticket = _make_ticket("loc-trunc", description=oversize, status="in_progress")

    mapped = outbound._map_local_to_jira_fields(ticket)
    landed = adf.fit_text_to_adf_limit(mapped["description"])
    assert len(landed) < len(oversize), "fixture must actually truncate"

    # Jira stores the truncated body as ADF — exactly what the next fetch returns.
    jira_fields = {
        "summary": mapped["summary"],
        "description": adf.text_to_adf(landed),
        "issuetype": {"name": mapped["issuetype"]},
        "priority": {"name": mapped["priority"]},
        "status": {"name": "In Progress"},
        "assignee": {"displayName": ticket["assignee"]},
        "labels": [],
    }
    assert jira_fields["description"]["type"] == "doc"  # sanity: really ADF

    # Inbound must NOT pull the truncated body back into local.
    inbound_muts, _ = inbound.compute_inbound_mutations(
        {"DIG-99": jira_fields}, bind, {"loc-trunc": ticket}
    )
    inbound_desc = [m.fields["description"] for m in inbound_muts if "description" in m.fields]
    assert inbound_desc == [], (
        "inbound pulled Jira's TRUNCATED description back into local (would clobber "
        f"the full body / invalidate the plan-review signature): {[d[:80] for d in inbound_desc]}"
    )

    # Outbound is also stable (it fits the local value before comparing).
    out_muts, _ = outbound.compute_outbound_mutations([ticket], {"DIG-99": jira_fields}, bind)
    assert "description" not in {f for m in out_muts for f in m.fields}, (
        "outbound re-emitted the truncated description (per-pass churn)"
    )

    # Hard constraint: the local ticket keeps its FULL untruncated description.
    assert ticket["description"] == oversize


def test_description_with_trailing_whitespace_does_not_churn(adf, outbound, inbound):
    """A local description with trailing newlines must still converge.

    Jira's ADF normalisation strips trailing whitespace (the DIG-4175 plateau).
    The differs rstrip-compare, so even a local body ending in ``\\n\\n`` must
    not produce a description diff after the round-trip.
    """
    bind = StubBindingStore({"loc-2": "DIG-2"})
    ticket = _make_ticket("loc-2", description="Body with trailing blank lines\n\n")
    jira_fields = _outbound_jira_shape(ticket, adf=adf, outbound=outbound, jira_status="To Do")

    inbound_muts, _ = inbound.compute_inbound_mutations(
        {"DIG-2": jira_fields}, bind, {"loc-2": ticket}
    )
    out_muts, _ = outbound.compute_outbound_mutations([ticket], {"DIG-2": jira_fields}, bind)
    assert "description" not in {f for m in inbound_muts for f in m.fields}
    assert "description" not in {f for m in out_muts for f in m.fields}


# ===========================================================================
# 2. status annotation-label round-trip (lossy status via rebar-status: label)
# ===========================================================================


def test_blocked_status_roundtrips_through_in_progress_plus_label(adf, outbound, inbound):
    """blocked -> (In Progress + rebar-status:blocked) -> blocked.

    ``blocked`` has no live DIG workflow equivalent: it maps to ``In Progress``
    and the lossless intent is carried in a ``rebar-status:blocked`` annotation
    label. Each half is unit-tested in isolation; this chains them. The outbound
    differ emits the annotation-label ADD; once that label rides on the Jira
    snapshot, the inbound differ must restore the ORIGINAL ``blocked`` status
    (the label takes precedence over the raw ``In Progress`` workflow value) and
    emit NO status change.
    """
    bind = StubBindingStore({"loc-3": "DIG-3"})
    ticket = _make_ticket("loc-3", status="blocked")

    # Outbound: status maps to In Progress, annotation label ADD is emitted.
    pre_push = _outbound_jira_shape(ticket, adf=adf, outbound=outbound, jira_status="In Progress")
    out_muts, _ = outbound.compute_outbound_mutations([ticket], {"DIG-3": pre_push}, bind)
    (om,) = out_muts
    label_adds = {lm["label"] for lm in om.labels if lm["action"] == "add"}
    assert "rebar-status:blocked" in label_adds, (
        f"outbound did not emit the rebar-status:blocked annotation ADD: {om.labels}"
    )

    # Apply that label to the Jira snapshot (what the next fetch returns).
    post_push = dict(pre_push)
    post_push["labels"] = ["rebar-status:blocked"]

    # Inbound: the label restores blocked (NOT in_progress) — no status churn.
    inbound_muts, _ = inbound.compute_inbound_mutations(
        {"DIG-3": post_push}, bind, {"loc-3": ticket}
    )
    inbound_status_changes = [m.fields["status"] for m in inbound_muts if "status" in m.fields]
    assert inbound_status_changes == [], (
        f"blocked status did not round-trip through the annotation label — inbound "
        f"emitted status change(s): {inbound_status_changes}"
    )

    # Outbound over the post-push snapshot is also stable (label already present,
    # status already In Progress): no annotation re-ADD, no status diff.
    out2, _ = outbound.compute_outbound_mutations([ticket], {"DIG-3": post_push}, bind)
    out2_label_adds = {lm["label"] for m in out2 for lm in m.labels if lm["action"] == "add"}
    assert "rebar-status:blocked" not in out2_label_adds


def test_cancelled_status_roundtrips_through_done_plus_label(adf, outbound, inbound):
    """cancelled -> (Done + rebar-status:cancelled) -> cancelled (the Done twin)."""
    bind = StubBindingStore({"loc-4": "DIG-4"})
    ticket = _make_ticket("loc-4", status="cancelled")
    pre_push = _outbound_jira_shape(ticket, adf=adf, outbound=outbound, jira_status="Done")
    out_muts, _ = outbound.compute_outbound_mutations([ticket], {"DIG-4": pre_push}, bind)
    (om,) = out_muts
    assert "rebar-status:cancelled" in {lm["label"] for lm in om.labels if lm["action"] == "add"}

    post_push = dict(pre_push)
    post_push["labels"] = ["rebar-status:cancelled"]
    inbound_muts, _ = inbound.compute_inbound_mutations(
        {"DIG-4": post_push}, bind, {"loc-4": ticket}
    )
    assert [m.fields["status"] for m in inbound_muts if "status" in m.fields] == []


# ===========================================================================
# 3. label add round-trip (rebar tag -> Jira label -> NOT re-echoed inbound)
# ===========================================================================


def test_label_add_does_not_reecho_inbound(adf, outbound, inbound):
    """A local tag pushed as a Jira label must NOT re-import as an inbound add.

    Outbound emits a label ADD for the user tag; once it lands on the Jira
    snapshot the inbound label diff must see it as already-mirrored (present on
    both sides) and emit NO label mutation — otherwise the tag ping-pongs.
    """
    bind = StubBindingStore({"loc-5": "DIG-5"})
    ticket = _make_ticket("loc-5", tags=["frontend"])

    pre_push = _outbound_jira_shape(ticket, adf=adf, outbound=outbound, jira_status="To Do")
    out_muts, _ = outbound.compute_outbound_mutations([ticket], {"DIG-5": pre_push}, bind)
    (om,) = out_muts
    assert {"action": "add", "label": "frontend"} in om.labels

    # The label is now on Jira (and the bridge-internal rebar-id label too).
    post_push = dict(pre_push)
    post_push["labels"] = ["frontend", "rebar-id:loc-5"]

    inbound_muts, _ = inbound.compute_inbound_mutations(
        {"DIG-5": post_push}, bind, {"loc-5": ticket}
    )
    inbound_label_actions = [lm for m in inbound_muts for lm in m.labels]
    assert inbound_label_actions == [], (
        f"label re-echoed inbound after the outbound add: {inbound_label_actions}"
    )

    # Outbound is now stable too (label present on both sides → no re-ADD).
    out2, _ = outbound.compute_outbound_mutations([ticket], {"DIG-5": post_push}, bind)
    assert [lm for m in out2 for lm in m.labels] == []


# ===========================================================================
# 4. parent reparent round-trip (parent_id <-> Jira parent.key)
# ===========================================================================


def test_reparent_outbound_then_inbound_converges(adf, outbound, inbound):
    """parent_id -> Jira parent.key -> parent_id converges through the binding.

    Outbound resolves the local ``parent_id`` to its bound Jira key and emits a
    ``parent`` field; once Jira returns ``parent: {"key": ...}`` the inbound
    differ resolves it back to the SAME local id and emits no ``parent_id``
    change. (The parent must be an Epic — Jira's hierarchy only permits Epic
    parents — so the differ does not suppress the diff.)
    """
    bind = StubBindingStore({"child-1": "DIG-20", "epic-1": "DIG-10"})
    parent_epic = _make_ticket("epic-1", ticket_type="epic", status="open")
    child = _make_ticket("child-1", ticket_type="task", parent_id="epic-1")

    # Outbound: resolve parent_id=epic-1 -> DIG-10, emit a parent field.
    child_jira = _outbound_jira_shape(child, adf=adf, outbound=outbound, jira_status="To Do")
    # No parent yet on the Jira side -> outbound emits the parent diff.
    out_muts, _ = outbound.compute_outbound_mutations(
        [parent_epic, child],
        {
            "DIG-10": _outbound_jira_shape(
                parent_epic, adf=adf, outbound=outbound, jira_status="To Do"
            ),
            "DIG-20": child_jira,
        },
        bind,
    )
    child_om = next(m for m in out_muts if m.local_id == "child-1")
    assert child_om.fields.get("parent") == "DIG-10", (
        f"outbound did not emit parent=DIG-10 for the reparent: {child_om.fields}"
    )

    # Apply the reparent on the Jira side (what the next fetch returns).
    child_jira_reparented = dict(child_jira)
    child_jira_reparented["parent"] = {"key": "DIG-10"}

    # Inbound: Jira parent.key=DIG-10 resolves back to local epic-1 == local
    # parent_id, so NO parent_id change is emitted.
    inbound_muts, _ = inbound.compute_inbound_mutations(
        {"DIG-20": child_jira_reparented}, bind, {"child-1": child}
    )
    parent_changes = [m.fields["parent_id"] for m in inbound_muts if "parent_id" in m.fields]
    assert parent_changes == [], (
        f"parent did not converge — inbound emitted parent_id change(s): {parent_changes}"
    )

    # Outbound is now stable (Jira parent.key already == resolved local parent).
    out2, _ = outbound.compute_outbound_mutations([child], {"DIG-20": child_jira_reparented}, bind)
    assert "parent" not in {f for m in out2 for f in m.fields}


# ===========================================================================
# 5. comment echo-suppression (decorated outbound comment not re-imported)
# ===========================================================================


def test_outbound_comment_not_reimported_inbound(adf, outbound, inbound):
    """An outbound comment carries the reconciler marker; inbound must skip it.

    Outbound decorates every pushed comment body with ``RECONCILER_MARKER`` so
    the next inbound pass can recognise its own echo. After ADF round-trip the
    marker survives, and the inbound comment diff must NOT re-import the
    decorated body as a 'new Jira comment' (the bridge-loop guard).
    """
    bind = StubBindingStore({"loc-6": "DIG-6"})
    ticket = _make_ticket("loc-6", comments=[{"body": "Investigated the root cause."}])

    # Outbound: the create/update emits a decorated comment add.
    pre_push = _outbound_jira_shape(ticket, adf=adf, outbound=outbound, jira_status="To Do")
    # Snapshot carries the (empty) comment field so _diff_comments uses it directly.
    pre_push["comment"] = {"comments": []}
    out_muts, _ = outbound.compute_outbound_mutations([ticket], {"DIG-6": pre_push}, bind)
    (om,) = out_muts
    (cm,) = om.comments
    decorated_body = cm["body"]
    assert outbound.RECONCILER_MARKER in decorated_body, (
        f"outbound comment was not decorated with the reconciler marker: {decorated_body!r}"
    )

    # Jira stores the comment body as ADF; the next fetch returns it that way.
    jira_comment = {"id": "9001", "body": adf.text_to_adf(decorated_body)}
    post_push = dict(pre_push)
    # Inbound reads comments from the flat ``comments`` list (live fetch shape).
    post_push["comments"] = [jira_comment]

    inbound_muts, _ = inbound.compute_inbound_mutations(
        {"DIG-6": post_push}, bind, {"loc-6": ticket}
    )
    imported_comments = [c for m in inbound_muts for c in m.comments]
    assert imported_comments == [], (
        f"the reconciler's own outbound comment was re-imported inbound (loop): {imported_comments}"
    )

    # And the outbound side does not re-add it either: the decorated body is now
    # present on Jira, so the marker-stripped dedup matches the local comment.
    post_for_outbound = dict(pre_push)
    post_for_outbound["comment"] = {"comments": [{"body": adf.text_to_adf(decorated_body)}]}
    out2, _ = outbound.compute_outbound_mutations([ticket], {"DIG-6": post_for_outbound}, bind)
    assert [c for m in out2 for c in m.comments] == [], (
        "outbound re-emitted the comment that is already mirrored on Jira"
    )


# ===========================================================================
# 6. link round-trip (rebar dep -> Jira issuelink -> NOT re-emitted either way)
# ===========================================================================


def test_link_relationship_survives_roundtrip_without_reemit(adf, outbound, inbound):
    """A local 'blocks' dep pushed as a Jira Blocks issuelink must reach a fixed
    point: once the link rides on the Jira snapshot, NEITHER the outbound differ
    re-emits a `set_relationship` (no per-pass churn) NOR the inbound differ
    re-imports the dep (no echo). This is the link analogue of cases 3/5 — the
    historical link layer (story 25ae) had NO round-trip convergence coverage,
    only one-directional unit tests.
    """
    bind = StubBindingStore({"loc-7": "DIG-7", "loc-8": "DIG-8"})
    blocker = _make_ticket("loc-7", status="open")
    blocker["deps"] = [{"target_id": "loc-8", "relation": "blocks", "link_uuid": "u-7"}]
    blocked = _make_ticket("loc-8", status="open")

    # Outbound: no issuelinks on the Jira side yet → emit the link ADD.
    pre_blocker = _outbound_jira_shape(blocker, adf=adf, outbound=outbound, jira_status="To Do")
    pre_blocked = _outbound_jira_shape(blocked, adf=adf, outbound=outbound, jira_status="To Do")
    out_muts, _ = outbound.compute_outbound_mutations(
        [blocker, blocked], {"DIG-7": pre_blocker, "DIG-8": pre_blocked}, bind
    )
    blocker_om = next(m for m in out_muts if m.local_id == "loc-7")
    assert any(
        lm["action"] == "add" and lm["type"] == "Blocks" and lm["to_key"] == "DIG-8"
        for lm in blocker_om.links
    ), f"outbound did not emit the Blocks link to DIG-8: {blocker_om.links}"

    # Apply the link to the Jira snapshot (what the next fetch returns). LIVE-JIRA
    # direction (bug 4b59): "DIG-7 blocks DIG-8" places DIG-8 on DIG-7's OUTWARD side
    # (type.outward == "blocks"). Inbound maps an outwardIssue Blocks back to the
    # 'blocks' relation (matching the local dep). (This fixture previously used the
    # inwardIssue side — the reversed convention that let the inbound inversion ship;
    # the absolute mapping is now pinned by test_link_direction_absolute.py.)
    post_blocker = dict(pre_blocker)
    post_blocker["issuelinks"] = [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "DIG-8"}}]

    # Outbound is now stable: the link is already present → no re-ADD (no churn).
    out2, _ = outbound.compute_outbound_mutations(
        [blocker, blocked], {"DIG-7": post_blocker, "DIG-8": pre_blocked}, bind
    )
    out2_links = [lm for m in out2 for lm in m.links]
    assert out2_links == [], (
        f"link re-emitted outbound after the round-trip (per-pass churn): {out2_links}"
    )

    # Inbound: the Jira Blocks link resolves back to the local dep already on
    # loc-7, so NO inbound link mutation is emitted (no echo).
    inbound_muts, _ = inbound.compute_inbound_mutations(
        {"DIG-7": post_blocker}, bind, {"loc-7": blocker}
    )
    inbound_links = [lm for m in inbound_muts for lm in m.links]
    assert inbound_links == [], (
        f"the link was re-imported inbound after the outbound add (loop): {inbound_links}"
    )
