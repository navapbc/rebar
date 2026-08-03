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
from _dc_support import assert_local_assignee_is as _assert_local_assignee_is
from _dc_support import assert_mint_registered as _assert_mint_registered
from _dc_support import assert_outbound_provenance_markers as _assert_outbound_provenance_markers
from _dc_support import assert_remote_parent_is as _assert_remote_parent_is
from _dc_support import envelope as _envelope
from _dc_support import forget_identity_mapping as _forget_identity_mapping
from _dc_support import raw_indexed_issue_count as _raw_indexed_issue_count
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
    """Block until THE PRODUCTION LINK READ for ``key`` satisfies ``predicate``.

    Waits on ``get_issuelinks_map`` rather than on ``get_issue_links``, and the difference
    matters for the same reason `_wait_until_search_reflects` exists. ``get_issue_links`` is a
    direct GET (`transport.py:477-486`) and is immediately consistent; the INBOUND PASS reads
    links from a JQL paged search — ``fetcher.py:592-593`` calls ``get_issuelinks_map``, which
    is ``_paged_search(f"project = {project}")`` (`transport.py:391-395`) — and Jira's Lucene
    index is not. A cell that waits on the direct GET can therefore hand the differ a stale
    document and then report a bridge defect that never happened.
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
    """Scoped dry-run plan entries naming this pair.

    MATCHES ON `target`, NOT on `local_id`. The envelope's `local_id` field is populated from
    `provenance["local_id"]` (`reconcile_helpers._build_plan_entries`) and for these entries it
    carries the JIRA KEY, not the rebar local id — observed directly in J11's first harness
    run (ticket 5200-e04e-246e-4aae), whose
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
    """Rows 2-7 and 9 inbound: mutate in DC, run a pass, assert the LOCAL ticket carries it.

    `bound_dc_issue` supplies an issue that is already imported and BOUND, so this cell
    exercises the UPDATE path on an existing ticket rather than re-testing the create path
    (row 1, which the thin slice already covers end to end).

    ROW 8 IS NOT IN THIS TABLE — it is `test_inbound_assign_round_trips` below, because it
    needs two passes and this driver runs one.
    """
    local_id, key = bound_dc_issue
    dc_transport.project = jira_dc_project

    expected = mutate(dc_transport, jira_dc_project, key)

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert "Traceback" not in cp.stderr, f"inbound pass raised:\n{cp.stderr[-2000:]}"

    oracle(_local(dc_store_copy_repo, local_id), expected)


@_skip
@_skip_no_extra
def test_inbound_assign_round_trips(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, bound_dc_issue: Any
) -> None:
    """Row 8 inbound: assigning a DC user puts THAT user on the local ticket.

    THIS CELL USED TO BE A `_INBOUND_CELLS` ROW AND COULD NOT FAIL IN EITHER HALF. It is the
    epic's signature failure mode, twice over in eight lines:

      * THE MUTATION WAS A NO-OP. `bound_dc_issue`'s seeded issue arrives ALREADY ASSIGNED to
        the project lead — `conftest._create_scratch_project` passes `lead=admin` with no
        `assigneeType`, so DC default-assigns to it, and this suite asserts that fact in two
        other places. Assigning `ADMIN_USER` therefore changed nothing:
        `inbound_fields._assignee_matches` (`inbound_fields.py:102-128`) short-circuits an
        unchanged assignee, so the differ had nothing to report. The J11 harness
        confirmed it independently by finding `jira/'admin'` already mapped before any cell ran.
      * THE ORACLE CHECKED A FIELD THE BINDING PASS HAD ALREADY POPULATED, and checked it only
        for TRUTHINESS. So the cell was green whether or not inbound assignee sync worked at
        all. The AC's row-8 inbound oracle is "`.assignee` is the mapped identity"; a
        truthiness check is not that.

    THE REPAIR IS THE ONE THE MINT CELL ALREADY ESTABLISHED: make the expected value one that
    is genuinely NOT there beforehand. That cell removes an identity MAPPING to establish
    absence; this one drives the ASSIGNEE to empty and asserts the emptiness reached the local
    ticket, so the value read at the end is one only the pass under test can have written.

    STANDALONE, NOT A ROW, for the reason rows 6 and 9 outbound are standalone: the table
    driver runs exactly ONE pass and this needs TWO (clear, converge, assign, converge). The
    admin is the ONE user the harness guarantees exists and is assignable
    (`_dc_support.py:28-31`), so "assign a DIFFERENT user than the pre-seeded one" is not
    available on this instance — going through empty is.

    THE ORACLE IS EXACT EQUALITY against the DC username, via
    `_dc_support.assert_local_assignee_is`, which is where the reasoning for that expected value
    lives (`.assignee` holds `_extract_name(fields["assignee"])`, and `_extract_name` prefers
    `name` over `displayName`, which on DC is the username). It is shared rather than inline so
    the harness-free mutation check can drive THE ORACLE ITSELF — the harness is amd64-only, so
    a green live run is the one thing that cannot demonstrate this cell discriminates.
    """
    local_id, key = bound_dc_issue
    dc_transport.project = jira_dc_project

    # SETUP — take the assignee away, and drive that all the way into the local ticket. Both
    # halves are asserted as SETUP: an unassign that does not propagate leaves the pre-seeded
    # value in place, which is exactly the state that made this cell vacuous. Cell `09-unassign`
    # covers this propagation on its own; if that cell is also red, fix it there.
    dc_transport.update_issue(key, assignee=None)
    _wait_until_dc_assignee_is(dc_transport, jira_dc_project, key, None, "the unassignment (setup)")
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert "Traceback" not in cp.stderr, f"the unassign setup pass raised:\n{cp.stderr[-2000:]}"
    _assert_local_assignee_is(
        _local(dc_store_copy_repo, local_id), "", stage="SETUP (not the assignment)"
    )

    # THE MUTATION UNDER TEST — now a REAL transition, empty -> admin.
    dc_transport.update_issue(key, assignee=ADMIN_USER)
    _wait_until_dc_assignee_is(dc_transport, jira_dc_project, key, ADMIN_USER, "the assignee")

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert "Traceback" not in cp.stderr, f"inbound assign pass raised:\n{cp.stderr[-2000:]}"

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
def test_outbound_create_stamps_both_provenance_markers(
    dc_store_copy_repo: Path, jira_dc_project: str, track_issue: Any, dc_request: Any
) -> None:
    """Row 1 OUTBOUND: a local ticket the pass CREATES in DC carries BOTH provenance markers.

    THIS ROW HAD NO TEST AT ALL, in either this module or the thin slice, and it was not on the
    story's own list of known gaps — `grep -rn "rebar-id:\\|properties/local_id" tests/external/`
    returned nothing. Every other outbound cell rides `bound_dc_issue`, which exists precisely
    so those cells exercise the UPDATE path; nothing exercised the CREATE path's write-back.

    THE TWO MARKERS ARE ASSERTED TOGETHER because neither is redundant and the create writes
    them as a pair (`dispatch_one.py:306-307`). The LABEL is what the dedup JQL re-finds the
    issue by (`dispatch_one.py:214` searches `labels = "rebar-id:<local_id>"`); lose it and the
    next pass creates a DUPLICATE. The ENTITY PROPERTY is what inbound consumers correlate on.
    A cell asserting one would pass a build that lost the other.

    THE COLON FORM IS THE ONE ASSERTED, and that is established from the writers, not chosen.
    This codebase carries both `rebar-id:<local_id>` and `rebar-id-<local_id>` (see
    `inbound_differ`'s exclusion list re bug `eadb`), and all three writers emit the COLON form:
    `dispatch_one.py:306`, `apply_inbound_records.py:290`, `binding_store.py:706`. The hyphen
    form is READ-ONLY legacy — `binding_walk.py:352` and `inbound_translate.py:77-78` accept it
    and `binding_store.py:715` searches it as a fallback, but nothing writes it. So a
    hyphen-only issue is a finding, and the oracle says so rather than accepting it.

    THE PROPERTY IS READ BY RAW REST, not through `transport.get_entity_property`. Reading a
    value back through the same abstraction that wrote it cannot distinguish "stored correctly"
    from "stored and re-read consistently wrong" — which is bug 0b27 exactly: a Cloud
    implementation wrapped the value as `{"value": …}`, storing the wrong shape and breaking
    correlation WITHOUT raising. The labels are read from that same raw document for the same
    reason, so nothing in this oracle passes through the writing path.
    """
    from rebar_reconciler.binding_store import load_binding_store

    import rebar

    title = _uniq("rebar J11 outbound create")
    local_id = rebar.create_ticket("task", title, repo_root=dc_store_copy_repo)

    # Scoped to the LOCAL ID alone — deliberately, and it is the one case where that is right:
    # an outbound CREATE has no Jira key yet, which is why `bound_dc_issue` has to pass both.
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=local_id)
    assert "Traceback" not in cp.stderr, f"outbound create pass raised:\n{cp.stderr[-2000:]}"

    key = load_binding_store(dc_store_copy_repo).get_jira_key(local_id)
    assert key, (
        f"the outbound pass did not create-and-bind {local_id!r} (get_jira_key returned "
        f"{key!r}), so there is no DC issue to read the provenance markers off. Row 1's markers "
        f"are written INSIDE the create (`dispatch_one.py:306-307`), so a missing binding is "
        f"upstream of this oracle, not a marker finding.\nstdout:\n{cp.stdout[-1500:]}"
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


@_skip
@_skip_no_extra
def test_outbound_unassign_round_trips(
    dc_store_copy_repo: Path, dc_transport: Any, bound_dc_issue: Any
) -> None:
    """Row 9 outbound: clearing the local assignee must leave `fields.assignee` NULL in DC.

    STANDALONE, NOT A `_OUTBOUND_CELLS` ROW, for the reason row 6 (remove-label) is standalone:
    the table driver runs exactly ONE pass, and a clear needs a CONVERGED ASSIGN before it or
    the absence afterwards proves nothing — the issue starts out assigned to the project lead in
    some runs and unassigned in others, so an unconditional "is it null?" could pass without any
    mutation happening at all. Two passes, so it cannot be a row.

    THE ORACLE IS THE REMOTE FIELD BEING EMPTY, not that the payload carried a clear
    instruction and not that the pass exited 0. That distinction is the entire point here: every
    layer on this path degrades quietly — the resolver treats an unmappable assignee as "desired
    = unassigned" (`outbound_differ.py:479-505`) and the transport's assign call is wrapped in
    error translation — so the only thing that discriminates "unassigned" from "silently left
    alone" is reading `fields.assignee` back off the instance.

    EXPECTED RED, AND THE MECHANISM IS PROVEN BY CODE PATH, not guessed. An empty local assignee
    is resolved to the EMPTY STRING, not to None: `_assignee_resolver` returns `("", True,
    False)` when `not assignee` (`outbound_differ.py:504-505`), and `assignee` is in
    `_OUTBOUND_BATCH_ALLOWLIST` (`dispatch_apply_phases.py:46`), so `update_issue(key,
    assignee="")` is what the transport receives. DC's `update_issue` pops `assignee` and calls
    `_assign(remote_id, "")` with no empty-value branch (`transport.py:281-303`), and
    pycontribs/jira treats ONLY None / -1 / "-1" as Unassigned — `JIRA._get_user_id` (jira
    3.10.5) otherwise runs a user search and raises `JIRAError("No matching user found for:
    '')`. Cloud has the fix and DC never got its half: `adapters/jira/acli.py:342-345,357-359`
    routes an empty/None assignee through `unassign_issue` precisely because passing it on
    "silently no-ops" (bug 85a1). That is the same Cloud-has-it/DC-doesn't shape as bug d067,
    which this transport's own docstring records at `transport.py:265-275`. Filed as
    [rebar:751e-06f1-bb0b-464c]; this cell asserts the AC's oracle (row 9 outbound:
    "`fields.assignee` is null") rather than pinning the current behaviour.
    """
    import rebar

    local_id, key = bound_dc_issue

    # SETUP — get the issue ASSIGNED through the bridge, and prove it landed. Reuses row 8's
    # mutate + oracle so the two cannot drift on how an assignment is expressed.
    expected = _out_assign(dc_store_copy_repo, local_id)
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert "Traceback" not in cp.stderr, f"outbound assign pass raised:\n{cp.stderr[-2000:]}"
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
    assert "Traceback" not in cp.stderr, f"outbound unassign pass raised:\n{cp.stderr[-2000:]}"

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
# Rows 10-11 — links, in both directions. ADD and REMOVE are SEPARATE cells.
# ---------------------------------------------------------------------------
#
# WHY THE SPLIT, since both directions previously carried one cell apiece. The two link
# cells below each claimed "Rows 10-11" in their docstring while asserting ONLY the add — a
# docstring overclaiming its own coverage, which is the exact defect class sibling ticket 2944
# existed to delete, and it is worse than a missing test: it makes the gap invisible to anyone
# auditing the table. The removals now live in their own cells (`test_inbound_delete_link_...`
# and `test_outbound_delete_link_...`), each asserting the link's ABSENCE after a removal that
# a PROVEN add preceded, and these two are re-scoped to row 10 alone. Splitting rather than
# appending is also the discipline this module already learned the expensive way (see
# `test_a_repeat_pass_over_a_converged_pair_plans_nothing`): a removal can fail for reasons the
# add cannot, so bundling them makes one red report two indistinguishable things.


@_skip
@_skip_no_extra
def test_inbound_link_round_trips(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    bound_dc_issue: Any,
) -> None:
    """Row 10 inbound: a Jira issue link surfaces as a local dep.

    SCOPED TO THE ADD, and the docstring says so. It previously read "Rows 10-11 ... and its
    removal removes it" while the body asserted only the add; the removal is now
    `test_inbound_delete_link_round_trips` below. No assertion was weakened — one was ADDED,
    elsewhere, and this claim narrowed to what this body actually proves.

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
    """Row 11 inbound: a Jira issue link DELETED in DC must disappear from the local ticket.

    THE ORACLE IS ABSENCE, ASSERTED AFTER A PROVEN ADD. The add half is SETUP, not the
    assertion, and it is asserted as setup: a removal cell whose link never arrived passes
    vacuously against a bridge that never wrote it. Same shape as
    `test_outbound_remove_label_round_trips` (row 6), for the same reason.

    "The pass did not raise" is deliberately NOT the oracle. The whole d067 defect was a
    soft-failed error with exit 0, so the only thing worth reading here is whether the dep is
    GONE from `rebar show`.

    EXPECTED RED — AND THE PRODUCT, NOT THIS CELL, IS WHY. `inbound_differ._diff_links_inbound`
    is ADD-ONLY by construction: its docstring opens "Reflect Jira issuelinks into rebar
    relations. ADD-only." and closes "ADD-only (no REMOVE mutations)"
    (`inbound_differ.py:380`, `:396`). Nothing walks the local deps looking for one whose Jira
    counterpart has gone, so a link a human deletes in Jira stays on the rebar ticket forever
    and no pass reports it. The OUTBOUND direction does have a removal path
    (`outbound_links._diff_link_removals`, `outbound_links.py:120-175`), which is what makes
    this an asymmetry rather than a deliberate whole-feature omission. Filed as
    [rebar:2b16-9be0-a8f5-41d9] with the citations; this cell is the evidence, so it asserts the
    AC's oracle (row 11 inbound: "that link is ABSENT") rather than pinning the current
    behaviour. Do not "fix" it by asserting the dep survives — that would freeze the gap.
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
    assert "Traceback" not in cp.stderr, f"priming pass for the link target raised:\n{cp.stderr}"
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
    assert "Traceback" not in cp.stderr, f"inbound link-add pass raised:\n{cp.stderr[-2000:]}"
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
    assert "Traceback" not in cp.stderr, f"inbound unlink pass raised:\n{cp.stderr[-2000:]}"

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
    """Row 10 outbound: a local `blocks` link surfaces in `fields.issuelinks`.

    SCOPED TO THE ADD, and the docstring now says so. It previously claimed "Rows 10-11 ... and
    unlink removes it. Both halves in one cell", and the body asserted ONLY the add (there was
    no unlink here at all) — a docstring overclaiming its coverage, which hides a gap more
    effectively than having no test. The removal is `test_outbound_delete_link_round_trips`
    below, which does exactly what the old text described: proves the add landed, then asserts
    absence. No assertion was weakened; one was added, elsewhere."""
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
    """Row 11 outbound: a local `unlink` must remove the link from `fields.issuelinks`.

    THE ORACLE IS ABSENCE ON THE INSTANCE, read with `get_issue_links` (a direct GET,
    `transport.py:477-486`) so it cannot be satisfied by what rebar believes it sent. Not "the
    pass exited 0" and not "the payload carried a remove instruction": the removal path
    (`outbound_links._diff_link_removals` → `dispatch_one`'s `delete_issue_link`) is exactly the
    kind of best-effort chain that logs and continues, so only the post-state is evidence.

    ITS OWN CELL, separate from row 10's add. The add is asserted here too, but as SETUP — an
    unlink asserted without first proving the link ARRIVED passes vacuously against a bridge
    that never wrote it, which is the same trap `test_outbound_remove_label_round_trips`
    documents for row 6.
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
    assert "Traceback" not in cp.stderr, f"binding pass for the link target raised:\n{cp.stderr}"
    bound_other = load_binding_store(dc_store_copy_repo).get_jira_key(other_local)
    assert bound_other == other, (
        f"SETUP FAILED (not the removal): the link target {other_local} is not bound (got "
        f"{bound_other!r}); an outbound link naming an unresolvable target is skipped, not "
        f"attempted."
    )

    # SETUP — push the link and PROVE it landed on the instance.
    rebar.link(local_id, other_local, "blocks", repo_root=dc_store_copy_repo)
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert "Traceback" not in cp.stderr, f"outbound link-add pass raised:\n{cp.stderr[-2000:]}"
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
    assert "Traceback" not in cp.stderr, f"outbound unlink pass raised:\n{cp.stderr[-2000:]}"

    after = _linked_keys(dc_transport.get_issue_links(key))
    assert other not in after, (
        f"the local link was REMOVED but the DC issue still carries it: fields.issuelinks on "
        f"{key} names {sorted(str(k) for k in after)}, expected {other!r} to be absent. The "
        f"outbound remove path is `outbound_links._diff_link_removals` "
        f"(`outbound_links.py:120-175`) applied via `delete_issue_link` "
        f"(`transport.py:556-577`); both log rather than raise, so exit 0 says nothing here."
    )


# ---------------------------------------------------------------------------
# Rows 12-13 — parent. DC splits what Cloud unifies, and the epic case is DECLINED.
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_outbound_epic_parent_round_trips_via_the_epic_link(
    dc_transport: Any, jira_dc_project: str, track_issue: Any, dc_request: Any
) -> None:
    """Row 12, the EPIC case: `set_parent` WRITES the Epic Link, and it round-trips.

    REWRITTEN, and the reason is recorded rather than quietly applied. This cell used to assert
    `pytest.raises(NotImplementedError)` — the loud decline that was correct while ticket 39c1's
    fix did not exist. Two harness runs changed what is correct here:

      * run 30834117797 confirmed the decline meant NO parent could ever reach DC, because the
        outbound emit gate only emits an EPIC parent (bug 8b25) and that was precisely the shape
        this side refused;
      * run 30840572608 refuted the FIRST attempt at the fix (change 1302, `add_issues_to_epic`
        under `agile_rest_path="greenhopper"`) — DC 8.17.1 answers
        POST /rest/greenhopper/1.0/epic/{key}/issue with HTTP 404 "null for uri".

    The cell's INTENT is unchanged: prove what `set_parent` does with an epic parent, and refuse to
    let `fields.parent` be written where DC would silently no-op it. What changed is the expected
    outcome, from "declines loudly" to "writes the Epic Link" — so this is not an inverted
    assertion hiding a failure, it is the oracle for the behaviour the ticket now ships.

    ASSERTED AGAINST A RAW REST READ-BACK, not the transport's return value, for the reason every
    row here follows: the write path and the proving read must not share code, or a broken writer
    that returns cleanly still passes.
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
    """Create an EPIC in `project` and return its key.

    Two things are discovered rather than assumed, and both fail as SETUP rather than as a
    bridge defect: the project must actually offer an "Epic" issue type (the scratch project is
    built from whichever template the image ships), and DC requires the "Epic Name" field on
    creation — omitting it is rejected with a validation error that would otherwise read as a
    transport bug.
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
    """The name of THIS project's sub-task issue type, ASKED of the instance.

    Not hardcoded as "Sub-task", for the reason `conftest._discover_project_templates` records
    about template keys: the scratch project is created from whichever template the image
    happens to offer, so its issue-type set is not knowable at authoring time. A hardcoded name
    that the project does not have would make `create_issue` fail and every parent cell below
    would report a PROJECT-CONFIGURATION problem as a bridge defect.
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
    """Block until ``get_parent_map`` reflects ``what`` for ``key``.

    Waits on THE PRODUCTION READ. The inbound pass does not read `fields.parent` off the issue:
    the fetcher makes one extra paged REST search via ``client.get_parent_map`` and merges the
    result into each snapshot entry (`fetcher.py:485-511`), and that search is index-backed and
    eventually consistent. So this is the `_wait_until_search_reflects` hazard again, one layer
    over: waiting on a direct GET would let a stale parent map be reported as a bridge defect.

    THE PREDICATE TAKES THE WHOLE MAP, NOT ``mapping[key]``, and that is a correctness
    requirement rather than a convenience. ``get_parent_map`` has a DEGRADATION CONTRACT: any
    REST failure logs a warning and returns ``{}`` (`transport.py:520-527`). A predicate handed
    only the looked-up value cannot distinguish "the instance says this issue has no parent"
    (key PRESENT, value None) from "the parent map failed and returned nothing" (key ABSENT) —
    so a clear-parent wait written that way is satisfied by a broken read, which is precisely
    the vacuous-oracle failure this suite keeps finding. Callers asserting an ABSENT parent must
    therefore require the key to be present, e.g. ``lambda m: key in m and not m[key]``.
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
def test_inbound_set_subtask_parent_round_trips(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    track_issue: Any,
    dc_request: Any,
) -> None:
    """Row 12 inbound, the SUB-TASK case: a DC re-parent must reach `.parent_id` locally.

    STANDALONE, not an `_INBOUND_CELLS` row, because the table driver hands every row the
    `bound_dc_issue` fixture — a TASK. On Data Center only a sub-task has a `fields.parent` at
    all (`transport.py:645-690`), so this row needs its own hierarchy: two candidate parents and
    a sub-task, all bound before the pass.

    RE-PARENTS RATHER THAN PARENTS, so the assertion is about a value THIS CELL wrote. A
    sub-task cannot be created parentless, so it arrives already pointing at one parent and the
    priming import may already have carried that; asserting THAT value would be re-reading a
    field the cell did not set. Moving it to a second parent makes the oracle a genuine
    round-trip, and the precondition below asserts the two differ so it cannot be vacuous.

    BOTH PARENTS MUST BE BOUND. `inbound_differ._extract_parent_local_id` resolves the Jira
    parent key through `binding_store.get_local_id` and returns None when the key is not yet
    bound, and the caller then SKIPS the field rather than emitting it (`inbound_differ.py:90-110`,
    `:257-270`). An unbound parent therefore produces no mutation at all — the same
    unresolvable-counterpart trap the link cells document.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    subtask_type = _subtask_type_name(dc_request, jira_dc_project)
    first = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 parent A"))
    second = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 parent B"))
    child = _seed(
        dc_transport,
        jira_dc_project,
        track_issue,
        _uniq("rebar J11 reparented subtask"),
        issuetype=subtask_type,
        extra={"parent": {"key": first}},
    )
    child_local = _jira_key_to_local_id(child)
    first_local = _jira_key_to_local_id(first)
    second_local = _jira_key_to_local_id(second)

    scope = ",".join((child_local, child, first_local, first, second_local, second))
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert "Traceback" not in cp.stderr, f"priming pass for the hierarchy raised:\n{cp.stderr}"
    store = load_binding_store(dc_store_copy_repo)
    for local_ref, key_ref in ((child_local, child), (second_local, second)):
        bound = store.get_jira_key(local_ref)
        assert bound == key_ref, (
            f"SETUP FAILED (not the reparent): {local_ref} is not bound (got {bound!r}, expected "
            f"{key_ref!r}). An unbound parent key resolves to None and the differ SKIPS the "
            f"parent field entirely (`inbound_differ.py:257-270`), so no mutation would even be "
            f"attempted."
        )
    before = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert before != second_local, (
        f"SETUP FAILED (not the reparent): the sub-task's local parent_id is ALREADY "
        f"{second_local!r} before this cell reparents it, so the assertion below would pass "
        f"without any mutation having happened."
    )

    dc_transport.set_parent(child, second)
    _wait_until_parent_map_reflects(
        dc_transport,
        jira_dc_project,
        child,
        lambda mapping: mapping.get(child) == second,
        f"the reparent to {second}",
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert "Traceback" not in cp.stderr, f"inbound reparent pass raised:\n{cp.stderr[-2000:]}"

    after = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert after == second_local, (
        f"the DC reparent did not reach the local ticket: .parent_id on {child_local} is "
        f"{after!r} (it was {before!r} before), expected {second_local!r} — the local id of "
        f"{second}, which `get_parent_map` confirms is now the sub-task's parent."
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
    """Row 13 inbound: a parent REMOVED in DC must clear `.parent_id` locally.

    THE ORACLE IS ABSENCE, and it is preceded by a precondition that the parent IS set locally
    — otherwise "parent_id is empty" is true before the cell does anything.

    THE DC-SIDE CLEAR IS ITS OWN ASSERTED SETUP STEP, deliberately. Data Center may refuse to
    null a sub-task's parent (a sub-task exists only under one), and if it does, that refusal is
    a PLATFORM constraint and not a rebar defect — so it is caught and reported as SETUP FAILED
    rather than being allowed to look like "the bridge did not clear the local parent". The two
    outcomes are genuinely different findings and this cell keeps them apart.

    EXPECTED RED ON THE ORACLE, with the mechanism proven by code path and filed as
    [rebar:88d9-fe42-e50f-4067]. TWO layers independently drop the signal:
      1. THE SNAPSHOT NEVER CARRIES "no parent". The fetcher merges `get_parent_map` into the
         snapshot and, when the mapped parent is None, deliberately leaves the field ABSENT —
         "When parent_jira_key is None, leave the field absent (top-level issue)"
         (`fetcher.py:508-511`). A de-parented issue then looks identical to one that never had
         a parent.
      2. THE DIFFER REFUSES TO EMIT A CLEAR: "We do NOT emit parent_id=None to avoid
         accidentally clearing a locally-set parent when we just can't resolve it yet"
         (`inbound_differ.py:266-270`).
    The apply layer is already ready for it — `apply_inbound_records` maps `parent_id` through
    `lambda v: v or ""` and its comment says an absent/empty parent "clears the parent"
    (`apply_inbound_records.py:361-372`) — so only the differ never sends it. The cell asserts
    the AC's oracle (row 13 inbound: "`.parent_id` is null"); do not invert it to pin the gap.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    subtask_type = _subtask_type_name(dc_request, jira_dc_project)
    parent = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 detach parent"))
    child = _seed(
        dc_transport,
        jira_dc_project,
        track_issue,
        _uniq("rebar J11 detached subtask"),
        issuetype=subtask_type,
        extra={"parent": {"key": parent}},
    )
    child_local = _jira_key_to_local_id(child)
    parent_local = _jira_key_to_local_id(parent)

    scope = ",".join((child_local, child, parent_local, parent))
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert "Traceback" not in cp.stderr, f"priming pass for the hierarchy raised:\n{cp.stderr}"
    bound = load_binding_store(dc_store_copy_repo).get_jira_key(child_local)
    assert bound == child, (
        f"SETUP FAILED (not the clear): the sub-task {child_local} is not bound (got {bound!r})"
    )
    before = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert before == parent_local, (
        f"SETUP FAILED (not the clear): the sub-task's local parent_id is {before!r}, expected "
        f"{parent_local!r}, so there is no parent for this cell to observe being cleared and the "
        f"oracle below would pass vacuously. Row 12 "
        f"(`test_inbound_set_subtask_parent_round_trips`) covers inbound parent import on its "
        f"own; if that cell is also red, fix it there."
    )

    # SETUP — clear the parent ON THE INSTANCE, and prove it. A refusal here is Data Center's,
    # not rebar's, and is reported as such.
    try:
        dc_transport.set_parent(child, None)
    except Exception as exc:  # noqa: BLE001 — classify: a DC refusal is a SETUP outcome, not a bridge defect
        raise AssertionError(
            f"SETUP FAILED (not a rebar defect): Data Center REFUSED to clear the parent of "
            f"sub-task {child} — {type(exc).__name__}: {exc}. A sub-task exists only under a "
            f"parent, so nulling `fields.parent` may not be a legal DC edit at all. That is a "
            f"platform constraint on row 13's inbound half and belongs in the AC, not in the "
            f"bridge. Row 13's OUTBOUND half "
            f"(`test_outbound_clear_parent_round_trips`) drives the same primitive and would "
            f"fail here too."
        ) from exc
    remote_parent = (dc_transport.get_issue_by_rest(child).get("fields") or {}).get("parent")
    assert not remote_parent, (
        f"SETUP FAILED (not the bridge): set_parent(..., None) returned without error but "
        f"{child} still carries fields.parent = {remote_parent!r}, so there is no remote clear "
        f"for the pass to mirror. `set_parent` PUTs {{'parent': None}} "
        f"(`transport.py:688-689`); Data Center accepted the request and ignored it."
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
        "the parent removal",
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert "Traceback" not in cp.stderr, f"inbound clear-parent pass raised:\n{cp.stderr[-2000:]}"

    after = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert not after, (
        f"the DC parent was REMOVED (confirmed absent from both the direct GET and the "
        f"search-backed parent map) but .parent_id on {child_local} is still {after!r}. The "
        f"inbound differ suppresses parent_id=None by design (`inbound_differ.py:266-270`) and "
        f"the fetcher never records the absence (`fetcher.py:508-511`) — see "
        f"[rebar:88d9-fe42-e50f-4067]."
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
    """Row 13 outbound: detaching the local parent must leave `fields.parent` ABSENT in DC.

    THROUGH A PASS, not against the transport, and that is the opposite choice from row 12's
    epic case for a reason worth stating: row 12's epic case asserts an EXCEPTION, and
    `dispatch_one._update_one_apply_parent` catches `Exception` and only warns
    (`dispatch_one.py:571-578`), so an exception assertion is untestable through a pass. This
    row asserts a POST-STATE on the instance, which the swallow cannot hide.

    THE CLEAR IS EXPRESSED AS `--parent=null`. An empty value is rejected outright ("--parent
    requires a non-empty value (use --parent=null to detach)"), and the differ needs the
    "parent" key PRESENT-WITH-A-FALSY-VALUE to distinguish a CLEAR from "no parent op this
    mutation" — `_update_one_apply_parent` keys out that presence explicitly and routes the
    clear through the same `set_parent` call as a set (`dispatch_one.py:539-550`).

    IF THIS FAILS, THE THREE CANDIDATE MECHANISMS ARE, IN ORDER: the differ never emitted the
    parent clear; `set_parent` raised and was swallowed at `dispatch_one.py:571-578`; or Data
    Center accepted `{"parent": None}` on a sub-task and ignored it. The precondition below
    rules out a fourth (nothing to clear) by asserting the parent is present on BOTH sides
    first.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    import rebar

    subtask_type = _subtask_type_name(dc_request, jira_dc_project)
    parent = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 out-detach par"))
    child = _seed(
        dc_transport,
        jira_dc_project,
        track_issue,
        _uniq("rebar J11 out-detached subtask"),
        issuetype=subtask_type,
        extra={"parent": {"key": parent}},
    )
    child_local = _jira_key_to_local_id(child)
    parent_local = _jira_key_to_local_id(parent)

    scope = ",".join((child_local, child, parent_local, parent))
    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert "Traceback" not in cp.stderr, f"priming pass for the hierarchy raised:\n{cp.stderr}"
    bound = load_binding_store(dc_store_copy_repo).get_jira_key(child_local)
    assert bound == child, (
        f"SETUP FAILED (not the clear): the sub-task {child_local} is not bound (got {bound!r}), "
        f"so an outbound update would take the CREATE path instead of touching {child}."
    )
    remote_parent = (dc_transport.get_issue_by_rest(child).get("fields") or {}).get("parent") or {}
    assert remote_parent.get("key") == parent, (
        f"SETUP FAILED (not the clear): {child} does not carry fields.parent = {parent!r} (got "
        f"{remote_parent!r}), so its absence after the pass would prove nothing."
    )
    before = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert before, (
        f"SETUP FAILED (not the clear): the local ticket {child_local} has no parent_id to "
        f"detach, so the differ has no CHANGE to emit — a pass would plan nothing and the "
        f"remote parent would survive for a reason unrelated to this row."
    )

    # THE MUTATION UNDER TEST — detach locally, then read the instance back.
    rebar.edit_ticket(child_local, repo_root=dc_store_copy_repo, parent="null")
    cleared_local = _local(dc_store_copy_repo, child_local).get("parent_id") or ""
    assert not cleared_local, (
        f"SETUP FAILED (not the clear): .parent_id on {child_local} is still {cleared_local!r} "
        f"after `--parent=null`, so the outbound pass has no detach to carry."
    )

    cp = _run(dc_store_copy_repo, _WRITING_MODE, only=scope)
    assert "Traceback" not in cp.stderr, f"outbound clear-parent pass raised:\n{cp.stderr[-2000:]}"

    after = (dc_transport.get_issue_by_rest(child).get("fields") or {}).get("parent")
    assert not after, (
        f"the local parent was DETACHED but {child} still carries fields.parent = {after!r}. "
        f"Candidates, in order: the differ never emitted the clear; `set_parent` raised and was "
        f"swallowed (`dispatch_one.py:571-578` warns and continues); or Data Center accepted "
        f"PUT {{'parent': None}} and ignored it (`transport.py:688-689`)."
    )


@_skip
@_skip_no_extra
def test_outbound_set_subtask_parent_round_trips(
    dc_transport: Any, jira_dc_project: str, track_issue: Any, dc_request: Any
) -> None:
    """Row 12 OUTBOUND, the SUB-TASK case: rebar WRITING a parent onto a DC issue.

    THIS ROW HAD NO TEST IN THIS DIRECTION, and the gap was invisible because a neighbour
    looked like it. `test_outbound_clear_parent_round_trips` (row 13 outbound) does assert a
    parent is present — but that parent came from ISSUE CREATION
    (`extra={"parent": {"key": parent}}`), not from a rebar write, so nothing anywhere proved
    rebar can SET a parent on Data Center. This is the untested corner of the exact area every
    "Cloud has the translation, DC never got its half" defect in this epic has landed in —
    d067 (status), 8d68 (inbound identity mint), 751e (unassign), 2b16 / 88d9 (link removal,
    parent clear) — and every one of them was a SILENT success.

    ASSERTED AGAINST THE TRANSPORT, NOT THROUGH A PASS, and unlike row 12's epic case this is
    not because of the swallow. It is because the pass-level round-trip is STRUCTURALLY
    UNREACHABLE on Data Center: two independent gates, each correct on its own terms, do not
    compose.

      * TO BE APPLIED, the DC transport requires the CHILD to be a SUB-TASK. `set_parent`
        writes `fields.parent` only for a sub-task and raises `NotImplementedError` for
        anything else, because epic membership on DC is an "Epic Link" custom field written
        through the Agile API under the `greenhopper` path
        (`adapters/jira_datacenter/transport.py:668-712`, the decline at `:702-710`).
      * TO BE EMITTED, the outbound differ requires the LOCAL PARENT to be an EPIC.
        `outbound_field_diff._resolve_local_parent:136-139` returns `(False, None)` — the
        field is omitted entirely — for any parent whose local `ticket_type` is not `epic`
        (bug 8b25's hierarchy guard; unit-covered by
        `tests/unit/rebar_reconciler/conflict/test_parent_hierarchy_guard.py`).

    A DC sub-task's parent is a STANDARD issue, whose local ticket_type is `task` — so the one
    child DC will accept a `fields.parent` write for is exactly the one whose parent the differ
    suppresses. Routing this row through a pass would therefore assert a mutation nothing
    emits. That is the same reason row 14 outbound
    (`test_outbound_delete_leaves_the_issue_absent_by_key_AND_by_id`) asserts the PRIMITIVE,
    and it is the shape reused here. THIS IS STILL A GENUINE ROUND-TRIP — rebar writes, the
    instance is read back independently — it is just scoped to the layer that can actually run.

    THE ORACLE READS RAW REST, deliberately not `get_issue_by_rest` (the same transport that
    wrote, which cannot separate "DC stored it" from "the object we mutated reports what we
    set") and not `get_parent_map` (a JQL paged search — eventually consistent, and the read
    the INBOUND row already owns). The reasoning lives with the oracle in
    `_dc_support.assert_remote_parent_is`, which is where the harness-free mutation check
    drives it.

    RE-PARENTS RATHER THAN PARENTS, for the reason the inbound row-12 cell gives: a sub-task
    cannot be created parentless, so it arrives already pointing at one parent and asserting
    THAT value would re-read a field this cell did not write. The precondition asserts the
    starting parent, so the ending value is attributable to the mutation and a silent no-op is
    named as one.
    """
    subtask_type = _subtask_type_name(dc_request, jira_dc_project)
    first = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 outset par A"))
    second = _seed(dc_transport, jira_dc_project, track_issue, _uniq("rebar J11 outset par B"))
    child = _seed(
        dc_transport,
        jira_dc_project,
        track_issue,
        _uniq("rebar J11 outset subtask"),
        issuetype=subtask_type,
        extra={"parent": {"key": first}},
    )

    # SETUP — the sub-task must START under `first`, or "it is under `second` afterwards" could
    # be true before the mutation. Asserted through the SAME raw-REST oracle as the result, so
    # the two cannot disagree about where `fields.parent` lives.
    status, body = dc_request(f"/rest/api/2/issue/{child}?fields=parent")
    _assert_remote_parent_is(child, status, body, first, stage="SETUP (not the reparent)")

    # THE MUTATION UNDER TEST — rebar's own write path for a DC parent.
    dc_transport.set_parent(child, second)

    status, body = dc_request(f"/rest/api/2/issue/{child}?fields=parent")
    _assert_remote_parent_is(child, status, body, second, previous_parent=first)


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


@_skip
@_skip_no_extra
def test_outbound_delete_leaves_the_issue_absent_by_key_AND_by_id(
    dc_transport: Any, jira_dc_project: str, track_issue: Any, dc_request: Any
) -> None:
    """Row 14 outbound: a deleted DC issue must be unreachable by KEY *and* by NUMERIC ID.

    ASSERTED DIRECTLY AGAINST THE TRANSPORT, like row 12's epic case, and for a structural
    reason rather than convenience: NO differ emits an `(outbound, delete)` mutation. The only
    production callers of `delete_issue` are the create-ROLLBACK
    (`apply_outbound.py:100-113`, which deletes an issue it had just created before re-raising)
    and the typed leaf `_apply_outbound_delete` (`apply_outbound.py:168-183`), which no diff
    path reaches. Nor could one: rebar has no local ticket deletion to diff against (see
    `_dc_support.forget_identity_mapping` — "There is no library delete for a ticket"), and
    ADR 0028 §1 forbids driving a destructive action from absence, which is what the INBOUND
    row-14 cell above asserts. So the reachable claim is about the PRIMITIVE, and routing it
    through a pass would mean asserting a mutation nothing emits.

    WHY BOTH LOOKUPS, which is the whole reason this row exists rather than a bare 404 check. A
    Jira issue MOVED to another project is re-KEYED, and its old key then 404s exactly like a
    deleted one — bug 7c26, whose fix has the binding store re-ask by immutable numeric id and
    re-key on a hit (`binding_store.py:142`, `:488-506`, `:558`; the outbound differ's
    `note_absent_or_rekey` at `outbound_differ.py:703-707`). A cell that checked only the key
    would therefore report "deleted" for an issue that is alive under a new key. The numeric id
    is captured BEFORE the delete, because afterwards there is nothing left to read it from.

    ABSENCE IS ASSERTED POSITIVELY, via raw REST status codes, so "no exception was raised" is
    nowhere in the oracle: `delete_issue` absorbs a 404 as idempotent success
    (`transport.py:529-554`), which means a delete that never happened and a delete of an
    already-absent issue return the SAME value. Only the post-state discriminates them, and
    this cell first asserts the issue is READABLE (HTTP 200 by both handles) so the later 404s
    are a change it caused rather than a state it inherited.
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

    THE CELL ESTABLISHES ITS OWN PRECONDITION RATHER THAN HOPING FOR IT. The J11 harness
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
    #
    # MEASURED BY RAW REST, NOT BY `_paged_search`. This used to read
    # `len(dc_transport._paged_search(...))` — and `_paged_search` IS the pagination fix this
    # cell exists to guard (ticket 9263). So a re-truncation failed the cell HERE, at its
    # precondition, under a message reading "NOT a pagination defect": the one place a reader
    # would be told to stop looking is the place the defect was. `raw_indexed_issue_count` pages
    # with explicit `startAt`/`maxResults` and advances on what the server RETURNED, so a
    # truncating `_paged_search` now reaches the real assertion below and is named there. The
    # disclaimer in this message is only honest because the measurement is independent.
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
    was originally bundled into every round-trip cell above, and J11's first harness run
    (ticket 5200-e04e-246e-4aae) then reported 19
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
