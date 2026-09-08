"""Exercise the J11 14-by-2 mutation table against live Data Center and a scrubbed store copy.

Each field and direction has an independent verdict: inbound writes a unique value through DC
and reads local state; outbound writes locally and reads the DC issue. Inbound cells wait for
JQL to reflect the changed value, not merely the key, so index lag is not misdiagnosed. Every
writing pass is scoped to its local-ID/key pair because the scrubbed store has no bindings;
only dry-runs may be unscoped.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _child_diag import assert_child_ran_clean
from _dc_support import ADMIN_USER, BASE, collect_base_urls, live_jira_ready
from _dc_support import assert_bridge_alert_for_mutation as _assert_bridge_alert_for_mutation
from _dc_support import assert_local_assignee_is as _assert_local_assignee_is
from _dc_support import assert_mint_registered as _assert_mint_registered
from _dc_support import assert_outbound_provenance_markers as _assert_outbound_provenance_markers
from _dc_support import envelope as _envelope
from _dc_support import forget_identity_mapping as _forget_identity_mapping
from _dc_support import probe_subtask_parent_editmeta_ops as _probe_subtask_parent_editmeta_ops
from _dc_support import probe_subtask_parent_put as _probe_subtask_parent_put
from _dc_support import raw_indexed_issue_count as _raw_indexed_issue_count
from _dc_support import read_local_ticket as _local
from _dc_support import run_reconcile as _run
from _dc_support import seed_searchable_issue as _seed
from _dc_support import skip_no_extra as _skip_no_extra
from _dc_support import skip_no_harness as _skip

# Re-export the exact sentinel name used to mark this module for the all-skipped live-test canary.
_live_jira_ready = live_jira_ready

_WRITING_MODE = "bootstrap-strict"


def _uniq(prefix: str) -> str:
    """A value no prior run can have written, so an oracle cannot pass on a stale read."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _wait_until_search_reflects(
    transport: Any,
    project: str,
    key: str,
    predicate: Callable[[dict[str, Any]], bool],
    what: str,
    timeout: float = 90.0,
) -> None:
    """Wait until JQL returns ``key`` with the changed state accepted by ``predicate``.

    Existence alone can expose a stale indexed document; the inbound differ reads this search
    result, so waiting for the exact change prevents index lag from masquerading as bridge loss.
    """
    deadline = time.monotonic() + timeout
    attempts = 0
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        attempts += 1
        for hit in transport.search_issues(f'project = "{project}" AND key = "{key}"'):
            if hit.get("key") == key:
                last = hit
                if predicate(hit):
                    return
        time.sleep(2.0)
    raise AssertionError(
        f"the index never reflected {what} on {key} within {timeout:.0f}s ({attempts} "
        f"attempts). This is NOT a bridge defect — the write succeeded, the SEARCH cannot see "
        f"it yet. Last indexed fields: {(last or {}).get('fields')!r}"
    )


def _linked_keys(links: list[dict[str, Any]]) -> set[str | None]:
    """The counterpart keys named by an ``issuelinks`` payload, in EITHER direction.

    A Jira link is nested under ``outwardIssue`` or ``inwardIssue`` depending on which end
    is being read, so a reader that inspects only one of the two silently sees no link half
    the time. Extracted from `test_outbound_link_round_trips`, which had this inline, so the
    add cell and the remove cell cannot drift on the shape they read.
    """
    return {
        (lk.get("outwardIssue") or lk.get("inwardIssue") or {}).get("key")
        for lk in links
        if isinstance(lk, dict)
    }


def _wait_until_links_reflect(
    transport: Any,
    project: str,
    key: str,
    predicate: Callable[[set[str | None]], bool],
    what: str,
    timeout: float = 90.0,
) -> None:
    """Wait until the production, search-backed link map for ``key`` satisfies ``predicate``.

    A direct issue-link GET is immediately consistent, but inbound uses paged JQL. Waiting on
    that same indexed path prevents a successful write plus stale search from looking like a
    bridge defect.
    """
    deadline = time.monotonic() + timeout
    attempts = 0
    last: set[str | None] = set()
    while time.monotonic() < deadline:
        attempts += 1
        last = _linked_keys(transport.get_issuelinks_map(project).get(key) or [])
        if predicate(last):
            return
        time.sleep(2.0)
    raise AssertionError(
        f"the SEARCH-backed link read never reflected {what} on {key} within {timeout:.0f}s "
        f"({attempts} attempts). This is NOT a bridge defect — the write succeeded, the search "
        f"cannot see it yet. Last counterpart keys seen: {sorted(str(k) for k in last)}"
    )


def _plan_entries_for(repo: Path, local_id: str, key: str) -> list[dict[str, Any]]:
    """Return scoped dry-run entries naming this local-ID/Jira-key pair.

    Match ``target`` as well as ``local_id`` because plan provenance may place the Jira key in
    both fields. Local-ID-only filtering can yield an empty plan and make absence checks vacuous.
    """
    cp = _run(repo, "dry-run", only=f"{local_id},{key}")
    plan = _envelope(cp).get("plan", [])
    return [e for e in plan if key in str(e.get("target")) or e.get("local_id") in (local_id, key)]


# Inbound rows mutate Data Center, wait for indexed visibility, and assert the local ticket.


def _in_summary(tr: Any, project: str, key: str) -> str:
    value = _uniq("rebar J11 inbound summary")
    tr.update_issue(key, summary=value)
    _wait_until_search_reflects(
        tr, project, key, lambda h: (h.get("fields") or {}).get("summary") == value, "the summary"
    )
    return value


def _oracle_in_summary(ticket: dict[str, Any], expected: str) -> None:
    assert ticket.get("title") == expected, (
        f"inbound summary did not reach the local ticket: .title is "
        f"{ticket.get('title')!r}, expected {expected!r}"
    )


def _in_description(tr: Any, project: str, key: str) -> str:
    value = _uniq("rebar J11 inbound description")
    tr.update_issue(key, description=value)
    _wait_until_search_reflects(
        tr,
        project,
        key,
        lambda h: value in ((h.get("fields") or {}).get("description") or ""),
        "the description",
    )
    return value


def _oracle_in_description(ticket: dict[str, Any], expected: str) -> None:
    assert expected in (ticket.get("description") or ""), (
        f"inbound description did not reach the local ticket: .description is "
        f"{ticket.get('description')!r}, expected to contain {expected!r}"
    )


def _in_status(tr: Any, project: str, key: str) -> str:
    tr.transition_issue_by_name(key, "In Progress")
    _wait_until_search_reflects(
        tr,
        project,
        key,
        lambda h: ((h.get("fields") or {}).get("status") or {}).get("name") == "In Progress",
        "the status transition",
    )
    return "in_progress"


def _oracle_in_status(ticket: dict[str, Any], expected: str) -> None:
    assert ticket.get("status") == expected, (
        f"inbound status did not reach the local ticket: .status is "
        f"{ticket.get('status')!r}, expected {expected!r}"
    )


def _in_add_label(tr: Any, project: str, key: str) -> str:
    label = _uniq("j11inlabel")
    tr.add_label(key, label)
    _wait_until_search_reflects(
        tr,
        project,
        key,
        lambda h: label in ((h.get("fields") or {}).get("labels") or []),
        "the added label",
    )
    return label


def _oracle_in_add_label(ticket: dict[str, Any], expected: str) -> None:
    assert expected in (ticket.get("tags") or []), (
        f"inbound label did not reach the local ticket: .tags is {ticket.get('tags')!r}, "
        f"expected to contain {expected!r}"
    )


def _in_remove_label(tr: Any, project: str, key: str) -> str:
    """Add a label, let it land, then REMOVE it — the oracle is its absence.

    The add half is setup, not the assertion: a removal cell that never had the label would
    pass vacuously, so the label is first driven all the way into the index.
    """
    label = _uniq("j11rmlabel")
    tr.add_label(key, label)
    _wait_until_search_reflects(
        tr,
        project,
        key,
        lambda h: label in ((h.get("fields") or {}).get("labels") or []),
        "the label to remove (setup)",
    )
    tr.remove_label(key, label)
    _wait_until_search_reflects(
        tr,
        project,
        key,
        lambda h: label not in ((h.get("fields") or {}).get("labels") or []),
        "the label removal",
    )
    return label


def _oracle_in_remove_label(ticket: dict[str, Any], expected: str) -> None:
    assert expected not in (ticket.get("tags") or []), (
        f"the removed label is STILL on the local ticket: .tags is {ticket.get('tags')!r}, "
        f"expected {expected!r} to be absent"
    )


def _in_comment(tr: Any, project: str, key: str) -> str:
    body = _uniq("rebar J11 inbound comment")
    tr.add_comment(key, body)
    # Comments are read through a dedicated endpoint rather than the search document, so
    # wait on THAT rather than on the index reflecting a field.
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        if any(body in (c.get("body") or "") for c in tr.get_comments(key)):
            break
        time.sleep(2.0)
    else:  # pragma: no cover - only on a pathologically slow instance
        raise AssertionError(f"the comment never became readable on {key}")
    return body


def _oracle_in_comment(ticket: dict[str, Any], expected: str) -> None:
    bodies = [c.get("body") or "" for c in (ticket.get("comments") or [])]
    assert any(expected in b for b in bodies), (
        f"inbound comment did not reach the local ticket: no comment body contains "
        f"{expected!r}. Bodies seen: {[b[:60] for b in bodies]}"
    )


def _wait_until_dc_assignee_is(
    tr: Any, project: str, key: str, user: str | None, what: str
) -> None:
    """Block until the SEARCH DOCUMENT shows `key` assigned to `user` (None = unassigned).

    Row 8's cell waits on BOTH states — unassigned for its setup, then assigned for the
    mutation — and the two must not drift on the shape they read: DC carries the user under
    `fields.assignee.name`, while an unassigned issue reads back as `None` or `{}` depending on
    the endpoint. One helper, one place to be wrong.
    """
    if user is None:
        _wait_until_search_reflects(
            tr, project, key, lambda h: (h.get("fields") or {}).get("assignee") in (None, {}), what
        )
        return
    _wait_until_search_reflects(
        tr,
        project,
        key,
        lambda h: (((h.get("fields") or {}).get("assignee") or {}).get("name")) == user,
        what,
    )


def _in_unassign(tr: Any, project: str, key: str) -> str:
    """Assign, let it land, then UNASSIGN — the oracle is the empty assignee."""
    tr.update_issue(key, assignee=ADMIN_USER)
    _wait_until_search_reflects(
        tr,
        project,
        key,
        lambda h: (((h.get("fields") or {}).get("assignee") or {}).get("name")) == ADMIN_USER,
        "the assignee to clear (setup)",
    )
    tr.update_issue(key, assignee=None)
    _wait_until_search_reflects(
        tr,
        project,
        key,
        lambda h: (h.get("fields") or {}).get("assignee") in (None, {}),
        "the unassignment",
    )
    return ""


def _oracle_in_unassign(ticket: dict[str, Any], expected: str) -> None:
    assert not ticket.get("assignee"), (
        f"the local ticket is STILL assigned after an inbound unassign: .assignee is "
        f"{ticket.get('assignee')!r}"
    )


_INBOUND_CELLS: list[tuple[str, Any, Any]] = [
    ("02-edit-summary", _in_summary, _oracle_in_summary),
    ("03-edit-description", _in_description, _oracle_in_description),
    ("04-transition-status", _in_status, _oracle_in_status),
    ("05-add-label", _in_add_label, _oracle_in_add_label),
    ("06-remove-label", _in_remove_label, _oracle_in_remove_label),
    ("07-add-comment", _in_comment, _oracle_in_comment),
    # Row 8 (assign) is NOT here — it needs two passes, so it is
    # `test_inbound_assign_round_trips` below. See that cell's docstring.
    ("09-unassign", _in_unassign, _oracle_in_unassign),
]


@_skip
@_skip_no_extra
@pytest.mark.parametrize(
    ("cell_id", "mutate", "oracle"), _INBOUND_CELLS, ids=[c[0] for c in _INBOUND_CELLS]
)
def test_inbound_mutation_round_trips(
    cell_id: str,
    mutate: Any,
    oracle: Any,
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    bound_dc_issue: Any,
) -> None:
    """Exercise inbound update rows 2–7 and 9 against an imported, bound issue.

    Row 8 is separate because assignment needs two passes while this driver performs one.
    """
    local_id, key = bound_dc_issue
    dc_transport.project = jira_dc_project

    expected = mutate(dc_transport, jira_dc_project, key)

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="inbound pass")

    oracle(_local(dc_store_copy_repo, local_id), expected)


@_skip
@_skip_no_extra
def test_inbound_assign_round_trips(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, bound_dc_issue: Any
) -> None:
    """Row 8 inbound: assigning the guaranteed DC admin writes that exact local username.

    The seeded issue may already be assigned to the project lead, making reassignment and a
    truthy oracle vacuous. This standalone cell therefore clears and converges first, proves
    local emptiness, then assigns and converges again. The shared exact-equality oracle is also
    exercised by harness-free mutation tests.
    """
    local_id, key = bound_dc_issue
    dc_transport.project = jira_dc_project

    # Establish and prove an empty remote and local assignee before testing assignment; otherwise
    # the fixture's pre-seeded value could satisfy the oracle.
    dc_transport.update_issue(key, assignee=None)
    _wait_until_dc_assignee_is(dc_transport, jira_dc_project, key, None, "the unassignment (setup)")
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="the unassign setup pass")
    _assert_local_assignee_is(
        _local(dc_store_copy_repo, local_id), "", stage="SETUP (not the assignment)"
    )

    # THE MUTATION UNDER TEST — now a REAL transition, empty -> admin.
    dc_transport.update_issue(key, assignee=ADMIN_USER)
    _wait_until_dc_assignee_is(dc_transport, jira_dc_project, key, ADMIN_USER, "the assignee")

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="inbound assign pass")

    _assert_local_assignee_is(_local(dc_store_copy_repo, local_id), ADMIN_USER)


# ===========================================================================
# OUTBOUND — mutate locally, assert on the DC ISSUE
# ===========================================================================


def _out_title(repo: Path, local_id: str) -> str:
    import rebar

    value = _uniq("rebar J11 outbound title")
    rebar.edit_ticket(local_id, repo_root=repo, title=value)
    return value


def _oracle_out_title(issue: dict[str, Any], expected: str) -> None:
    got = (issue.get("fields") or {}).get("summary")
    assert got == expected, f"outbound title did not reach DC: fields.summary is {got!r}"


def _out_description(repo: Path, local_id: str) -> str:
    import rebar

    value = _uniq("rebar J11 outbound description")
    rebar.edit_ticket(local_id, repo_root=repo, description=value)
    return value


def _oracle_out_description(issue: dict[str, Any], expected: str) -> None:
    got = (issue.get("fields") or {}).get("description") or ""
    assert expected in got, f"outbound description did not reach DC: fields.description is {got!r}"


def _out_status(repo: Path, local_id: str) -> str:
    import rebar

    current = _local(repo, local_id).get("status") or "open"
    # Require the imported pre-state rather than conditionally skipping an already-satisfied
    # transition; the oracle must assert a status this helper actually created.
    assert current != "in_progress", (
        f"{local_id} is already 'in_progress' before this helper transitions it, so the status "
        f"this row asserts would be pre-existing state rather than a mutation this cell made. "
        f"Fix the fixture/ordering that pre-advanced the ticket rather than skipping the write."
    )
    rebar.transition(local_id, current, "in_progress", repo_root=repo)
    return "In Progress"


def _oracle_out_status(issue: dict[str, Any], expected: str) -> None:
    got = ((issue.get("fields") or {}).get("status") or {}).get("name")
    assert got == expected, f"outbound status did not reach DC: fields.status.name is {got!r}"


def _out_add_label(repo: Path, local_id: str) -> str:
    import rebar

    label = _uniq("j11outlabel")
    rebar.tag(local_id, label, repo_root=repo)
    return label


def _oracle_out_add_label(issue: dict[str, Any], expected: str) -> None:
    labels = (issue.get("fields") or {}).get("labels") or []
    assert expected in labels, f"outbound label did not reach DC: fields.labels is {labels!r}"


def _oracle_out_remove_label(issue: dict[str, Any], expected: str) -> None:
    labels = (issue.get("fields") or {}).get("labels") or []
    assert expected not in labels, (
        f"the removed tag is STILL on the DC issue: fields.labels is {labels!r}"
    )


def _out_comment(repo: Path, local_id: str) -> str:
    import rebar

    body = _uniq("rebar J11 outbound comment")
    rebar.comment(local_id, body, repo_root=repo)
    return body


def _oracle_out_comment(issue: dict[str, Any], expected: str) -> None:
    comments = ((issue.get("fields") or {}).get("comment") or {}).get("comments") or []
    bodies = [c.get("body") or "" for c in comments]
    assert any(expected in b for b in bodies), (
        f"outbound comment did not reach DC: no fields.comment.comments[].body contains "
        f"{expected!r}. Bodies seen: {[b[:60] for b in bodies]}"
    )


def _out_assign(repo: Path, local_id: str) -> str:
    import rebar

    identity = rebar.ensure_identity_for("jira", ADMIN_USER, ADMIN_USER, repo_root=repo)
    rebar.edit_ticket(local_id, repo_root=repo, assignee=identity)
    return ADMIN_USER


def _oracle_out_assign(issue: dict[str, Any], expected: str) -> None:
    assignee = (issue.get("fields") or {}).get("assignee") or {}
    assert assignee.get("name") == expected, (
        f"outbound assignee did not reach DC: fields.assignee is {assignee!r}, expected a user "
        f"named {expected!r}"
    )


_OUTBOUND_CELLS: list[tuple[str, Any, Any]] = [
    ("02-edit-title", _out_title, _oracle_out_title),
    ("03-edit-description", _out_description, _oracle_out_description),
    ("04-transition-status", _out_status, _oracle_out_status),
    ("05-add-label", _out_add_label, _oracle_out_add_label),
    ("07-add-comment", _out_comment, _oracle_out_comment),
    ("08-assign", _out_assign, _oracle_out_assign),
]


@_skip
@_skip_no_extra
@pytest.mark.parametrize(
    ("cell_id", "mutate", "oracle"), _OUTBOUND_CELLS, ids=[c[0] for c in _OUTBOUND_CELLS]
)
def test_outbound_mutation_round_trips(
    cell_id: str,
    mutate: Any,
    oracle: Any,
    dc_store_copy_repo: Path,
    dc_transport: Any,
    bound_dc_issue: Any,
) -> None:
    """Rows 2-8 outbound: mutate the local ticket, run a pass, assert the DC ISSUE carries it.

    Reads the issue back with `get_issue_by_rest`, i.e. from the instance rather than from any
    local projection, so the assertion cannot be satisfied by the value rebar believes it sent.
    """
    local_id, key = bound_dc_issue

    expected = mutate(dc_store_copy_repo, local_id)

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="outbound pass")

    oracle(dc_transport.get_issue_by_rest(key), expected)


@_skip
@_skip_no_extra
def test_outbound_create_stamps_both_provenance_markers(
    dc_store_copy_repo: Path, jira_dc_project: str, track_issue: Any, dc_request: Any
) -> None:
    """Row 1 outbound: a pass-created DC issue carries both provenance markers.

    Update rows cannot cover create write-back. Dedup consumes the writer's colon-form label;
    inbound correlation consumes the entity property, so both are required and the read-only
    legacy hyphen form is insufficient. Read both via raw REST, outside the writing abstraction,
    to detect an incorrectly wrapped property that the same transport might re-read consistently.
    """
    from rebar_reconciler.binding_store import load_binding_store

    import rebar

    title = _uniq("rebar J11 outbound create")
    local_id = rebar.create_ticket("task", title, repo_root=dc_store_copy_repo)

    # Scoped to the LOCAL ID alone — deliberately, and it is the one case where that is right:
    # an outbound CREATE has no Jira key yet, which is why `bound_dc_issue` has to pass both.
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=local_id)
    assert_child_ran_clean(cp, what="outbound create pass")

    key = load_binding_store(dc_store_copy_repo).get_jira_key(local_id)
    if not key:
        # A real reconcile pass distinguishes this from a direct-transport failure. A swallowed
        # create-and-bind error should leave a ``bridge_alerts`` record for the local ID.
        alerts = _assert_bridge_alert_for_mutation(cp, dc_store_copy_repo, local_id)
        if alerts:
            raise AssertionError(
                f"the outbound pass did not create-and-bind {local_id!r} (get_jira_key returned "
                f"None), and the alert store explains why: {len(alerts)} `mutation-error` "
                f"record(s) were recorded for it. Most recent reason: "
                f"{alerts[-1].get('reason')!r}. Full record(s): {alerts!r}\n"
                f"stdout:\n{cp.stdout[-1500:]}"
            )
        raise AssertionError(
            f"the outbound pass did not create-and-bind {local_id!r} (get_jira_key returned "
            f"None) — but a PROVEN-CLEAN pass (exit 0, no traceback) recorded NO `bridge_alerts` "
            f"entry for it either. This is a DIFFERENT and STRONGER finding than a swallowed "
            f"exception: nothing here indicates the create was ever ATTEMPTED, which points back "
            f"to [rebar:18a5-2bd8-3e56-4bd8]'s stage 1 (never planned) or stage 2 (planned but "
            f"not dispatched) rather than stage 3 (dispatched, then swallowed).\n"
            f"stdout:\n{cp.stdout[-1500:]}"
        )
    track_issue(key)

    status, issue = dc_request(f"/rest/api/2/issue/{key}?fields=labels")
    assert status == 200 and isinstance(issue, dict), (
        f"the created issue {key} is not readable by raw REST (HTTP {status}); the markers "
        f"cannot be asserted at all."
    )
    prop_status, prop_body = dc_request(f"/rest/api/2/issue/{key}/properties/local_id")

    _assert_outbound_provenance_markers(
        local_id, (issue.get("fields") or {}).get("labels") or [], prop_status, prop_body
    )


@_skip
@_skip_no_extra
def test_outbound_remove_label_round_trips(
    dc_store_copy_repo: Path, dc_transport: Any, bound_dc_issue: Any
) -> None:
    """Row 6 outbound, separated because it needs a converged ADD before the REMOVE.

    Written as its own cell rather than folded into the table: a removal asserted without
    first proving the label ARRIVED passes vacuously on a bridge that never wrote it.
    """
    import rebar

    local_id, key = bound_dc_issue
    label = _uniq("j11outrm")

    rebar.tag(local_id, label, repo_root=dc_store_copy_repo)
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="outbound add-label pass")
    labels = (dc_transport.get_issue_by_rest(key).get("fields") or {}).get("labels") or []
    assert label in labels, (
        f"SETUP FAILED (not the removal): the tag never reached DC, so its absence later would "
        f"prove nothing. fields.labels is {labels!r}"
    )

    rebar.untag(local_id, label, repo_root=dc_store_copy_repo)
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="outbound remove-label pass")

    _oracle_out_remove_label(dc_transport.get_issue_by_rest(key), label)


@_skip
@_skip_no_extra
def test_outbound_unassign_round_trips(
    dc_store_copy_repo: Path, dc_transport: Any, bound_dc_issue: Any
) -> None:
    """Row 9 outbound: clearing locally must leave DC ``fields.assignee`` null.

    This standalone two-pass cell first assigns and proves the remote field, preventing an
    initially unassigned issue from satisfying the clear oracle. It then clears locally and
    reads the DC post-state—not merely exit status or payload—because resolver and transport
    failures can degrade quietly. The known gap is empty-string routing through pycontribs rather
    than DC's explicit unassign path.
    """
    import rebar

    local_id, key = bound_dc_issue

    # SETUP — get the issue ASSIGNED through the bridge, and prove it landed. Reuses row 8's
    # mutate + oracle so the two cannot drift on how an assignment is expressed.
    expected = _out_assign(dc_store_copy_repo, local_id)
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="outbound assign pass")
    assigned = (dc_transport.get_issue_by_rest(key).get("fields") or {}).get("assignee") or {}
    assert assigned.get("name") == expected, (
        f"SETUP FAILED (not the unassign): the assignment never reached DC, so a null assignee "
        f"below would prove nothing — it could simply never have been set. fields.assignee is "
        f"{assigned!r}. Cell `08-assign` covers this propagation on its own; if that cell is "
        f"also red, fix it there."
    )

    # THE MUTATION UNDER TEST — clear the local assignee. An empty string is what the CLI/library
    # writes for a cleared assignee (verified: `edit_ticket(..., assignee="")` leaves `.assignee`
    # as `""`), and it is also exactly the value the differ then resolves as "unassigned".
    rebar.edit_ticket(local_id, repo_root=dc_store_copy_repo, assignee="")
    cleared_local = _local(dc_store_copy_repo, local_id).get("assignee")
    assert not cleared_local, (
        f"SETUP FAILED (not the unassign): the LOCAL assignee is still {cleared_local!r} after "
        f"an edit that clears it, so the outbound pass has no clear to carry."
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="outbound unassign pass")

    after = (dc_transport.get_issue_by_rest(key).get("fields") or {}).get("assignee")
    assert not after, (
        f"the DC issue is STILL ASSIGNED after the local assignee was cleared and the pass ran: "
        f"fields.assignee on {key} is {after!r}, expected null. The empty string reaches "
        f"`assign_issue` unchanged (`transport.py:281-303`) and pycontribs only treats "
        f"None/-1/'-1' as Unassigned — see [rebar:751e-06f1-bb0b-464c]. NOTE the failure mode "
        f"this also catches: a search on the empty string that MATCHES a user would leave the "
        f"issue assigned to an arbitrary account, which is worse than a no-op."
    )


# ---------------------------------------------------------------------------
# Link additions and removals have separate inbound and outbound cells. Each removal first proves
# the add, so absence cannot pass vacuously and failures remain attributable to one operation.


@_skip
@_skip_no_extra
def test_inbound_link_round_trips(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    bound_dc_issue: Any,
) -> None:
    """Row 10 inbound: a Jira issue-link addition surfaces as a local dependency.

    A priming pass must first bind and import the counterpart into the active local set; inbound
    link translation precedes binding adoption and skips unresolved or dormant targets. Link
    removal is asserted independently below.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    local_id, key = bound_dc_issue
    other = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 link target"))
    other_local = _jira_key_to_local_id(other)

    # Priming pass: import + bind the link TARGET, so the link pass can resolve it.
    scope = f"{local_id},{key},{other_local},{other}"
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="priming pass for the link target")
    bound_other = load_binding_store(dc_store_copy_repo).get_jira_key(other_local)
    assert bound_other == other, (
        f"SETUP FAILED: the link target {other_local} is not bound (got {bound_other!r}); an "
        f"inbound link naming an unresolvable target is skipped, not attempted."
    )
    assert _local(dc_store_copy_repo, other_local).get("ticket_id") == other_local, (
        f"SETUP FAILED: the link target {other_local} is bound but not in the ACTIVE local set; "
        f"the inbound differ refuses to mirror a dep onto a dormant counterpart."
    )

    dc_transport.set_relationship(key, other, "Blocks")
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        if dc_transport.get_issue_links(key):
            break
        time.sleep(2.0)
    else:  # pragma: no cover
        raise AssertionError(f"the issue link never became readable on {key}")

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="inbound link pass")

    deps = _local(dc_store_copy_repo, local_id).get("deps") or []
    targets = {d.get("target_id") for d in deps}
    assert other_local in targets, (
        f"the inbound Jira link did not surface as a local dep on {local_id}: deps target "
        f"{sorted(targets)}, expected to contain {other_local!r}"
    )


@pytest.mark.xfail(
    reason=(
        "DECIDED, NOT BROKEN (ticket 2b16). rebar does not mirror a peer-side link "
        "DELETION: the shipped semantics are local-wins-and-restore, so a link deleted "
        "in Jira is re-added next pass. Convergent, loses no local data, and IDENTICAL "
        "ON BOTH BACKENDS -- the inbound link differ is backend-agnostic core, Cloud has "
        "never mirrored a peer deletion either and has no equivalent cell at all. "
        "Deferred after an ecosystem review: mirroring a peer-side relationship deletion "
        "is not commonly handled. Aha! refuses it outright citing inadvertent-data-loss "
        "risk; Asana<->Jira and Workfront<->Jira leave the far item in place; Exalate "
        "treats link sync as opt-in scripted config. rebar has FIRST-HAND evidence for "
        "that caution: the sibling defect on ticket 88d9 shipped the same inference "
        "(peer absence + our own provenance marker = a deletion) and orphaned 63 tickets "
        "on its first production pass. XFAIL rather than inverted or deleted: the cell "
        "still states the behaviour a future implementation must produce, and it fails "
        "LOUDLY (xpass) the moment the removal path works -- which an inverted assertion "
        "would hide. The design and its working template are recorded on 2b16."
    ),
    strict=False,
)
@_skip
@_skip_no_extra
def test_inbound_delete_link_round_trips(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    bound_dc_issue: Any,
) -> None:
    """Row 11 inbound: deleting a Jira issue link removes the local dependency.

    First prove the link was added, then assert its absence from local state; exit zero is not
    evidence because the bridge soft-fails. This remains expected-red while inbound link diffing
    is add-only, unlike the outbound removal path; do not invert the oracle to preserve that gap.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    local_id, key = bound_dc_issue
    other = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 unlink target"))
    other_local = _jira_key_to_local_id(other)

    # Priming pass — the counterpart must be BOUND and in the ACTIVE local set before the link
    # pass, for the two reasons `test_inbound_link_round_trips` documents at length
    # (`inbound_differ.py:402-404` and `:409-412`).
    scope = f"{local_id},{key},{other_local},{other}"
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="priming pass for the link target")
    bound_other = load_binding_store(dc_store_copy_repo).get_jira_key(other_local)
    assert bound_other == other, (
        f"SETUP FAILED (not the removal): the link target {other_local} is not bound (got "
        f"{bound_other!r}); an inbound link naming an unresolvable target is skipped, not "
        f"attempted, so its later absence would prove nothing."
    )

    # SETUP — drive the link ALL THE WAY into the local ticket, and assert it got there.
    dc_transport.set_relationship(key, other, "Blocks")
    _wait_until_links_reflect(
        dc_transport, jira_dc_project, key, lambda seen: other in seen, "the link to remove"
    )
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="inbound link-add pass")
    targets = {d.get("target_id") for d in (_local(dc_store_copy_repo, local_id).get("deps") or [])}
    assert other_local in targets, (
        f"SETUP FAILED (not the removal): the inbound link never reached the local ticket, so "
        f"its absence below would prove nothing. deps on {local_id} target {sorted(targets)}, "
        f"expected to contain {other_local!r}. Row 10 "
        f"(`test_inbound_link_round_trips`) covers this add on its own; if that cell is also "
        f"red, fix it there."
    )

    # THE MUTATION UNDER TEST — delete the link in DC by its id, then prove it is gone from the
    # instance before asking rebar about it.
    link_ids = [lk.get("id") for lk in dc_transport.get_issue_links(key) if isinstance(lk, dict)]
    assert link_ids, f"SETUP FAILED: no link id to delete on {key} after the add converged"
    for link_id in link_ids:
        dc_transport.delete_issue_link(str(link_id))
    _wait_until_links_reflect(
        dc_transport, jira_dc_project, key, lambda seen: other not in seen, "the link removal"
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="inbound unlink pass")

    after = {d.get("target_id") for d in (_local(dc_store_copy_repo, local_id).get("deps") or [])}
    assert other_local not in after, (
        f"the Jira link was DELETED (confirmed absent from the search-backed link read) but the "
        f"local dep on {local_id} still targets {other_local!r}: deps target {sorted(after)}. "
        f"The inbound link differ is ADD-only (`inbound_differ.py:380,396`) — see "
        f"[rebar:2b16-9be0-a8f5-41d9]."
    )


@_skip
@_skip_no_extra
def test_outbound_link_round_trips(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    bound_dc_issue: Any,
) -> None:
    """Row 10 outbound: a local ``blocks`` addition reaches DC ``fields.issuelinks``.

    This cell covers only addition; the independent removal cell proves add then absence.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    import rebar

    local_id, key = bound_dc_issue
    other = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 outlink target"))
    other_local = _jira_key_to_local_id(other)

    # The target must be BOUND too: an outbound link can only name a Jira key the binding
    # store can resolve, so an unbound target would make the differ skip the link entirely
    # and the cell would fail for a setup reason wearing the costume of a bridge defect.
    scope = f"{local_id},{key},{other_local},{other}"
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="binding pass for the link target")
    bound_other = load_binding_store(dc_store_copy_repo).get_jira_key(other_local)
    assert bound_other == other, (
        f"SETUP FAILED: the link target {other_local} is not bound (got {bound_other!r}); an "
        f"outbound link naming an unresolvable target is skipped, not attempted."
    )

    rebar.link(local_id, other_local, "blocks", repo_root=dc_store_copy_repo)
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="outbound link pass")

    seen = _linked_keys(dc_transport.get_issue_links(key))
    assert other in seen, (
        f"the local 'blocks' link did not reach DC: fields.issuelinks on {key} names {seen}, "
        f"expected to contain {other!r}"
    )


@_skip
@_skip_no_extra
def test_outbound_delete_link_round_trips(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    bound_dc_issue: Any,
) -> None:
    """Row 11 outbound: local ``unlink`` removes the DC issue link.

    Prove the add as setup, then assert absence through a direct instance read. Exit status and
    the best-effort removal payload cannot establish the remote post-state.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    import rebar

    local_id, key = bound_dc_issue
    other = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 outunlink target"))
    other_local = _jira_key_to_local_id(other)

    # The target must be BOUND: an outbound link can only name a key the binding store resolves,
    # so an unbound target makes the differ skip the link and the cell would fail for a setup
    # reason wearing the costume of a bridge defect (see `test_outbound_link_round_trips`).
    scope = f"{local_id},{key},{other_local},{other}"
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="binding pass for the link target")
    bound_other = load_binding_store(dc_store_copy_repo).get_jira_key(other_local)
    assert bound_other == other, (
        f"SETUP FAILED (not the removal): the link target {other_local} is not bound (got "
        f"{bound_other!r}); an outbound link naming an unresolvable target is skipped, not "
        f"attempted."
    )

    # SETUP — push the link and PROVE it landed on the instance.
    rebar.link(local_id, other_local, "blocks", repo_root=dc_store_copy_repo)
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="outbound link-add pass")
    seen = _linked_keys(dc_transport.get_issue_links(key))
    assert other in seen, (
        f"SETUP FAILED (not the removal): the local 'blocks' link never reached DC, so its "
        f"absence below would prove nothing. fields.issuelinks on {key} names {seen}. Row 10 "
        f"(`test_outbound_link_round_trips`) covers this add on its own; if that cell is also "
        f"red, fix it there."
    )

    # THE MUTATION UNDER TEST — unlink locally, then read the instance back.
    # `unlink` takes NO relation argument (`rebar.unlink(id1, id2)`) — it removes the edge
    # between the pair, which is what row 11 is about.
    rebar.unlink(local_id, other_local, repo_root=dc_store_copy_repo)
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="outbound unlink pass")

    after = _linked_keys(dc_transport.get_issue_links(key))
    assert other not in after, (
        f"the local link was REMOVED but the DC issue still carries it: fields.issuelinks on "
        f"{key} names {sorted(str(k) for k in after)}, expected {other!r} to be absent. The "
        f"outbound remove path is `outbound_links._diff_link_removals` "
        f"(`outbound_links.py:120-175`) applied via `delete_issue_link` "
        f"(`transport.py:556-577`); both log rather than raise, so exit 0 says nothing here."
    )


# Rows 12-13: DC stores subtask parents in `fields.parent` and epic parents in
# the instance-discovered Epic Link field (ticket 39c1).


@_skip
@_skip_no_extra
def test_outbound_epic_parent_round_trips_via_the_epic_link(
    dc_transport: Any, jira_dc_project: str, track_issue: Any, dc_request: Any
) -> None:
    """Row 12 epic case: ``set_parent`` writes and clears the Epic Link.

    This replaces the former expected refusal now that the supported DC path exists; a standard
    issue must not use silently ignored ``fields.parent``. Assert both attach and detach through
    raw REST rather than the writer’s return value.
    """
    epic_field = _epic_link_field_id(dc_request)
    if epic_field is None:
        pytest.fail(
            "SETUP FAILED (not a rebar defect): this instance exposes no 'Epic Link' field, so "
            "the epic-parent path cannot be exercised. That is the same platform shape the "
            "transport declines on; see rebar ticket 39c1."
        )
    epic_key = _seed_epic(dc_request, dc_transport, jira_dc_project, track_issue)
    child = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 epic-child"))

    dc_transport.set_parent(child, epic_key)

    status, body = dc_request(f"/rest/api/2/issue/{child}?fields={epic_field}")
    assert status == 200 and isinstance(body, dict), (
        f"could not read {child} back to verify the Epic Link (HTTP {status})"
    )
    got = (body.get("fields") or {}).get(epic_field)
    assert got == epic_key, (
        f"the epic parent did not land: {child}'s Epic Link ({epic_field}) is {got!r}, expected "
        f"{epic_key!r}. This is the silent-no-op signature the whole ticket is about — "
        "`dispatch_one` swallows set_parent's failure, so an unchanged field is the only place "
        "it is observable."
    )

    # A CLEAR must null the SAME field, since dispatch_one routes both through this one call.
    dc_transport.set_parent(child, None)
    status, body = dc_request(f"/rest/api/2/issue/{child}?fields={epic_field}")
    cleared = (body.get("fields") or {}).get(epic_field) if isinstance(body, dict) else "<unread>"
    assert not cleared, (
        f"the epic parent was detached locally but {child}'s Epic Link still reads {cleared!r}"
    )


@_skip
@_skip_no_extra
def test_a_subtask_reparent_is_REFUSED_rather_than_silently_ignored(
    dc_transport: Any, jira_dc_project: str, track_issue: Any, dc_request: Any
) -> None:
    """Assert the explicit refusal when DC accepts but ignores a subtask reparent.

    The transport must raise ``NotImplementedError`` only after raw read-back still shows the
    original parent, and the message names requested and observed parents. That type maps the
    mutation to terminal ``outbound-parent-unrepresentable`` rather than futile retry.
    """
    subtask_type = _subtask_type_name(dc_request, jira_dc_project)
    first = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 refuse par-1"))
    second = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 refuse par-2"))
    child = _seed(
        dc_transport,
        jira_dc_project,
        track_issue,
        _uniq("rebar J11 refuse subtask"),
        issuetype=subtask_type,
        extra={"parent": {"key": first}},
    )

    # Raw probes remeasure this accept-and-ignore behavior because pycontribs hides the HTTP
    # response. They bypass `set_parent` and feed diagnostics rather than assertions.
    editmeta_status, editmeta_ops = _probe_subtask_parent_editmeta_ops(dc_request, child)
    probe_status, probe_body = _probe_subtask_parent_put(dc_request, child, second)
    update_verb_status, update_verb_body = _probe_subtask_parent_put(
        dc_request, child, second, verb="update"
    )
    probe_diagnostics = (
        f"raw PUT (fields form) for the reparent -> HTTP {probe_status}, body "
        f"{str(probe_body)[:300]}; raw PUT (update-verb form) -> HTTP {update_verb_status}, "
        f"body {str(update_verb_body)[:300]}; /editmeta for 'parent' (HTTP {editmeta_status}) "
        f"exposes operations {editmeta_ops!r}"
    )

    with pytest.raises(NotImplementedError) as excinfo:
        dc_transport.set_parent(child, second)

    message = str(excinfo.value)
    assert second in message and first in message, (
        f"the refusal does not name BOTH the requested parent ({second}) and the one still "
        f"attached ({first}), so an operator cannot tell an ignored write from a rejected one: "
        f"{message}. Raw platform probes: {probe_diagnostics}"
    )

    observed = (dc_transport.get_issue_by_rest(child).get("fields") or {}).get("parent") or {}
    assert observed.get("key") == first, (
        f"the transport refused, but {child}'s parent is {observed.get('key')!r} rather than the "
        f"original {first!r}. The refusal must be the RESULT of a read-back that found the write "
        f"ignored — if the parent actually moved, this decline is wrong and is suppressing a "
        f"mutation that worked."
    )


@_skip
@_skip_no_extra
def test_outbound_epic_parent_reaches_dc_THROUGH_A_RECONCILE_PASS(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    dc_request: Any,
) -> None:
    """Prove the parent emit and apply gates overlap on a real reconcile pass.

    A direct transport test covers only application; this cell requires the local parent to be
    an ``epic`` so the emit guard includes it, then verifies the Epic Link independently through
    raw REST. An unexpected local type is a setup failure, not evidence about application.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    import rebar

    epic_field = _epic_link_field_id(dc_request)
    if epic_field is None:
        pytest.fail(
            "SETUP FAILED (not a rebar defect): this instance exposes no 'Epic Link' field, so "
            "the epic-parent path cannot be exercised. See rebar ticket 39c1-2a32-b564-4b4b."
        )
    epic_key = _seed_epic(dc_request, dc_transport, jira_dc_project, track_issue)
    child = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 pass epic-child"))
    child_local = _jira_key_to_local_id(child)
    epic_local = _jira_key_to_local_id(epic_key)

    scope = ",".join((child_local, child, epic_local, epic_key))
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="priming pass for the hierarchy")
    bound = load_binding_store(dc_store_copy_repo).get_jira_key(child_local)
    assert bound == child, (
        f"SETUP FAILED (not the emit): the child {child_local} is not bound (got {bound!r}), so "
        f"an outbound update would take the CREATE path instead of touching {child}."
    )
    parent_type = (_local(dc_store_copy_repo, epic_local).get("ticket_type") or "").lower()
    assert parent_type == "epic", (
        f"SETUP FAILED (not the emit): the seeded epic {epic_key} imported as local ticket_type "
        f"{parent_type!r}, not 'epic'. Bug 8b25's hierarchy guard omits the parent field unless "
        f"the local parent is an epic, so the pass below would plan nothing and this cell would "
        f"be red for an import reason rather than an emit-or-apply reason."
    )
    before = dc_request(f"/rest/api/2/issue/{child}?fields={epic_field}")[1] or {}
    before_link = (before.get("fields") or {}).get(epic_field) if isinstance(before, dict) else None
    assert not before_link, (
        f"SETUP FAILED (not the emit): {child} already carries an Epic Link ({before_link!r}) "
        f"before the pass, so its presence afterwards would prove nothing."
    )

    # THE MUTATION UNDER TEST — attach the parent LOCALLY and let a real pass carry it.
    rebar.edit_ticket(child_local, repo_root=dc_store_copy_repo, parent=epic_local)
    staged = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert staged == epic_local, (
        f"SETUP FAILED (not the emit): .parent_id on {child_local} is {staged!r}, expected "
        f"{epic_local!r}, so the outbound pass has no attach to carry."
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="outbound epic-parent pass")

    status, body = dc_request(f"/rest/api/2/issue/{child}?fields={epic_field}")
    got = (body.get("fields") or {}).get(epic_field) if isinstance(body, dict) else "<unread>"
    assert got == epic_key, (
        f"A RECONCILE PASS DID NOT EMIT THE EPIC PARENT: {child}'s Epic Link ({epic_field}) is "
        f"{got!r}, expected {epic_key!r} (HTTP {status}). The transport-level cell above proves "
        f"Data Center ACCEPTS this write, so an unchanged field here means the pass never made "
        f"it — the emit gate and the apply gate are still disjoint, which is 39c1's whole "
        f"subject. `dispatch_one` swallows set_parent's failure, so the field is the only place "
        f"this is observable."
    )


@_skip
@_skip_no_extra
def test_a_repeat_pass_over_a_converged_epic_parent_plans_nothing(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    dc_request: Any,
) -> None:
    """Assert that a converged Epic Link parent is not planned again.

    Keep this separate from the round-trip and from title idempotence: Epic Link write/read
    agreement is its own path. Wait until the production, index-backed parent map sees the attach
    before requiring an empty repeat plan, so lag cannot be mistaken for churn.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    import rebar

    epic_field = _epic_link_field_id(dc_request)
    if epic_field is None:
        pytest.fail(
            "SETUP FAILED (not a rebar defect): this instance exposes no 'Epic Link' field, so "
            "the epic-parent path cannot be exercised. See rebar ticket 9bb9-56a3-e9c0-46e9."
        )
    epic_key = _seed_epic(dc_request, dc_transport, jira_dc_project, track_issue)
    child = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 epic-idem child"))
    child_local = _jira_key_to_local_id(child)
    epic_local = _jira_key_to_local_id(epic_key)

    scope = ",".join((child_local, child, epic_local, epic_key))
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="priming pass for the hierarchy")
    assert load_binding_store(dc_store_copy_repo).get_jira_key(child_local) == child, (
        f"SETUP FAILED (not idempotence): the child {child_local} is not bound."
    )

    rebar.edit_ticket(child_local, repo_root=dc_store_copy_repo, parent=epic_local)

    # Positive control: the pending local parent edit must surface for this pair; otherwise the
    # later empty filtered plan cannot prove convergence (bug 59b2, Finding B).
    pending = _plan_entries_for(dc_store_copy_repo, child_local, child)
    assert pending, (
        f"the filtered dry-run surfaced NO entry for {child_local}/{child} even though a local "
        f"parent attach is pending and DC does not yet carry it. The filter is not matching this "
        f"pair, so the emptiness asserted at the end of this cell would mean nothing."
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="converging epic-parent pass")

    # The attach must have LANDED before "a repeat plans nothing" means anything: over an
    # unconverged pair a second pass SHOULD plan work, and this cell would pass for the wrong
    # reason. Confirmed by RAW REST, independent of the pass that wrote it.
    status, body = dc_request(f"/rest/api/2/issue/{child}?fields={epic_field}")
    landed = (body.get("fields") or {}).get(epic_field) if isinstance(body, dict) else None
    assert landed == epic_key, (
        f"SETUP FAILED (not idempotence): the epic parent never reached {child} (Epic Link is "
        f"{landed!r}, HTTP {status}), so a repeat pass planning work would be CORRECT rather "
        f"than churn. That emit failure is [rebar:39c1-2a32-b564-4b4b], not this cell."
    )
    _wait_until_parent_map_reflects(
        dc_transport,
        jira_dc_project,
        child,
        lambda m: m.get(child) == epic_key,
        "the converged epic parent (before re-planning)",
    )

    mine = _plan_entries_for(dc_store_copy_repo, child_local, child)
    assert mine == [], (
        f"NOT IDEMPOTENT: with the epic parent confirmed on the instance AND visible to "
        f"get_parent_map, a repeat pass still plans {len(mine)} mutation(s) for "
        f"{child_local}/{child}: {mine[:4]}. Index lag is excluded by the wait above, so this is "
        f"real churn — the outbound writer and the inbound reader disagree about the epic "
        f"parent, and every pass will re-emit the same attach."
    )


def _named_field_id(dc_request: Any, name: str) -> str | None:
    """The id of the field called `name` on THIS instance, or None.

    `customfield_NNNNN` numbers differ per deployment, so every epic-related field has to be
    asked for by name — the same reason `_subtask_type_name` asks for the issue type rather
    than hardcoding "Sub-task".
    """
    status, body = dc_request("/rest/api/2/field")
    if status != 200 or not isinstance(body, list):
        return None
    return next(
        (str(f.get("id")) for f in body if isinstance(f, dict) and f.get("name") == name),
        None,
    )


def _epic_link_field_id(dc_request: Any) -> str | None:
    """This instance's "Epic Link" field id — the field a non-sub-task's parent lives in."""
    return _named_field_id(dc_request, "Epic Link")


def _seed_epic(dc_request: Any, dc_transport: Any, project: str, track_issue: Any) -> str:
    """Create an Epic after discovering the project type and required ``Epic Name`` field.

    Missing capabilities fail as harness setup rather than being misreported as transport defects.
    """
    status, body = dc_request(f"/rest/api/2/project/{project}")
    assert status == 200 and isinstance(body, dict), (
        f"SETUP FAILED: could not read project {project} to find its Epic type (HTTP {status})"
    )
    names = {str(it.get("name")) for it in (body.get("issueTypes") or []) if isinstance(it, dict)}
    if "Epic" not in names:
        pytest.fail(
            f"SETUP FAILED (not a rebar defect): project {project} offers no 'Epic' issue type "
            f"(has {sorted(names)}), so the epic-parent path cannot be exercised here."
        )
    epic_name_field = _named_field_id(dc_request, "Epic Name")
    if epic_name_field is None:
        pytest.fail(
            "SETUP FAILED (not a rebar defect): this instance exposes no 'Epic Name' field, "
            "which Data Center requires to create an Epic."
        )
    summary = _uniq("rebar J11 epic-parent")
    return _seed(
        dc_transport,
        project,
        track_issue,
        summary,
        issuetype="Epic",
        extra={epic_name_field: summary},
    )


def _subtask_type_name(dc_request: Any, project: str) -> str:
    """Read this project’s subtask type name from DC.

    Provisioning guarantees presence but not a forever-fixed display name. Discovery prevents a
    project-configuration mismatch from masquerading as a parent-bridge failure.
    """
    status, body = dc_request(f"/rest/api/2/project/{project}")
    assert status == 200 and isinstance(body, dict), (
        f"SETUP FAILED: could not read project {project} to discover its issue types "
        f"(HTTP {status})"
    )
    names = [
        str(it.get("name"))
        for it in (body.get("issueTypes") or [])
        if isinstance(it, dict) and it.get("subtask")
    ]
    assert names, (
        f"SETUP FAILED (not a bridge defect): project {project} exposes NO sub-task issue type "
        f"(types: {[i.get('name') for i in body.get('issueTypes') or [] if isinstance(i, dict)]}"
        f"). Rows 12-13 are about `fields.parent`, which on Data Center only a SUB-TASK has "
        f"(`transport.py:645-690`), so they cannot be exercised on a project without one."
    )
    return names[0]


def _wait_until_parent_map_reflects(
    transport: Any,
    project: str,
    key: str,
    predicate: Callable[[dict[str, str | None]], bool],
    what: str,
    timeout: float = 90.0,
) -> None:
    """Wait until the production parent map reflects ``what`` for ``key``.

    Inbound consumes this index-backed paged read, not a direct issue GET. Give predicates the
    whole mapping so a present key with no parent differs from the degradation result ``{}``; a
    clear must require membership as well as a false value.
    """
    deadline = time.monotonic() + timeout
    attempts = 0
    last: dict[str, str | None] = {}
    while time.monotonic() < deadline:
        attempts += 1
        last = transport.get_parent_map(project)
        if predicate(last):
            return
        time.sleep(2.0)
    raise AssertionError(
        f"get_parent_map never reflected {what} for {key} within {timeout:.0f}s ({attempts} "
        f"attempts). This is NOT a bridge defect — the write succeeded, the search-backed parent "
        f"map cannot see it yet (or, if the map is EMPTY below, the map read itself degraded to "
        f"{{}} per its contract). Last parent seen for {key}: {last.get(key)!r}; map holds "
        f"{len(last)} issue(s)."
    )


@_skip
@_skip_no_extra
def test_inbound_set_parent_round_trips(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    dc_request: Any,
) -> None:
    """Row 12 inbound: an instance Epic Link reaches local ``parent_id``.

    Use a parentless standard issue because DC silently ignores subtask reparenting and a prior
    local parent would trigger local-wins suppression. Prime and prove bindings, set the supported
    Epic Link, wait on the production parent map, then assert the real pass adopts the bound
    parent locally.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    epic_field = _epic_link_field_id(dc_request)
    if epic_field is None:
        pytest.fail(
            "SETUP FAILED (not a rebar defect): this instance exposes no 'Epic Link' field, so "
            "the parent path DC supports cannot be exercised. See rebar ticket 9f26."
        )
    epic_key = _seed_epic(dc_request, dc_transport, jira_dc_project, track_issue)
    child = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 adopt child"))
    child_local = _jira_key_to_local_id(child)
    epic_local = _jira_key_to_local_id(epic_key)

    scope = ",".join((child_local, child, epic_local, epic_key))
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="priming pass for the hierarchy")
    store = load_binding_store(dc_store_copy_repo)
    for local_ref, key_ref in ((child_local, child), (epic_local, epic_key)):
        bound = store.get_jira_key(local_ref)
        assert bound == key_ref, (
            f"SETUP FAILED (not the adopt): {local_ref} is not bound (got {bound!r}, expected "
            f"{key_ref!r}). An unbound parent key resolves to None and the differ SKIPS the "
            f"parent field entirely, so no mutation would even be attempted."
        )
    before = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert not before, (
        f"SETUP FAILED (not the adopt): the child's local parent_id is already {before!r} "
        f"before the instance-side set. The assertion below could then pass without the "
        f"inbound pass carrying anything, AND a local parent would be re-asserted outbound "
        f"(local-wins), suppressing the very inbound mirror this cell is about."
    )
    status, body = dc_request(f"/rest/api/2/issue/{child}?fields={epic_field}")
    pre_link = (body.get("fields") or {}).get(epic_field) if isinstance(body, dict) else "<unread>"
    assert status == 200 and not pre_link, (
        f"SETUP FAILED (not the adopt): {child} already carries an Epic Link ({pre_link!r}) "
        f"before this cell sets one (HTTP {status}), so its presence afterwards proves nothing."
    )

    # THE MUTATION UNDER TEST — set the parent on the INSTANCE, prove it landed there by a raw
    # REST read, then let a real inbound pass carry it into the local store.
    dc_transport.set_parent(child, epic_key)
    status, body = dc_request(f"/rest/api/2/issue/{child}?fields={epic_field}")
    landed = (body.get("fields") or {}).get(epic_field) if isinstance(body, dict) else "<unread>"
    assert status == 200 and landed == epic_key, (
        f"SETUP FAILED (not the bridge): set_parent returned without error but {child}'s Epic "
        f"Link ({epic_field}) reads {landed!r}, expected {epic_key!r} (HTTP {status})."
    )
    # Wait on the index-backed production `get_parent_map`, not direct GET. Inspect the whole
    # map so a degraded `{}` is not mistaken for "no parent".
    _wait_until_parent_map_reflects(
        dc_transport,
        jira_dc_project,
        child,
        lambda mapping: mapping.get(child) == epic_key,
        f"the parent set to {epic_key}",
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="inbound parent pass")

    after = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert after == epic_local, (
        f"the DC parent did not reach the local ticket: .parent_id on {child_local} is "
        f"{after!r} (it was empty before), expected {epic_local!r} — the local id of "
        f"{epic_key}, which `get_parent_map` confirms is now the child's parent on the "
        f"instance."
    )


@_skip
@_skip_no_extra
def test_inbound_clear_parent_round_trips(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    dc_request: Any,
) -> None:
    """Row 13 inbound: clearing a standard issue’s Epic Link clears local ``parent_id``.

    DC cannot null a subtask’s mandatory object-valued parent, so that platform constraint has
    a separate pin. The supported case is observable because ``get_parent_map`` also reads the
    Epic Link fallback; prove the attach before clearing it.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    epic_field = _epic_link_field_id(dc_request)
    if epic_field is None:
        pytest.fail(
            "SETUP FAILED (not a rebar defect): this instance exposes no 'Epic Link' field, so "
            "the non-sub-task parent-clear path cannot be exercised. See rebar ticket 39c1."
        )
    epic_key = _seed_epic(dc_request, dc_transport, jira_dc_project, track_issue)
    child = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 epic-detach child"))
    child_local = _jira_key_to_local_id(child)
    epic_local = _jira_key_to_local_id(epic_key)

    # SETUP — give the child an Epic Link parent, and prove it landed BEFORE priming the local
    # store, so the value the differ later clears is one this cell itself established.
    dc_transport.set_parent(child, epic_key)
    status, body = dc_request(f"/rest/api/2/issue/{child}?fields={epic_field}")
    landed = isinstance(body, dict) and body["fields"].get(epic_field) == epic_key
    assert status == 200 and landed, (
        f"SETUP FAILED (not the clear): {child}'s Epic Link ({epic_field}) never reached "
        f"{epic_key!r} (HTTP {status}, body {body!r}), so there is no parent for this cell to "
        f"observe being cleared."
    )

    scope = ",".join((child_local, child, epic_local, epic_key))
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="priming pass for the hierarchy")
    bound = load_binding_store(dc_store_copy_repo).get_jira_key(child_local)
    assert bound == child, (
        f"SETUP FAILED (not the clear): the child {child_local} is not bound (got {bound!r})"
    )
    before = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert before == epic_local, (
        f"SETUP FAILED (not the clear): the child's local parent_id is {before!r}, expected "
        f"{epic_local!r} (the local id of the seeded epic {epic_key}), so there is no parent "
        f"for this cell to observe being cleared and the oracle below would pass vacuously."
    )

    # THE MUTATION UNDER TEST — clear the Epic Link on the instance, and prove it landed there
    # (both by a direct GET and by the search-backed parent map inbound actually reads) before
    # asking rebar about it.
    dc_transport.set_parent(child, None)
    status, body = dc_request(f"/rest/api/2/issue/{child}?fields={epic_field}")
    cleared = (body.get("fields") or {}).get(epic_field) if isinstance(body, dict) else "<unread>"
    assert not cleared, (
        f"SETUP FAILED (not the bridge): set_parent(..., None) returned without error but "
        f"{child}'s Epic Link ({epic_field}) still reads {cleared!r}."
    )
    # The key must be PRESENT in the map with a falsy parent. `child not in mapping` would also
    # be "no parent seen", but it is what a DEGRADED map ({} per the contract at
    # `transport.py:520-527`) looks like, and waiting on that would make the oracle below run
    # against a read that failed.
    _wait_until_parent_map_reflects(
        dc_transport,
        jira_dc_project,
        child,
        lambda mapping: child in mapping and not mapping[child],
        "the Epic Link removal",
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="inbound clear-parent pass")

    after = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert not after, (
        f"the DC Epic Link was cleared (confirmed absent from both the direct GET and the "
        f"search-backed parent map, which 9bb9 taught to read it) but .parent_id on "
        f"{child_local} is still {after!r}. See [rebar:37e7-d751-0042-4b94] and "
        f"[rebar:9bb9-56a3-e9c0-46e9]."
    )

    # THE SMALL PIN (37e7's operator decision) — DC must still REFUSE to null a SUB-TASK's
    # `fields.parent`. This is deliberately separate from the round trip above: it is a platform
    # constraint, not a rebar defect, so it is asserted directly against the transport rather
    # than through a pass. If DC ever starts accepting this, the assertion below fails loudly and
    # reopens the sub-task coverage question instead of the gap being silently forgotten.
    subtask_type = _subtask_type_name(dc_request, jira_dc_project)
    subtask_parent = _seed(
        dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 subtask-refusal-parent")
    )
    subtask_child = _seed(
        dc_transport,
        jira_dc_project,
        track_issue,
        _uniq("rebar J11 subtask-refusal-child"),
        issuetype=subtask_type,
        extra={"parent": {"key": subtask_parent}},
    )
    with pytest.raises(Exception) as excinfo:
        dc_transport.set_parent(subtask_child, None)
    assert excinfo.value is not None, (
        f"Data Center ACCEPTED nulling sub-task {subtask_child}'s fields.parent — this used to "
        f"be refused intrinsically (see 37e7's root-cause comment); if this instance now "
        f"allows it, the sub-task clear is representable again and this ticket's rewrite "
        f"decision should be revisited."
    )


@_skip
@_skip_no_extra
def test_outbound_clear_parent_round_trips(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    dc_request: Any,
) -> None:
    """Row 13 outbound: a local detach clears the DC Epic Link.

    Subtask parent clearing is unsupported, so prime a standard child with a locally managed Epic
    relation—the provenance required for removal propagation. Prove the attach landed, detach
    locally, and verify absence by raw REST outside the writer.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    import rebar

    epic_field = _epic_link_field_id(dc_request)
    if epic_field is None:
        pytest.fail(
            "SETUP FAILED (not a rebar defect): this instance exposes no 'Epic Link' field, so "
            "the parent-clear path DC supports cannot be exercised. See rebar ticket 4b9e."
        )
    epic_key = _seed_epic(dc_request, dc_transport, jira_dc_project, track_issue)
    child = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 out-detach child"))
    child_local = _jira_key_to_local_id(child)
    epic_local = _jira_key_to_local_id(epic_key)

    scope = ",".join((child_local, child, epic_local, epic_key))
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="priming pass for the hierarchy")
    bound = load_binding_store(dc_store_copy_repo).get_jira_key(child_local)
    assert bound == child, (
        f"SETUP FAILED (not the clear): the child {child_local} is not bound (got {bound!r}), so "
        f"an outbound update would take the CREATE path instead of touching {child}."
    )
    parent_type = (_local(dc_store_copy_repo, epic_local).get("ticket_type") or "").lower()
    assert parent_type == "epic", (
        f"SETUP FAILED (not the clear): the seeded epic {epic_key} imported as local ticket_type "
        f"{parent_type!r}, not 'epic'. Bug 8b25's emit guard omits the parent field unless the "
        f"local parent is an epic, so the priming SET below would plan nothing."
    )

    # PRIMING — rebar itself attaches the parent, so the ref is MANAGED and the detach is allowed
    # to propagate. Carried by a real pass and proven landed on the instance before continuing.
    rebar.edit_ticket(child_local, repo_root=dc_store_copy_repo, parent=epic_local)
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="priming attach pass")
    status, body = dc_request(f"/rest/api/2/issue/{child}?fields={epic_field}")
    primed = (body.get("fields") or {}).get(epic_field) if isinstance(body, dict) else "<unread>"
    assert primed == epic_key, (
        f"SETUP FAILED (not the clear): rebar's priming attach did not land — {child}'s Epic "
        f"Link ({epic_field}) is {primed!r}, expected {epic_key!r} (HTTP {status}). There is no "
        f"parent for this cell to observe being cleared, and its absence later would prove "
        f"nothing."
    )

    # THE MUTATION UNDER TEST — detach locally, then read the instance back.
    # An empty value is rejected outright ("--parent requires a non-empty value (use
    # --parent=null to detach)"), and the differ needs the "parent" key PRESENT-WITH-A-FALSY-
    # VALUE to tell a CLEAR from "no parent op this mutation".
    rebar.edit_ticket(child_local, repo_root=dc_store_copy_repo, parent="null")
    cleared_local = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert not cleared_local, (
        f"SETUP FAILED (not the clear): .parent_id on {child_local} is still {cleared_local!r} "
        f"after `--parent=null`, so the outbound pass has no detach to carry."
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="outbound clear-parent pass")

    status, body = dc_request(f"/rest/api/2/issue/{child}?fields={epic_field}")
    # Prove the read succeeded before checking absence; an error response can otherwise look
    # like a cleared field.
    assert status == 200 and isinstance(body, dict) and "fields" in body, (
        f"could not read {child} back to verify the clear (HTTP {status}, body "
        f"{str(body)[:200]}) — an unreadable issue must not be reported as a cleared one."
    )
    after = (body.get("fields") or {}).get(epic_field)
    assert not after, (
        f"the local parent was DETACHED but {child}'s Epic Link ({epic_field}) still reads "
        f"{after!r} (HTTP {status}). Candidates, in order: the differ never emitted the clear "
        f"(check that the attach above made the ref MANAGED — `_parent_clear_is_managed` gates "
        f"on it); `set_parent` raised and was swallowed (`dispatch_one` warns and continues); or "
        f"Data Center accepted the Epic Link null and ignored it — which would contradict "
        f"`test_outbound_epic_parent_round_trips_via_the_epic_link`, green on the same instance."
    )


@_skip
@_skip_no_extra
def test_outbound_unrepresentable_parent_is_REPORTED_rather_than_silently_dropped(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    dc_request: Any,
) -> None:
    """Report an unrepresentable outbound parent durably without writing it.

    A non-epic local parent is suppressed by the emit guard before transport application, so a
    real pass must create an ``outbound-field-dropped`` alert naming the issue and ``parent``.
    Raw REST must also show neither ``fields.parent`` nor Epic Link was written. This complements
    the direct transport-refusal cell by asserting the user-visible pass outcome.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    import rebar

    # Resolved up front because the "nothing was written" oracle below reads it: the Epic Link
    # is the field a write for THIS child shape would actually land in, so without it that
    # assertion cannot fail.
    epic_field = _epic_link_field_id(dc_request)
    if epic_field is None:
        pytest.fail(
            "SETUP FAILED (not a rebar defect): this instance exposes no 'Epic Link' field, so "
            "the 'nothing was written' half of this cell could not be asserted against the "
            "field a write would land in. See rebar ticket 9f26."
        )
    parent = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 unrep parent"))
    child = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 unrep child"))
    child_local = _jira_key_to_local_id(child)
    parent_local = _jira_key_to_local_id(parent)

    scope = ",".join((child_local, child, parent_local, parent))
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="priming pass for the hierarchy")
    store = load_binding_store(dc_store_copy_repo)
    for local_ref, key_ref in ((child_local, child), (parent_local, parent)):
        bound = store.get_jira_key(local_ref)
        assert bound == key_ref, (
            f"SETUP FAILED (not the emit): {local_ref} is not bound (got {bound!r}), so an "
            f"outbound update would take the CREATE path instead of touching {key_ref}."
        )
    parent_type = (_local(dc_store_copy_repo, parent_local).get("ticket_type") or "").lower()
    assert parent_type and parent_type != "epic", (
        f"SETUP FAILED (not the emit): the seeded parent {parent} imported as local ticket_type "
        f"{parent_type!r}. The suppression under test fires only for a NON-epic parent, so an "
        f"epic here would emit the parent normally and this cell would prove nothing."
    )

    # THE MUTATION UNDER TEST — the user attaches a parent the tracker cannot hold for this
    # child shape, and a real pass carries (or drops) it.
    rebar.edit_ticket(child_local, repo_root=dc_store_copy_repo, parent=parent_local)
    staged = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert staged == parent_local, (
        f"SETUP FAILED (not the emit): .parent_id on {child_local} is {staged!r}, expected "
        f"{parent_local!r}, so the outbound pass has no attach to drop."
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert_child_ran_clean(cp, what="outbound unrepresentable-parent pass")

    # (1) THE OPERATOR IS TOLD. `assert_bridge_alert_for_mutation` refuses to read the store at
    # all unless the pass provably completed — an absent alert directory is otherwise
    # indistinguishable from "nobody looked".
    dedup_key = f"outbound-field-dropped:{child}:parent"
    alerts = _assert_bridge_alert_for_mutation(cp, dc_store_copy_repo, child_local, key=dedup_key)
    assert any(a.get("key") == dedup_key for a in alerts), (
        f"the pass DROPPED the parent attach for {child_local} -> {parent_local} and recorded "
        f"NOTHING: no `{dedup_key}` in {alerts!r}. The pass exited 0 and reported convergence "
        f"while the user's hierarchy edit was discarded, which is exactly the silent-divergence "
        f"failure 39c1 made durable for the apply side and this ticket closes for the emit side."
    )

    # An alert is insufficient if a write occurred. Check raw REST rather than the writing
    # transport, and read both fields: `fields.parent` is always empty for a standard issue,
    # while an attempted parent write would target the Epic Link.
    assert epic_field is not None  # narrowed by the SETUP guard above
    status, body = dc_request(f"/rest/api/2/issue/{child}?fields=parent,{epic_field}")
    assert status == 200 and isinstance(body, dict) and "fields" in body, (
        f"could not read {child} back to verify nothing was written (HTTP {status}, body "
        f"{str(body)[:200]}) — an unreadable issue must not be mistaken for an unwritten one."
    )
    fields = body.get("fields") or {}
    wrote_parent = fields.get("parent")
    wrote_link = fields.get(epic_field)
    assert not wrote_parent and not wrote_link, (
        f"rebar reported the dropped parent AND still wrote one: {child} now carries "
        f"fields.parent = {wrote_parent!r} and Epic Link ({epic_field}) = {wrote_link!r}. The "
        f"suppression exists because this parent shape cannot be represented on Data Center, "
        f"so emitting it anyway reintroduces the very silent no-op the alert warns about."
    )


# ---------------------------------------------------------------------------
# Row 14 — deletion never plans a teardown of the local side (ADR 0028 §1)
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_a_deleted_dc_issue_never_plans_a_local_teardown(
    dc_store_copy_repo: Path, dc_transport: Any, bound_dc_issue: Any
) -> None:
    """Row 14 inbound: remote absence must not plan local teardown.

    Snapshot absence is not deletion under ADR 0028; only bounded direct-GET grace can prove it.
    Use a dry-run because a writing pass would separately plan a duplicate create, then assert no
    destructive local action and that the ticket survives.
    """
    local_id, key = bound_dc_issue

    # Positive control: a pending local edit must reach this filtered plan before deletion.
    # Revert it afterward so the no-teardown verdict examines the converged pair (bug 59b2).
    import rebar as _rebar

    _probe_title = _uniq("rebar J11 row14 filter probe")
    _original_title = _local(dc_store_copy_repo, local_id).get("title")
    _rebar.edit_ticket(local_id, repo_root=dc_store_copy_repo, title=_probe_title)
    _reach = _plan_entries_for(dc_store_copy_repo, local_id, key)
    _rebar.edit_ticket(local_id, repo_root=dc_store_copy_repo, title=_original_title)
    assert _reach, (
        f"the filtered dry-run surfaced NO entry for {local_id}/{key} with a local edit pending, "
        f"so `--filter-local-ids` is not matching this pair and the teardown-absence asserted "
        f"below would be vacuous. The surviving-ticket assertion at the end of this cell is NOT a "
        f"sufficient backstop: if the filter matches nothing the pass does nothing, so the ticket "
        f"survives for the wrong reason and both assertions pass together."
    )

    dc_transport.delete_issue(key)

    cp = _run(dc_store_copy_repo, "dry-run", only=f"{local_id},{key}")
    plan = _envelope(cp).get("plan", [])
    mine = [e for e in plan if e.get("local_id") == local_id or key in str(e.get("target"))]
    teardown = [
        e
        for e in mine
        if e.get("action") in ("delete", "retire", "archive")
        or (e.get("direction") == "inbound" and e.get("action") == "conflict")
    ]
    assert not teardown, (
        f"deleting {key} planned a local teardown for {local_id}, but ADR 0028 §1 forbids any "
        f"destructive action driven by snapshot absence. Teardown entries: {teardown}. "
        f"All entries for this pair: {mine}"
    )

    survivor = _local(dc_store_copy_repo, local_id)
    assert survivor.get("ticket_id") == local_id, (
        f"the local ticket {local_id} did not survive a pass over its hard-deleted DC partner"
    )
    assert survivor.get("status") not in ("deleted", "archived"), (
        f"the local ticket {local_id} was torn down to {survivor.get('status')!r} because its "
        f"DC partner was deleted — ADR 0028 §1 forbids acting on absence alone"
    )


@_skip
@_skip_no_extra
def test_outbound_delete_leaves_the_issue_absent_by_key_AND_by_id(
    dc_transport: Any, jira_dc_project: str, track_issue: Any, dc_request: Any
) -> None:
    """Row 14 outbound primitive: deletion leaves the issue absent by key and numeric ID.

    No differ emits outbound deletion, so exercise the reachable transport operation directly.
    Prove both handles return 200 before deletion and 404 afterward; key-only absence could be a
    project move and re-key, while return-without-error cannot prove the post-state.
    """
    key = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 outbound delete"))

    raw = dc_transport.get_issue_by_rest(key)
    numeric_id = str(raw.get("id") or "")
    assert numeric_id.isdigit(), (
        f"SETUP FAILED (not the deletion): {key} carries no numeric id ({numeric_id!r}), so the "
        f"by-id half of the 7c26 pair cannot be asserted at all."
    )
    for handle, what in ((key, "key"), (numeric_id, "numeric id")):
        status, _body = dc_request(f"/rest/api/2/issue/{handle}")
        assert status == 200, (
            f"SETUP FAILED (not the deletion): {key} is not readable by {what} {handle!r} "
            f"BEFORE the delete (HTTP {status}), so a 404 afterwards would not be this cell's "
            f"doing."
        )

    dc_transport.delete_issue(key)

    for handle, what in ((key, "key"), (numeric_id, "numeric id")):
        status, body = dc_request(f"/rest/api/2/issue/{handle}")
        assert status == 404, (
            f"{key} is STILL REACHABLE by {what} {handle!r} after delete_issue (HTTP {status}) "
            f"— the deletion did not take, or the issue was MOVED and re-keyed rather than "
            f"deleted (bug 7c26: an old key 404s either way, which is why this cell asks by "
            f"both handles). Body: {str(body)[:300]}"
        )


# ---------------------------------------------------------------------------
# The identity criterion — asserted POSITIVELY, not as "nothing raised"
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_the_inbound_assignee_mints_a_jira_family_identity(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, bound_dc_issue: Any
) -> None:
    """Assert that inbound assignment mints one shared-family placeholder identity.

    First unassign and remove the binding pass’s mapping, proving read-only resolution is absent.
    After assigning the guaranteed admin and running the pass, require ``jira`` resolution, no
    ``jira-datacenter`` fork, and placeholder status. Ticket-field username equality is a separate
    oracle because local ``assignee`` never stores the registry ID.
    """
    import rebar

    local_id, key = bound_dc_issue
    dc_transport.project = jira_dc_project

    # SETUP, PART 1 — TAKE THE ASSIGNEE AWAY, so the pass under test has one to carry.
    # `bound_dc_issue`'s seeded issue arrives ALREADY assigned to the harness admin (the
    # project is created with `lead=admin` and no `assigneeType`, so DC default-assigns to the
    # project lead), and its binding pass therefore already imported that assignee. Re-assigning
    # the same user would leave the inbound differ nothing to report — `_assignee_matches`
    # (`inbound_fields.py:102-128`) short-circuits an unchanged assignee — and the mint at
    # `apply_inbound_records.py:369` only runs when `"assignee" in fields`. Unassigning first
    # makes the later assignment a REAL transition. That this both works and propagates is not
    # assumed: it is what cell `09-unassign` (`_in_unassign` / `_oracle_in_unassign` above)
    # exercises, and it is green on the harness.
    dc_transport.update_issue(key, assignee=None)
    _wait_until_search_reflects(
        dc_transport,
        jira_dc_project,
        key,
        lambda h: (h.get("fields") or {}).get("assignee") in (None, {}),
        "the unassignment (setup)",
    )
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="the unassign setup pass")
    cleared = _local(dc_store_copy_repo, local_id).get("assignee")
    assert not cleared, (
        f"SETUP FAILED (not a product finding): the local ticket is still assigned to {cleared!r} "
        f"after an inbound unassign, so the assignment this cell is about to make would not be a "
        f"CHANGE and the pass would have no assignee to mint from. Cell `09-unassign` covers this "
        f"propagation on its own; if that cell is also red, fix it there."
    )

    # SETUP, PART 2 — ESTABLISH THE ABSENCE THE ORACLE ASSERTS, then assert it.
    # The mapping is NOT left behind by the scrub: every identity on the real `tickets` branch
    # carries `mappings: []`, so the copied store maps no Jira user at all. It is minted DURING
    # this test, by `bound_dc_issue`'s binding pass importing that default assignee
    # (`apply_inbound_records.py:200-203`). So "pick a user the scrub leaves unmapped" is not
    # available — the fixture re-mints whichever user its issue is assigned to, and the harness
    # admin is the ONE user guaranteed to exist (`_dc_support.py:28-31`). The cell therefore
    # removes that one mapping itself and then asserts the absence it just created. The
    # assertion is NOT decoration: it fails if the removal did not take, if a second identity
    # also carries the mapping, or if `resolve_mapping` ever stops being a pure read — each of
    # which would let the post-pass check pass vacuously, which is the tautology this oracle
    # was rewritten to remove.
    _forget_identity_mapping(dc_store_copy_repo, "jira", ADMIN_USER)
    pre_existing = rebar.resolve_mapping("jira", ADMIN_USER, repo_root=dc_store_copy_repo)
    assert pre_existing is None, (
        f"SETUP FAILED (not a product finding): the store copy STILL maps jira/{ADMIN_USER!r} "
        f"to {pre_existing!r} after this cell removed every identity carrying that mapping, so a "
        f"mapping afterwards would prove nothing about this pass."
    )

    dc_transport.update_issue(key, assignee=ADMIN_USER)
    _wait_until_search_reflects(
        dc_transport,
        jira_dc_project,
        key,
        lambda h: (((h.get("fields") or {}).get("assignee") or {}).get("name")) == ADMIN_USER,
        "the assignee",
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="inbound assignee pass")

    _assert_mint_registered(dc_store_copy_repo, ADMIN_USER)

    # And, separately, that the human-readable name reached the ticket — the OTHER half of the
    # additive contract. Asserted as its own statement so "no identity" and "no assignee" are
    # never reported as one failure.
    assignee = _local(dc_store_copy_repo, local_id).get("assignee")
    assert assignee, (
        f"the inbound assignee did not reach the local ticket: .assignee on {local_id} is "
        f"{assignee!r} (the identity mint is additive — it must not be the only thing that lands)"
    )


# ---------------------------------------------------------------------------
# Pagination — the defect class that silently lost 92% of a snapshot, twice
# ---------------------------------------------------------------------------


def _observed_page_size(dc_request: Any, project: str) -> int:
    """Read the server-applied page size from its echoed ``maxResults``.

    An oversized request exposes DC’s silent clamp even when the project contains few issues.
    """
    status, body = dc_request(
        f"/rest/api/2/search?jql=project%3D{project}&maxResults=100000&fields=key"
    )
    assert status == 200 and body is not None, f"search for the page size failed: {status}"
    return int(body.get("maxResults") or 0)


@_skip
@_skip_no_extra
def test_the_inbound_snapshot_survives_multi_page_pagination(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    dc_request: Any,
) -> None:
    """Seed beyond two effective pages and require every issue in the inbound plan.

    Derive the target from the server-echoed and reconciler page sizes to force three pages. Run
    unfiltered to exercise fetching but dry-run for safety over the unbound store copy. This guards
    against advancing by requested size or treating a server-clamped short page as EOF.
    """
    server_page = _observed_page_size(dc_request, jira_dc_project)
    reconciler_page = 100  # fetcher._iter_pages' default, and what every caller passes
    effective = min(server_page, reconciler_page) if server_page else reconciler_page
    target = 2 * effective + 1
    print(
        f"[j11-pagination] server-echoed maxResults={server_page}; reconciler page_size="
        f"{reconciler_page}; effective={effective}; seeding {target} issues to force "
        f"{-(-target // effective)} pages"
    )

    dc_transport.project = jira_dc_project
    seeded: list[str] = []
    for i in range(target):
        created = dc_transport.create_issue(
            {"summary": f"rebar J11 pagination {i:04d}", "issuetype": "Task"}
        )
        key = created["key"]
        track_issue(key)
        seeded.append(key)

    # Wait until raw REST counts every indexed issue; seeing only the last key would not prove
    # earlier visibility. This independent pager keeps `_paged_search` (ticket 9263) from
    # validating its own precondition.
    deadline = time.monotonic() + 300.0
    indexed = 0
    while time.monotonic() < deadline:
        indexed = _raw_indexed_issue_count(dc_request, jira_dc_project)
        if indexed >= target:
            break
        time.sleep(5.0)
    print(f"[j11-pagination] indexed {indexed} of {target} seeded issues (raw REST count)")
    assert indexed >= target, (
        f"only {indexed} of {target} seeded issues became searchable within 300s — the index is "
        f"lagging further than this suite allows. NOT a pagination defect: this count is taken "
        f"over RAW REST paging, independent of `_paged_search`, so it is the index and not the "
        f"fix under test that is short."
    )

    cp = _run(dc_store_copy_repo, "dry-run")
    plan = _envelope(cp).get("plan", [])
    # Match on `target` — see `_plan_entries_for`. The envelope's `local_id` carries the JIRA KEY
    # for these entries, so the original filter (derived local id vs `local_id`) matched nothing
    # and reported "0 of 201 recovered", which reads as total data loss and was purely this bug.
    planned = {
        str(e.get("target"))
        for e in plan
        if e.get("direction") == "inbound" and e.get("action") == "create"
    }
    missing = [k for k in seeded if k not in planned]
    print(f"[j11-pagination] recovered {target - len(missing)} of {target} seeded issues")
    assert not missing, (
        f"the inbound fetch recovered only {target - len(missing)} of {target} seeded issues — "
        f"{len(missing)} were silently LOST across page boundaries (this is the deac/9263 "
        f"truncation signature). First missing: {missing[:5]}"
    )


# ---------------------------------------------------------------------------
# "This project's live Jira is untouched" — as a FILE-CONTENT check
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_no_config_in_the_working_repo_points_anywhere_but_the_harness(
    dc_store_copy_repo: Path,
) -> None:
    """Require every repository ``base_url`` assignment to name only the harness.

    This complements credential-environment checks: a stray configured production URL can redirect
    a writing pass. Compare the complete set, not merely the presence of the expected URL.
    """
    import shutil

    # Positive control: `collect_base_urls` must find both URLs in a decoy tree; otherwise an
    # empty or broken collector could make the real-copy equality check pass (bug 59b2).
    decoy_root = dc_store_copy_repo / ".j11-decoy"
    (decoy_root / ".rebar").mkdir(parents=True, exist_ok=True)
    (decoy_root / "rebar.toml").write_text(f'[reconciler]\nbase_url = "{BASE}"\n')
    (decoy_root / ".rebar" / "nested.toml").write_text(
        '[reconciler]\nbase_url = "https://real-jira.example.com"\n'
    )
    decoy_found = collect_base_urls(decoy_root)
    assert set(decoy_found) == {BASE, "https://real-jira.example.com"}, (
        f"the base_url collector failed its own positive control: over a decoy tree containing a "
        f"foreign URL in .rebar/nested.toml it found {decoy_found!r}. Until this passes, the "
        f"assertion below cannot be read as evidence of anything."
    )
    shutil.rmtree(decoy_root)

    found = collect_base_urls(dc_store_copy_repo)
    assert set(found) == {BASE}, (
        f"the working repo names base_url(s) {found!r}; the ONLY permitted value is the harness "
        f"URL {BASE!r}. Anything else means a pass from this copy could reach a real instance."
    )


# ---------------------------------------------------------------------------
# Idempotence — ITS OWN CELL, deliberately, and separate from every round-trip
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_a_repeat_pass_over_a_converged_pair_plans_nothing(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, bound_dc_issue: Any
) -> None:
    """Require an indexed, converged pair to produce an empty repeat plan.

    Keep idempotence separate from round-trip fields so churn cannot obscure a successful mutation.
    Wait until JQL sees the applied title before replanning; otherwise stale search looks like
    genuine work.
    """
    import rebar

    local_id, key = bound_dc_issue
    new_title = _uniq("rebar J11 idempotence")

    rebar.edit_ticket(local_id, repo_root=dc_store_copy_repo, title=new_title)

    # Positive control: the pending title edit must surface for this pair; otherwise the later
    # empty filtered plan cannot prove idempotence (bug 59b2, Finding B).
    pending = _plan_entries_for(dc_store_copy_repo, local_id, key)
    assert pending, (
        f"the filtered dry-run surfaced NO entry for {local_id}/{key} even though a local title "
        f"edit is pending and DC still holds the old summary. The filter is not matching this "
        f"pair, so the emptiness asserted at the end of this cell would mean nothing."
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="the converging pass")

    # The write must have LANDED before "a repeat plans nothing" means anything: over an
    # unconverged pair a second pass SHOULD plan work, and the cell would pass or fail for the
    # wrong reason.
    remote = dc_transport.get_issue_by_rest(key)
    assert (remote.get("fields") or {}).get("summary") == new_title, (
        "SETUP FAILED (not idempotence): the edit never reached DC, so a repeat pass planning "
        "work would be correct rather than churn."
    )
    _wait_until_search_reflects(
        dc_transport,
        jira_dc_project,
        key,
        lambda h: (h.get("fields") or {}).get("summary") == new_title,
        "the converged summary (before re-planning)",
    )

    mine = _plan_entries_for(dc_store_copy_repo, local_id, key)
    assert mine == [], (
        f"NOT IDEMPOTENT: with the write confirmed on the instance AND visible to search, a "
        f"repeat pass still plans {len(mine)} mutation(s) for {local_id}/{key}: {mine[:4]}. "
        f"Index lag is excluded by the wait above, so this is real churn."
    )
