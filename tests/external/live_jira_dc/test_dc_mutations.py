"""J11 — the COMPREHENSIVE mutation table: 14 mutations x 2 directions, live against a real
Jira Data Center instance, over a scrubbed copy of the project's real ticket store
(epic e369, ticket 5200-e04e-246e-4aae).

WHAT THIS ADDS OVER THE THIN SLICE. `test_store_copy_isolation.py` proves the two ENDS — an
issue created in DC reaches the local store, and a local edit reaches the DC issue. That is the
epic's headline criterion and it is green. It is not the same claim as "the bridge round-trips
the things a ticket actually consists of": a bridge can carry a title and silently drop labels,
comments, assignees, links and parents. Each row below asserts ONE observable, so a regression
names the field it broke instead of reporting an aggregate "converged".

EVERY CELL IS ITS OWN TEST. The rows are parametrized rather than looped inside one function,
so pytest reports 28 independent verdicts. That is deliberate and it is the plan's requirement:
a single test asserting fourteen things fails on the first one and hides the other thirteen,
which is precisely how this epic lost time before ("counts are not causes" — three live runs
reported the same 2-failed/12-passed with three different causes).

READ DIRECTION MATTERS. An inbound cell mutates through the DC REST API and reads the LOCAL
ticket; an outbound cell mutates locally and reads the DC ISSUE. In both cases the value
asserted is the value the cell itself wrote and is unique per run, so no cell can pass by
re-reading an unchanged field.

TWO HAZARDS THAT SHAPE EVERY INBOUND CELL:
  1. INDEX LAG. The fetch finds issues through a JQL search and Jira's Lucene index is
     eventually consistent, so a field written a moment ago may not be visible to the pass yet.
     Every inbound cell therefore waits until a SEARCH REFLECTS THE NEW VALUE — not merely
     until the key is searchable, which is a weaker condition that let an earlier cell run
     against a stale document. When the wait times out it says so, so a timing artefact is
     never reported as a bridge defect.
  2. SCOPING IS MANDATORY. The scrub removes every binding, so an UNSCOPED writing pass would
     route all ~2,700 copied tickets down the CREATE path and file them into the harness. Every
     writing pass here is scoped with `--filter-local-ids <local_id>,<key>`; the only unscoped
     passes are DRY-RUNS, which write nothing.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _dc_support import ADMIN_USER, BASE, live_jira_ready
from _dc_support import assert_mint_registered as _assert_mint_registered
from _dc_support import envelope as _envelope
from _dc_support import forget_identity_mapping as _forget_identity_mapping
from _dc_support import read_local_ticket as _local
from _dc_support import run_reconcile as _run
from _dc_support import seed_searchable_issue as _seed
from _dc_support import skip_no_extra as _skip_no_extra
from _dc_support import skip_no_harness as _skip

# THE ALL-SKIP CANARY KEYS ON THIS NAME. `tests/external/conftest.py`'s
# `pytest_collection_modifyitems` applies the `jira_live` marker only to modules that define a
# module-level `_live_jira_ready`, and the canary then fails a run in which live tests were
# COLLECTED but none EXECUTED. Refactoring this helper into `_dc_support` silently removed this
# module from that bookkeeping — so the cells carrying the epic's headline evidence could all-skip
# and the run would be green and silent. Re-exported under the name the canary looks for.
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
    """Block until a JQL SEARCH returns `key` in a state satisfying `predicate`.

    STRONGER THAN `wait_until_searchable`, and the difference is the whole point. That helper
    waits for the key to EXIST in the index; this one waits for the index to reflect the
    specific CHANGE the cell just made. The inbound pass reads fields off the search result
    (`transport.search_issues` returns the unwrapped issues), so a cell that only waits for
    existence can hand the differ a stale document and then report "the change did not reach
    the local store" — a bridge defect that never happened.
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


def _plan_entries_for(repo: Path, local_id: str, key: str) -> list[dict[str, Any]]:
    """Scoped dry-run plan entries naming this pair.

    MATCHES ON `target`, NOT on `local_id`. The envelope's `local_id` field is populated from
    `provenance["local_id"]` (`reconcile_helpers._build_plan_entries`) and for these entries it
    carries the JIRA KEY, not the rebar local id — observed directly in run 30721408463, whose
    outbound entries read `{'target': 'RBJISZB-1', 'local_id': 'RBJISZB-1'}`. A filter keying on
    the derived local id alone therefore matches NOTHING, which is how the pagination cell
    reported a suspiciously round "0 of 201 recovered" and nearly became a false data-loss alarm.
    The thin slice's `test_the_inbound_create_is_PLANNED_for_a_new_dc_issue` already gets this
    right by ORing on `target`; this mirrors it.
    """
    cp = _run(repo, "dry-run", only=f"{local_id},{key}")
    plan = _envelope(cp).get("plan", [])
    return [e for e in plan if key in str(e.get("target")) or e.get("local_id") in (local_id, key)]


# ===========================================================================
# INBOUND — mutate in Data Center, assert on the LOCAL ticket
# ===========================================================================
#
# Each row is (id, mutate, oracle). `mutate` performs the DC-side change and returns the
# value the oracle expects; it is also responsible for waiting until the index reflects it.


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


def _in_assign(tr: Any, project: str, key: str) -> str:
    tr.update_issue(key, assignee=ADMIN_USER)
    _wait_until_search_reflects(
        tr,
        project,
        key,
        lambda h: (((h.get("fields") or {}).get("assignee") or {}).get("name")) == ADMIN_USER,
        "the assignee",
    )
    return ADMIN_USER


def _oracle_in_assign(ticket: dict[str, Any], expected: str) -> None:
    """The local `.assignee` is a rebar IDENTITY id, not the raw DC username.

    So the oracle is not `== "admin"`. It is that the field is populated AND that it is the
    id the identity registry mints for this DC user under the SHARED `jira` family — which is
    the epic's shared-identity decision, asserted positively in
    `test_the_inbound_assignee_mints_a_jira_family_identity` below.
    """
    assert ticket.get("assignee"), (
        f"inbound assignee did not reach the local ticket: .assignee is "
        f"{ticket.get('assignee')!r} (expected the minted identity for {expected!r})"
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
    ("08-assign", _in_assign, _oracle_in_assign),
    ("09-unassign", _in_unassign, _oracle_in_unassign),
]


@_skip
@_skip_no_extra
@pytest.mark.parametrize(
    "cell_id,mutate,oracle", _INBOUND_CELLS, ids=[c[0] for c in _INBOUND_CELLS]
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
    """Rows 2-9 inbound: mutate in DC, run a pass, assert the LOCAL ticket carries it.

    `bound_dc_issue` supplies an issue that is already imported and BOUND, so this cell
    exercises the UPDATE path on an existing ticket rather than re-testing the create path
    (row 1, which the thin slice already covers end to end).
    """
    local_id, key = bound_dc_issue
    dc_transport.project = jira_dc_project

    expected = mutate(dc_transport, jira_dc_project, key)

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert "Traceback" not in cp.stderr, f"inbound pass raised:\n{cp.stderr[-2000:]}"

    oracle(_local(dc_store_copy_repo, local_id), expected)


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
    if current != "in_progress":
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
    "cell_id,mutate,oracle", _OUTBOUND_CELLS, ids=[c[0] for c in _OUTBOUND_CELLS]
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
    assert "Traceback" not in cp.stderr, f"outbound pass raised:\n{cp.stderr[-2000:]}"

    oracle(dc_transport.get_issue_by_rest(key), expected)


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
    assert "Traceback" not in cp.stderr, f"outbound add-label pass raised:\n{cp.stderr[-2000:]}"
    labels = (dc_transport.get_issue_by_rest(key).get("fields") or {}).get("labels") or []
    assert label in labels, (
        f"SETUP FAILED (not the removal): the tag never reached DC, so its absence later would "
        f"prove nothing. fields.labels is {labels!r}"
    )

    rebar.untag(local_id, label, repo_root=dc_store_copy_repo)
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert "Traceback" not in cp.stderr, f"outbound remove-label pass raised:\n{cp.stderr[-2000:]}"

    _oracle_out_remove_label(dc_transport.get_issue_by_rest(key), label)


# ---------------------------------------------------------------------------
# Rows 10-11 — links, in both directions
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_inbound_link_round_trips(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    bound_dc_issue: Any,
) -> None:
    """Rows 10-11 inbound: a Jira issue link surfaces as a local dep, and its removal removes it.

    THE FAR END MUST BE BOUND *AND* LOCALLY PRESENT BEFORE THE LINK PASS. The inbound link
    translator resolves the counterpart through the binding store and skips an unresolvable
    one — `inbound_differ.py:402-404`, "unbound — retry next pass" — and then skips again when
    the counterpart is missing from the pass's active local set (`inbound_differ.py:409-412`,
    built once at pass start from `rebar list`). Neither can be satisfied within the SAME pass
    that first imports the target: the inbound differ runs before the binding walk that adopts
    it (`run_differs.py:586` then `:688`). A single-pass version of this cell therefore
    asserted an outcome that is structurally unreachable and reported an empty `deps` as a
    bridge defect. `test_outbound_link_round_trips` below already carries the same priming
    pass, with the same reasoning, which is why it passes.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    local_id, key = bound_dc_issue
    other = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 link target"))
    other_local = _jira_key_to_local_id(other)

    # Priming pass: import + bind the link TARGET, so the link pass can resolve it.
    scope = f"{local_id},{key},{other_local},{other}"
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert "Traceback" not in cp.stderr, f"priming pass for the link target raised:\n{cp.stderr}"
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
    assert "Traceback" not in cp.stderr, f"inbound link pass raised:\n{cp.stderr[-2000:]}"

    deps = _local(dc_store_copy_repo, local_id).get("deps") or []
    targets = {d.get("target_id") for d in deps}
    assert other_local in targets, (
        f"the inbound Jira link did not surface as a local dep on {local_id}: deps target "
        f"{sorted(targets)}, expected to contain {other_local!r}"
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
    """Rows 10-11 outbound: a local `blocks` link surfaces in `fields.issuelinks`, and unlink
    removes it. Both halves in one cell because the removal is only meaningful after the add
    has been PROVEN to land — asserted separately below so the two do not blur."""
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
    assert "Traceback" not in cp.stderr, f"binding pass for the link target raised:\n{cp.stderr}"
    bound_other = load_binding_store(dc_store_copy_repo).get_jira_key(other_local)
    assert bound_other == other, (
        f"SETUP FAILED: the link target {other_local} is not bound (got {bound_other!r}); an "
        f"outbound link naming an unresolvable target is skipped, not attempted."
    )

    rebar.link(local_id, other_local, "blocks", repo_root=dc_store_copy_repo)
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert "Traceback" not in cp.stderr, f"outbound link pass raised:\n{cp.stderr[-2000:]}"

    links = dc_transport.get_issue_links(key)
    seen = {
        (lk.get("outwardIssue") or lk.get("inwardIssue") or {}).get("key")
        for lk in links
        if isinstance(lk, dict)
    }
    assert other in seen, (
        f"the local 'blocks' link did not reach DC: fields.issuelinks on {key} names {seen}, "
        f"expected to contain {other!r}"
    )


# ---------------------------------------------------------------------------
# Rows 12-13 — parent. DC splits what Cloud unifies, and the epic case is DECLINED.
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_set_parent_declines_the_epic_case_loudly(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """Row 12, the EPIC case: `set_parent` raises NotImplementedError NAMING the limitation.

    ASSERTED DIRECTLY AGAINST THE TRANSPORT, not through a reconcile pass, and that is not a
    shortcut: `dispatch_one.py:564` SWALLOWS NotImplementedError, so a pass-level assertion is
    untestable by construction — the exception never escapes to anything the test can observe.

    This is the documented DC behaviour, not a defect: epic membership on DC is an "Epic Link"
    custom field written through the Agile API under the `greenhopper` path, which
    pycontribs/jira does not target by default. Declining loudly beats writing `fields.parent`
    and letting DC silently no-op it.
    """
    key = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 parent-decline"))
    with pytest.raises(NotImplementedError) as caught:
        dc_transport.set_parent(key, key)
    message = str(caught.value)
    assert "sub-task" in message.lower(), (
        f"the decline must NAME the limitation so an operator can act on it; got: {message!r}"
    )


# ---------------------------------------------------------------------------
# Row 14 — deletion never plans a teardown of the local side (ADR 0028 §1)
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_a_deleted_dc_issue_never_plans_a_local_teardown(
    dc_store_copy_repo: Path, dc_transport: Any, bound_dc_issue: Any
) -> None:
    """Row 14 inbound: deleting the DC issue must NOT plan any teardown of the local side.

    ORACLE CORRECTED. This cell previously asserted an `(inbound, probe)` plan entry and cited
    `differ.py:630-650` as its emitter. That citation was wrong and the assertion was
    unsatisfiable: `_compute_mutations_emit_absent_partner_probes` reads
    `local_state[key]["jira_key"]`, but its only production caller passes the PREVIOUS JIRA
    SNAPSHOT as `local_state` (`run_differs.py:222` — `compute_mutations(ctx.prev_snapshot,
    ctx.curr_snapshot, ...)`), and a snapshot entry is the raw Jira `fields` dict
    (`fetcher.py:479`, contract shape `_snapshot_schema.py:96-135`) which carries no
    `jira_key`. So that loop never fires in production and NOTHING emits an `(inbound, probe)`
    for a bound pair whose local ticket is active. The pair's real owner is the outbound
    differ's bounded direct GET, which on a confirmed 404 records the absence toward grace and
    deliberately emits no mutation (`outbound_differ.py:692-702`); `binding_walk.py:167-170`
    skips active-local pairs precisely to leave them to it.

    THE AUTHORITATIVE OBSERVABLE IS THE ONE THIS DOCSTRING ALWAYS DESCRIBED — "rebar does not
    tear down a local ticket because a remote read failed once". ADR 0028 §1: snapshot-absence
    is NOT a signal of deletion, and no destructive or terminal action may be driven by it;
    deletion is proven only by a bounded GET 404 counted to grace (§2). So the assertion is
    that the plan carries NO local teardown for this pair and the local ticket survives.

    Asserted from a DRY-RUN. That is not merely tidiness here: a writing pass over a key that
    has left the snapshot also plans an `(outbound, create)` for it, which is a separate
    finding filed on its own — running this cell in a writing mode would file a duplicate
    issue into the harness.
    """
    local_id, key = bound_dc_issue

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


# ---------------------------------------------------------------------------
# The identity criterion — asserted POSITIVELY, not as "nothing raised"
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_the_inbound_assignee_mints_a_jira_family_identity(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, bound_dc_issue: Any
) -> None:
    """An inbound assignee MINTS an identity, and it is minted under the SHARED `jira` family.

    ASSERTED POSITIVELY, AND AGAINST THE REGISTRY RATHER THAN THE TICKET. Bug 5f48 was exactly
    this failing SILENTLY — `jira-datacenter` was not a valid creation channel and the mint was
    swallowed — so "the pass did not raise" is worth nothing here.

    WHAT THIS CELL USED TO DO, AND WHY IT COULD NEVER FAIL. It read `.assignee` off the local
    ticket and compared it to `rebar.ensure_identity_for("jira", "admin", ...)`. Two separate
    errors compounded:

      1. `.assignee` IS NOT THE IDENTITY. Local tickets store the assignee as a BARE STRING on
         BOTH deployments by design — `inbound_fields._assignee_matches` exists precisely
         because Jira returns an object and local holds a string — and the mint is ADDITIVE:
         `apply_inbound_records._ensure_inbound_assignee_identity` says outright that it
         "NEVER changes the human-readable name extraction". This story's own plan said the
         same: the identity is NOT a field on the ticket JSON. So the field was never going to
         carry the id.
      2. `ensure_identity_for` IS CREATE-OR-REUSE. Calling it inside the oracle MINTS the
         identity if the pass did not, so comparing its return value against itself can never
         distinguish "the pass minted it" from "the assertion just minted it". Even with (1)
         corrected, that comparison would be a tautology.

    SO THE OBSERVABLE IS A BEFORE/AFTER ON THE REGISTRY, read through the READ-ONLY resolver
    `rebar.resolve_mapping` — which returns None rather than creating. The mapping is absent
    before the pass and present after it; that difference is the pass's own work and nothing
    else's.

    The FAMILY is asserted in both directions. The epic's shared-identity decision is that DC
    and Cloud share the `jira` provider, with the DEPLOYMENT distinguished by
    `RemoteRef.instance` rather than by forking the store vocabulary. So the mapping must
    resolve under `jira` AND must NOT exist under `jira-datacenter` — a positive-only check
    would pass a build that minted under both.

    THE CELL ESTABLISHES ITS OWN PRECONDITION RATHER THAN HOPING FOR IT. Harness run 30763838558
    showed the "absent before" assertion failing: jira/'admin' was already mapped. That is not
    the scrub's doing — every identity on the real `tickets` branch carries `mappings: []`. It
    is `bound_dc_issue`'s binding pass importing the seeded issue's DEFAULT assignee (the
    project lead, i.e. the admin) and minting for it. Since the fixture will re-mint whichever
    user its issue is assigned to, and the admin is the only user the harness guarantees, the
    cell unassigns, syncs, removes that one mapping, and asserts the absence it just
    established — the assertion stays, and now discriminates a failed setup instead of an
    unwinnable one.
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
    assert "Traceback" not in cp.stderr, f"the unassign setup pass raised:\n{cp.stderr[-2000:]}"
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
    assert "Traceback" not in cp.stderr, f"inbound assignee pass raised:\n{cp.stderr[-2000:]}"

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
    """The page size the SERVER actually applies, read from its own echoed `maxResults`.

    Observed rather than assumed, which is the AC's requirement and also the only honest way:
    Jira DC silently clamps `maxResults` to `jira.search.views.default.max`, so asking for a
    huge page and reading back what the server says it gave is the measurement. Requesting a
    deliberately absurd size makes the clamp visible even on a nearly empty project — a count
    of returned issues could not, because it is bounded by how many exist.
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
    """Seed past the reconciler's page size and assert EVERY seeded issue is recovered.

    THIS GUARDS A DEFECT THAT SHIPPED TWICE. `get_parent_map` (fixed in 1105), then
    `get_issuelinks_map`/`get_comment_map` (9263), then `fetcher._iter_pages` (deac) all
    advanced by the REQUESTED page size and stopped on a SHORT page — so a server-truncated
    FIRST page read as "that is all there is". Measured at the time: 20 of 250 recovered, 92%
    of the inbound snapshot silently lost, raising nothing. A unit test with a fake client
    caught it only after it was known to look for; this cell makes the real instance say so.

    The reconciler pages `_iter_pages` at 100 (`fetcher.py:256,458`), so seeding 2*100+1 forces
    THREE pages. The count is derived from the observed server page size and REPORTED in the
    run output (`-rA` keeps a passing test's stdout) rather than left implicit.

    Runs UNFILTERED but DRY-RUN: unfiltered is required because the point is what the FETCH
    recovers, and dry-run is what makes unfiltered safe over an unbound store copy.
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

    # Wait for the INDEX to hold them all — a count check, because waiting on the last key
    # alone would not prove the earlier ones are visible to a paged search.
    deadline = time.monotonic() + 300.0
    indexed = 0
    while time.monotonic() < deadline:
        indexed = len(dc_transport._paged_search(f'project = "{jira_dc_project}"'))
        if indexed >= target:
            break
        time.sleep(5.0)
    print(f"[j11-pagination] indexed {indexed} of {target} seeded issues")
    assert indexed >= target, (
        f"only {indexed} of {target} seeded issues became searchable within 300s — the index is "
        f"lagging further than this suite allows. NOT a pagination defect."
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
    """Collect EVERY `base_url` assignment in the working repo and assert the set is the harness.

    A FILE-CONTENT check, not an environment check, and the distinction is the point: the
    environment assertions in `test_the_working_repo_is_isolated_from_this_project` prove no
    credential is present, but they cannot prove that some config file in this copy names the
    project's real Jira. A single stray `base_url` is all it would take for a writing pass to
    aim at production, and asserting the SET (rather than "the harness URL appears") is what
    catches a second value sitting alongside the right one.
    """
    import re

    candidates = [
        dc_store_copy_repo / "rebar.toml",
        dc_store_copy_repo / "pyproject.toml",
    ]
    rebar_dir = dc_store_copy_repo / ".rebar"
    if rebar_dir.is_dir():
        candidates.extend(sorted(p for p in rebar_dir.rglob("*") if p.is_file()))
    pattern = re.compile(r"""^\s*base_url\s*=\s*["']([^"']+)["']""", re.MULTILINE)
    found: dict[str, list[str]] = {}
    for path in candidates:
        if not path.is_file():
            continue
        for value in pattern.findall(path.read_text()):
            found.setdefault(value, []).append(str(path.relative_to(dc_store_copy_repo)))

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
    """After a mutation converges, a second pass must plan nothing for that pair.

    A SEPARATE CELL, and the separation is the lesson rather than a style choice. This assertion
    was originally bundled into every round-trip cell above, and run 30721408463 then reported 19
    failures of which THIRTEEN were mutations that had round-tripped perfectly and tripped only on
    this check — the real signal buried under false reds. An assertion that can fail for a reason
    unrelated to the cell's subject belongs in its own cell. (This is the same "split it into two
    cells" move that localised an earlier four-attempt bug on the first run.)

    WAITS FOR THE INDEX BEFORE RE-PLANNING, which the bundled version did not. The differ reads
    the remote snapshot through a JQL search and Jira's index is eventually consistent, so a
    dry-run issued immediately after a write sees the OLD document and re-plans the update it just
    applied. Without this wait the check cannot distinguish index lag from genuine churn, and a
    failure would be unattributable — the exact trap `_wait_until_search_reflects` exists for.
    """
    import rebar

    local_id, key = bound_dc_issue
    new_title = _uniq("rebar J11 idempotence")

    rebar.edit_ticket(local_id, repo_root=dc_store_copy_repo, title=new_title)
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert "Traceback" not in cp.stderr, f"the converging pass raised:\n{cp.stderr[-2000:]}"

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
