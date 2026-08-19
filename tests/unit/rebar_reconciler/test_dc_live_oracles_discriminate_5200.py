"""MUTATION CHECKS for the three J11 live oracles repaired under ticket 5200.

WHY THESE EXIST AT ALL. The DC harness image is linux/amd64-only and does not boot on an
arm64 workstation (three measured attempts, each over an hour — see the suite's README), and a
module under ``tests/external/live_jira_dc/`` without a harness skipif burns a 20-minute budget
and then errors (measured 1208s). So a repaired live oracle CANNOT be shown to work by running
it. The suite's answer, established by
``test_inbound_assignee_oracle_discriminates_5200.py``, is to keep each oracle's discriminating
logic in ``_dc_support`` and drive it here, harness-free, RED and GREEN. Each oracle is run
VERBATIM — imported, not paraphrased — because a paraphrase can stay red while the live cell
has quietly gone vacuous, which is this epic's signature failure mode.

THE THREE GAPS, all found by an independent verification pass over the story's 18 acceptance
criteria:

  1. Inbound cell ``08-assign`` could not fail in EITHER half. It assigned the harness admin to
     an issue that ``bound_dc_issue`` already delivers assigned to the project lead (the admin),
     and then asserted only that the local ``.assignee`` was TRUTHY — a field the fixture's own
     binding pass had already populated. Both the mutation and the oracle were no-ops.
  2. Row 1 OUTBOUND had NO TEST. ``grep -rn "rebar-id:\\|properties/local_id" tests/external/``
     returned nothing, so the provenance markers an outbound create plants were unasserted in
     both label and entity-property form.
  3. The pagination cell measured its PRECONDITION with ``_paged_search`` — the very fix it
     exists to guard (ticket 9263) — so a re-truncation failed the precondition under a message
     that read "NOT a pagination defect".
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENGINE = _REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"

#: The harness admin, i.e. the value ``_dc_support.ADMIN_USER`` defaults to. Restated as a
#: literal rather than imported so a mistaken change to that default cannot make these checks
#: agree with the live cell by construction.
_DC_USER = "admin"
#: A Data Center user object, exactly as a DC search document carries it: a ``name``, NO
#: ``accountId`` (bug 5f48's shape).
_DC_USER_OBJECT = {"name": _DC_USER, "displayName": "Administrator"}


def _load_dc_support() -> Any:
    """Import ``tests/external/live_jira_dc/_dc_support.py`` BY PATH.

    That directory is only on ``sys.path`` while pytest collects the external suite, so a plain
    ``import _dc_support`` works in a full run and fails in a unit-only one. ``_dc_support`` is
    not a ``test_*.py`` module, so importing it collects nothing.

    Its import builds ``skip_no_harness``, which PROBES the harness over the network. Unit tests
    forbid network access, so the probe is stubbed to its unreachable answer for the duration of
    the import only — nothing here consults that marker.
    """
    path = _REPO_ROOT / "tests" / "external" / "live_jira_dc" / "_dc_support.py"
    spec = importlib.util.spec_from_file_location("_dc_support_for_5200_live_oracles", path)
    assert spec and spec.loader, f"could not load the live suite's helpers from {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    real_urlopen = urllib.request.urlopen

    def _no_probe(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("harness probe suppressed: unit tests do not touch the network")

    urllib.request.urlopen = _no_probe  # type: ignore[assignment]
    try:
        spec.loader.exec_module(module)
    finally:
        urllib.request.urlopen = real_urlopen  # type: ignore[assignment]
    return module


@pytest.fixture
def support() -> Any:
    return _load_dc_support()


def _old_truthiness_oracle(ticket: dict[str, Any]) -> None:
    """Cell ``08-assign``'s ORACLE AS IT STOOD, transcribed from the pre-fix source.

    Kept here on purpose: "the new oracle is stricter" is a claim, and the only way to show the
    repair has TEETH is to exhibit a state the old one accepts and the new one rejects.
    """
    assert ticket.get("assignee"), "inbound assignee did not reach the local ticket"


# ===========================================================================
# GAP 1 — inbound cell 08-assign: exact assignee, not truthiness
# ===========================================================================


def test_the_repaired_assign_oracle_rejects_what_the_truthy_one_accepted(support: Any) -> None:
    """THE EVIDENCE THAT THE REPAIR HAS TEETH, and the whole reason gap 1 was worth closing.

    The state below is the state the live cell actually ran in: the local ticket carries the
    assignee ``bound_dc_issue``'s BINDING PASS imported when the seeded issue arrived
    default-assigned to the project lead. If inbound assignee sync were completely broken, that
    value would still be sitting there, and the old oracle would still be green.
    """
    stale = {"assignee": "someone-the-binding-pass-imported"}

    _old_truthiness_oracle(stale)  # green — this is the vacuity

    with pytest.raises(AssertionError) as excinfo:
        support.assert_local_assignee_is(stale, _DC_USER)

    message = str(excinfo.value)
    assert "expected EXACTLY" in message and _DC_USER in message, (
        f"the repaired oracle failed, but not for the wrong assignee — message was {message!r}"
    )


def test_the_repaired_assign_oracle_passes_on_the_value_the_pass_writes(support: Any) -> None:
    """THE GREEN HALF, with the expected value DERIVED FROM PRODUCTION rather than guessed.

    ``.assignee`` is written as ``_extract_name(fields["assignee"])`` on both inbound paths
    (``apply_inbound_records.py:210`` on create, ``:370`` on update), and
    ``inbound_translate._extract_name`` prefers ``name`` over ``displayName``. Driving that
    function with a REAL Data Center user object is what proves the oracle expects the DC
    USERNAME and not the display name — assert ``== "Administrator"`` and the live cell would go
    red against a perfectly working bridge.
    """
    spec = importlib.util.spec_from_file_location(
        "_inbound_translate_for_5200", _ENGINE / "inbound_translate.py"
    )
    assert spec and spec.loader
    translate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = translate
    spec.loader.exec_module(translate)

    landed = translate._extract_name(dict(_DC_USER_OBJECT))
    assert landed == _DC_USER, (
        f"production extraction puts {landed!r} on the local ticket for a DC assignee, not "
        f"{_DC_USER!r}; the live oracle's expected value is wrong and the cell would be red "
        f"against a working bridge"
    )

    support.assert_local_assignee_is({"assignee": landed}, _DC_USER)


def test_the_repaired_assign_oracle_goes_red_when_nothing_landed(support: Any) -> None:
    """The plain miss. Both oracles catch this one; it is asserted so the repair is not
    mistaken for having traded the easy failure away for the hard one."""
    with pytest.raises(AssertionError):
        _old_truthiness_oracle({"assignee": ""})
    with pytest.raises(AssertionError):
        support.assert_local_assignee_is({"assignee": ""}, _DC_USER)
    with pytest.raises(AssertionError):
        support.assert_local_assignee_is({}, _DC_USER)


def test_the_cells_unassigned_precondition_is_itself_a_real_gate(support: Any) -> None:
    """THE SETUP THE REPAIRED CELL NOW DOES FOR ITSELF, driven through the same helper.

    The cell's discriminating power rests entirely on the local assignee being EMPTY before it
    assigns — that is what makes the later value attributable to the pass under test rather than
    to the binding pass. So the emptiness check has to be able to fail, and to say which of the
    two things went wrong when it does.
    """
    with pytest.raises(AssertionError) as excinfo:
        support.assert_local_assignee_is(
            {"assignee": _DC_USER}, "", stage="SETUP (not the assignment)"
        )
    message = str(excinfo.value)
    assert "STILL ASSIGNED" in message and "SETUP" in message, (
        f"the precondition failed without naming itself as setup — message was {message!r}"
    )

    support.assert_local_assignee_is({"assignee": ""}, "")
    support.assert_local_assignee_is({}, "")


# ===========================================================================
# GAP 2 — row 1 outbound: the provenance label AND the entity property
# ===========================================================================


def _property_ok(local_id: str) -> tuple[int, dict[str, Any]]:
    """A raw-REST entity-property read as DC answers it: value stored VERBATIM."""
    return 200, {"key": "local_id", "value": local_id}


def test_the_writer_emits_the_COLON_label_form_and_nothing_emits_the_hyphen(support: Any) -> None:
    """WHICH FORM ROW 1 ASSERTS, pinned to the WRITERS rather than to a reading of them.

    Both forms exist in this codebase (see the exclusion list in ``inbound_differ.py`` about bug
    ``eadb``), and only one is written. All three writers emit ``rebar-id:<local_id>``:
    ``dispatch_one.py`` (the outbound create this row is about),
    ``apply_inbound_records.py`` (the inbound-create write-back) and
    ``binding_recovery.py`` (pending-binding recovery). The hyphen form is READ-ONLY legacy.
    Asserted as source text because a label literal is a string, which is precisely the thing a
    semantic reference search cannot see.

    The writer list is keyed on FILE NAME, so it pins each writer's location as well as its
    behaviour and must be retargeted whenever one moves. Recovery moved out of
    ``binding_store.py`` into ``binding_recovery.py`` in RP-02 S3 (``polarized-servile-jenny``),
    which is why the third entry changed; the census itself is unweakened, and a grep for the
    literal across ``src/`` still finds exactly these three writers. The per-writer line numbers
    this docstring used to carry were already stale and have been dropped rather than refreshed —
    naming the module is enough to find the call site, and a line number here goes stale on every
    unrelated edit above it.

    Source-text matching is inherently coupled to WHERE the literal is written, so this is not
    the only oracle for it. The BEHAVIOURAL twin lives in
    ``state/test_binding_recovery.py::test_keyed_pending_recovery_performs_no_search``, which
    drives recovery against a recording client and asserts the label it actually emits. If this
    census and that test ever disagree, the behavioural one is right.
    """
    writers = ("dispatch_one.py", "apply_inbound_records.py", "binding_recovery.py")
    for name in writers:
        source = (_ENGINE / name).read_text()
        assert 'f"rebar-id:{local_id}"' in source, (
            f"{name} no longer writes the colon-form label; row 1's oracle asserts "
            f"{support.REBAR_ID_LABEL_PREFIX!r} and would be wrong"
        )
        assert 'add_label(keyed, f"rebar-id-{local_id}")' not in source, (
            f"{name} now writes the HYPHEN form too; row 1's oracle asserts only the colon form"
        )
    assert support.REBAR_ID_LABEL_PREFIX == "rebar-id:"
    assert support.LEGACY_REBAR_ID_LABEL_PREFIX == "rebar-id-"


def test_the_provenance_oracle_passes_on_a_correctly_created_issue(support: Any) -> None:
    local_id = "1234-abcd-5678-ef90"
    support.assert_outbound_provenance_markers(
        local_id,
        ["rebar-id:1234-abcd-5678-ef90", "some-human-label"],
        *_property_ok(local_id),
    )


def test_the_provenance_oracle_goes_red_when_the_label_is_missing(support: Any) -> None:
    """The failure that matters most: without the label the dedup JQL at
    ``dispatch_one.py:214`` cannot re-find the issue, and the next pass creates a DUPLICATE."""
    local_id = "1234-abcd-5678-ef90"
    with pytest.raises(AssertionError) as excinfo:
        support.assert_outbound_provenance_markers(
            local_id, ["some-human-label"], *_property_ok(local_id)
        )
    message = str(excinfo.value)
    assert "provenance label" in message and f"rebar-id:{local_id}" in message, message
    assert "DUPLICATE" in message, f"the failure does not say what breaks: {message!r}"


def test_the_provenance_oracle_does_not_accept_the_hyphen_form_as_equivalent(
    support: Any,
) -> None:
    """A hyphen-only issue is a FINDING, not a pass. Nothing writes that form, so an issue this
    pass created carrying it means the create went down an unexpected path — and the message has
    to say so, or the next reader "fixes" the oracle by widening it."""
    local_id = "1234-abcd-5678-ef90"
    with pytest.raises(AssertionError) as excinfo:
        support.assert_outbound_provenance_markers(
            local_id, [f"rebar-id-{local_id}"], *_property_ok(local_id)
        )
    message = str(excinfo.value)
    assert "LEGACY HYPHEN form" in message and "no writer emits it" in message, message


def test_the_provenance_oracle_goes_red_when_the_entity_property_is_absent(support: Any) -> None:
    """The half a label-only cell would miss. The property is what inbound consumers correlate
    on, and a 404 is how "the write never landed" presents over raw REST."""
    local_id = "1234-abcd-5678-ef90"
    with pytest.raises(AssertionError) as excinfo:
        support.assert_outbound_provenance_markers(
            local_id, [f"rebar-id:{local_id}"], 404, {"errorMessages": ["not found"]}
        )
    message = str(excinfo.value)
    assert "NOT READABLE" in message and "HTTP 404" in message, message


def test_the_provenance_oracle_rejects_bug_0b27s_wrapped_value_shape(support: Any) -> None:
    """THE REASON THE PROPERTY IS READ BY RAW REST RATHER THAN THROUGH THE TRANSPORT.

    Bug 0b27 stored the value wrapped as ``{"value": …}`` — the wrong shape, breaking
    correlation without ever raising. Reading it back through the same helper that wrote it is
    consistent-but-wrong and cannot detect this; the raw endpoint's own envelope can, so the
    oracle asserts the VALUE and not merely a 200.
    """
    local_id = "1234-abcd-5678-ef90"
    with pytest.raises(AssertionError) as excinfo:
        support.assert_outbound_provenance_markers(
            local_id,
            [f"rebar-id:{local_id}"],
            200,
            {"key": "local_id", "value": {"value": local_id}},
        )
    message = str(excinfo.value)
    assert "0b27" in message and "VERBATIM" in message, message

    with pytest.raises(AssertionError):
        support.assert_outbound_provenance_markers(
            local_id, [f"rebar-id:{local_id}"], 200, {"key": "local_id", "value": "some-other-id"}
        )


# ===========================================================================
# GAP 3 — the pagination cell's precondition must not use its own subject
# ===========================================================================


class _ClampingSearch:
    """A DC search endpoint that CLAMPS ``maxResults``, which the real one does.

    ``jira.search.views.default.max`` caps the page regardless of what was asked for, so a
    SHORT page is normal rather than the end of the result set. This is the shape that turned
    a requested 250 into "20 recovered" in defects 1105 / 9263 / deac.
    """

    def __init__(self, total: int, clamp: int) -> None:
        self.total = total
        self.clamp = clamp
        self.keys = [f"RBJ-{i:04d}" for i in range(total)]
        self.requests: list[tuple[int, int]] = []

    def __call__(self, path: str) -> tuple[int, dict[str, Any]]:
        params = dict(
            part.split("=", 1) for part in path.split("?", 1)[1].split("&") if "=" in part
        )
        start_at = int(params["startAt"])
        asked = int(params["maxResults"])
        self.requests.append((start_at, asked))
        served = min(asked, self.clamp)
        window = self.keys[start_at : start_at + served]
        return 200, {
            "startAt": start_at,
            "maxResults": served,
            "total": self.total,
            "issues": [{"key": k} for k in window],
        }


def _truncating_paged_search(server: _ClampingSearch) -> list[dict[str, Any]]:
    """``_paged_search`` AS IT BEHAVED WHEN TRUNCATING — advance by the REQUESTED size and stop
    on a short page. This is the historical bug, restated so the fake server can be shown
    capable of exposing it: a precondition that cannot see truncation cannot misreport it
    either, and gap 3 is precisely that it CAN."""
    status, body = server("/rest/api/2/search?jql=project%3DRBJ&startAt=0&maxResults=100")
    assert status == 200
    issues = body["issues"]
    return issues


def test_the_raw_count_sees_every_issue_a_clamped_search_withholds(support: Any) -> None:
    """THE REPLACEMENT PRECONDITION, against the exact server behaviour that broke the old one.

    201 issues behind a server that serves at most 50 per request. The measurement has to reach
    all 201 by advancing on what was RETURNED; anything that advances on what was REQUESTED, or
    stops on a short page, under-counts and the cell then blames the index.
    """
    server = _ClampingSearch(total=201, clamp=50)

    counted = support.raw_indexed_issue_count(server, "RBJ", page_size=100)

    assert counted == 201, (
        f"the raw count recovered {counted} of 201 behind a clamping server — the replacement "
        f"precondition has inherited the truncation bug it is meant to measure. Requests made: "
        f"{server.requests}"
    )
    assert len(server.requests) >= 5, (
        f"the count terminated in {len(server.requests)} request(s); it cannot have paged 201 "
        f"issues 50 at a time, so it is reading `total` rather than the issues"
    )


def test_the_old_precondition_fails_on_the_very_defect_it_disclaimed(support: Any) -> None:
    """GAP 3 IN ONE ASSERTION. Same server, same seeded count, two measurements.

    ``_paged_search`` truncating is EXACTLY what the pagination cell exists to catch. Measured
    through it, the precondition reports 50 of 201, trips, and says "the index is lagging
    further than this suite allows. NOT a pagination defect" — actively misdirecting the reader
    away from the defect. Measured through raw REST it reports 201, the precondition holds, and
    the cell reaches the assertion that names the truncation.
    """
    server = _ClampingSearch(total=201, clamp=50)
    target = 201

    via_subject = len(_truncating_paged_search(server))
    assert via_subject < target, (
        "the truncating stand-in did not truncate, so this comparison proves nothing"
    )

    via_raw = support.raw_indexed_issue_count(server, "RBJ", page_size=100)
    assert via_raw >= target, (
        f"the independent precondition ALSO under-counted ({via_raw} of {target}); the cell "
        f"would still fail at setup under a message denying the pagination defect"
    )


def test_the_raw_count_refuses_to_report_a_partial_number(support: Any) -> None:
    """A search endpoint that never advances must RAISE, not return a small number. A partial
    count returned quietly would be read as an index-lag verdict — the same misattribution gap
    3 is about, one layer down."""

    def _stuck(path: str) -> tuple[int, dict[str, Any]]:
        return 200, {"total": 999, "issues": [{"key": "RBJ-0000"}]}

    with pytest.raises(AssertionError) as excinfo:
        support.raw_indexed_issue_count(_stuck, "RBJ", page_size=100, max_requests=4)
    assert "did not terminate" in str(excinfo.value)


def test_the_raw_count_surfaces_a_failed_request_instead_of_counting_zero(support: Any) -> None:
    def _broken(path: str) -> tuple[int, Any]:
        return 503, "service unavailable"

    with pytest.raises(AssertionError) as excinfo:
        support.raw_indexed_issue_count(_broken, "RBJ")
    assert "HTTP 503" in str(excinfo.value)


# ===========================================================================
# GAP 4 — row 12 OUTBOUND: rebar writing a parent onto a DC issue
# ===========================================================================
#
# The fourth gap in criterion 11, found after the first three were closed: row 12 had no
# OUTBOUND test at all. It looked covered because `test_outbound_clear_parent_round_trips`
# (row 13 outbound) asserts a parent IS present — but that parent came from issue CREATION
# (`extra={"parent": {"key": parent}}`), never from a rebar write.
#
# Everything below is harness-free. The DC transport is driven with a fake pycontribs client,
# which is how the rest of `tests/unit/rebar_reconciler/` exercises it, so the CONTRACT claims
# in the live cell's docstring are executable rather than asserted in prose.


class _FakeIssue:
    """A pycontribs `Issue`-shaped object: a `.raw` payload plus a recording `.update`.

    **`.update` APPLIES a `fields.parent` write back into `.raw`, and that is load-bearing**
    (bug 1a9f-50c0-e7a5-4fda). Data Center answers a sub-task parent write with 204 and silently
    ignores it, so `set_parent` now verifies by reading the field back and raises when the parent
    did not move. A double that records the write without reflecting it is indistinguishable from
    that platform bug, so it would make the sub-task cell below fail for a reason unrelated to
    what it asserts — the write SHAPE, not persistence.
    """

    def __init__(self, key: str, *, subtask: bool, parent: str | None = None) -> None:
        fields: dict[str, Any] = {"issuetype": {"name": "Sub-task", "subtask": subtask}}
        if parent:
            fields["parent"] = {"key": parent}
        self.raw = {"key": key, "fields": fields}
        self.updates: list[dict[str, Any]] = []

    def update(self, fields: dict[str, Any] | None = None, **_kwargs: Any) -> None:
        self.updates.append(fields or {})
        if fields and "parent" in fields:
            parent = fields["parent"]
            raw_fields = self.raw["fields"]
            assert isinstance(raw_fields, dict)
            if isinstance(parent, dict):
                raw_fields["parent"] = dict(parent)
            else:
                raw_fields.pop("parent", None)


class _FakeClient:
    """A pycontribs `JIRA`-shaped double.

    `fields()` is present because the REAL client has it and `set_parent` now uses it to find
    the instance's "Epic Link" id (ticket 39c1). Omitting it would make the epic path decline
    for a reason the epic cells are not testing — see the note on the epic-path cell below.
    `epic_link` set to None simulates an instance that has no such field.
    """

    def __init__(self, issue: _FakeIssue, *, epic_link: str | None = "customfield_10014") -> None:
        self._issue = issue
        self._epic_link = epic_link

    def issue(self, _remote_id: str) -> _FakeIssue:
        return self._issue

    def fields(self) -> list[dict[str, Any]]:
        out = [{"id": "summary", "name": "Summary"}]
        if self._epic_link is not None:
            out.append({"id": self._epic_link, "name": "Epic Link"})
        return out


def _dc_transport_for(issue: _FakeIssue, *, epic_link: str | None = "customfield_10014") -> Any:
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    return JiraDataCenterTransport(client=_FakeClient(issue, epic_link=epic_link), project="RBJ")


def test_dc_set_parent_WRITES_fields_parent_for_a_subtask() -> None:
    """CONTRACT, HALF ONE: the sub-task case is genuinely SUPPORTED, so row 12 outbound is a
    real round-trip and not a limitation cell.

    Nothing in `tests/unit/` exercised DC `set_parent` before this — the write shape
    (`{"parent": {"key": …}}`, `jira_datacenter/transport.py:711-712`) was unasserted at every
    level. Asserted on the PAYLOAD, because "it did not raise" is what a silent no-op also
    looks like.
    """
    issue = _FakeIssue("RBJ-3", subtask=True, parent="RBJ-1")

    _dc_transport_for(issue).set_parent("RBJ-3", "RBJ-2")

    assert issue.updates == [{"parent": {"key": "RBJ-2"}}], (
        f"DC set_parent did not PUT the sub-task's parent as fields.parent; it sent "
        f"{issue.updates!r}. The live row-12-outbound cell asserts that write landed, so a "
        f"changed shape here makes that cell assert the wrong thing."
    )


def test_dc_set_parent_USES_A_DIFFERENT_PATH_for_the_non_subtask_case() -> None:
    """CONTRACT, HALF TWO: epic and sub-task are DIFFERENT PATHS — they do not collapse into
    one already-covered case, which is why row 12 outbound needed a cell rather than an
    amended criterion.

    REWRITTEN (ticket 39c1), and the reason matters more than the edit. This cell used to
    assert `pytest.raises(NotImplementedError)`, which was correct while the epic case was
    declined. Change 1311 makes it WRITE the instance-discovered "Epic Link" custom field
    instead (the Agile-API route change 1302 tried was refuted live by harness run
    30840572608 — DC 8.17.1 404s on the greenhopper epic path).

    **It would have kept passing if left alone, and that is the point.** This module's client
    double had no `fields()`, so after 1311 the epic path declined because the double could not
    enumerate fields — NOT because epic membership is a different field. The assertion would
    have stayed green while testing nothing about the contract it names: the same vacuous-oracle
    class as the `08-assign` cell this very story had to repair. The double now carries
    `fields()` because the real client does.

    The INTENT is unchanged: the two paths must remain distinguishable, and a non-sub-task must
    never get a `fields.parent` write, which DC silently no-ops.
    """
    issue = _FakeIssue("RBJ-9", subtask=False)

    _dc_transport_for(issue).set_parent("RBJ-9", "RBJ-1")

    assert issue.updates == [{"customfield_10014": "RBJ-1"}], (
        f"the epic path must write the discovered Epic Link field; got {issue.updates!r}"
    )
    assert not any("parent" in u for u in issue.updates), (
        f"a `fields.parent` write on a non-subtask is exactly the silent no-op this path exists "
        f"to prevent: {issue.updates!r}"
    )


def test_dc_set_parent_DECLINES_when_the_instance_has_no_epic_link_field() -> None:
    """The decline SURVIVES, narrowed to the case where it is still correct.

    Removing the old decline assertion outright would drop coverage of the behaviour that keeps
    the failure attributed: an instance with no "Epic Link" field genuinely cannot represent the
    parent, and must say so rather than fall back to `fields.parent`. Since change 1305 the
    exception type is load-bearing — `dispatch_one` classifies it as
    `outbound-parent-unrepresentable` rather than the retryable `outbound-parent-failed`.
    """
    issue = _FakeIssue("RBJ-9", subtask=False)

    with pytest.raises(NotImplementedError) as excinfo:
        _dc_transport_for(issue, epic_link=None).set_parent("RBJ-9", "RBJ-1")

    message = str(excinfo.value)
    assert "sub-task" in message.lower() and "epic link" in message.lower(), (
        f"the decline must name the limitation an operator can act on; got {message!r}"
    )
    assert issue.updates == [], (
        f"the declined path still issued a write: {issue.updates!r}. A `fields.parent` write on "
        f"a non-subtask is exactly the silent no-op the decline exists to prevent."
    )


def test_the_two_dc_parent_gates_are_DISJOINT_so_a_pass_cannot_carry_this_row() -> None:
    """WHY THE LIVE CELL ASSERTS THE PRIMITIVE RATHER THAN ROUTING THROUGH A PASS.

    This is the structural finding, and it is asserted rather than argued because it is the
    justification for the cell's shape. Two gates, each defensible alone:

      * APPLY gate — DC accepts a `fields.parent` write only for a SUB-TASK (above).
      * EMIT gate — `outbound_field_diff._resolve_local_parent:136-139` OMITS the parent field
        entirely when the local parent's `ticket_type` is not `epic` (bug 8b25's hierarchy
        guard).

    A DC sub-task's parent is a STANDARD issue, which imports as local `ticket_type` "task".
    So the only child DC will apply a parent write for is the only parent the differ refuses to
    emit one for. Routing row 12 outbound through a pass would assert a mutation nothing emits
    — the row-14-outbound situation verbatim.
    """
    spec = importlib.util.spec_from_file_location(
        "_outbound_field_diff_for_5200", _ENGINE / "outbound_field_diff.py"
    )
    assert spec and spec.loader
    ofd = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ofd
    spec.loader.exec_module(ofd)

    class _Bindings:
        def get_jira_key(self, local_id: str) -> str:
            return {"parent-local": "RBJ-1"}.get(local_id, "")

    child = {"ticket_id": "child-local", "parent_id": "parent-local"}

    # A DC sub-task's parent is a Task — the differ omits it, so no mutation is emitted.
    present, value = ofd._resolve_local_parent(child, _Bindings(), {"parent-local": "task"})
    assert (present, value) == (False, None), (
        f"_resolve_local_parent no longer suppresses a non-epic parent (got {(present, value)!r}); "
        f"if the emit gate has changed, row 12 outbound may now be reachable THROUGH A PASS and "
        f"the live cell should be re-shaped as a full round-trip"
    )

    # An epic parent IS emitted — which is the case DC's set_parent declines. Both halves are
    # asserted so "the gates are disjoint" is a measurement and not a reading.
    assert ofd._resolve_local_parent(child, _Bindings(), {"parent-local": "epic"}) == (
        True,
        "RBJ-1",
    )


def _parent_doc(key: str, parent: str | None) -> tuple[int, dict[str, Any]]:
    """A raw-REST issue document as DC answers `GET /issue/{key}?fields=parent`."""
    fields: dict[str, Any] = {"parent": {"key": parent}} if parent else {}
    return 200, {"key": key, "fields": fields}


def test_the_parent_oracle_passes_when_the_write_landed(support: Any) -> None:
    support.assert_remote_parent_is(
        "RBJ-3", *_parent_doc("RBJ-3", "RBJ-2"), "RBJ-2", previous_parent="RBJ-1"
    )


def test_the_parent_oracle_names_a_silent_no_op_as_one(support: Any) -> None:
    """THE FAILURE THIS ROW EXISTS FOR. Every DC defect in this area presented the same way:
    no traceback, pass reported OK, field unchanged. So the oracle must not merely fail — it
    must say that the value it found is the value from BEFORE, or the next reader looks for a
    write that went astray instead of one that never happened."""
    with pytest.raises(AssertionError) as excinfo:
        support.assert_remote_parent_is(
            "RBJ-3", *_parent_doc("RBJ-3", "RBJ-1"), "RBJ-2", previous_parent="RBJ-1"
        )
    message = str(excinfo.value)
    assert "STILL 'RBJ-1'" in message and "silent-no-op signature" in message, message
    assert "dispatch_one.py:571-578" in message, (
        f"the message must point at the swallow that makes the post-state the only observable; "
        f"got {message!r}"
    )


def test_the_parent_oracle_goes_red_when_the_field_was_cleared_or_misdirected(
    support: Any,
) -> None:
    """A write that CLEARED the parent, and one that landed on the wrong issue, are different
    findings from a no-op and neither may pass."""
    with pytest.raises(AssertionError) as excinfo:
        support.assert_remote_parent_is(
            "RBJ-3", *_parent_doc("RBJ-3", None), "RBJ-2", previous_parent="RBJ-1"
        )
    assert "cleared the field" in str(excinfo.value), str(excinfo.value)

    with pytest.raises(AssertionError) as excinfo:
        support.assert_remote_parent_is(
            "RBJ-3", *_parent_doc("RBJ-3", "RBJ-7"), "RBJ-2", previous_parent="RBJ-1"
        )
    assert "wrong issue" in str(excinfo.value), str(excinfo.value)


def test_the_parent_oracles_setup_use_rejects_the_wrong_starting_parent(support: Any) -> None:
    """The cell asserts its STARTING parent through the same helper. Without that, "it is under
    B afterwards" could have been true before the mutation — the vacuity this session exists to
    remove — so the setup use has to be able to fail too."""
    with pytest.raises(AssertionError) as excinfo:
        support.assert_remote_parent_is(
            "RBJ-3", *_parent_doc("RBJ-3", "RBJ-2"), "RBJ-1", stage="SETUP (not the reparent)"
        )
    assert "SETUP (not the reparent)" in str(excinfo.value), str(excinfo.value)


def test_the_parent_oracle_surfaces_an_unreadable_issue_instead_of_a_missing_parent(
    support: Any,
) -> None:
    with pytest.raises(AssertionError) as excinfo:
        support.assert_remote_parent_is("RBJ-3", 404, {"errorMessages": ["gone"]}, "RBJ-2")
    assert "not readable by raw REST" in str(excinfo.value) and "HTTP 404" in str(excinfo.value)
