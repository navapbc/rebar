"""Unit coverage for the live_jira_dc harness fixtures (bug f391, epic e369).

WHY THESE ARE UNIT TESTS. The behaviour under test lives in a LIVE-tier conftest
that cannot run on every workstation — the harness base image is linux/amd64 only
(``tests/external/live_jira_dc/README.md``) — and the defect it fixes only fires
after 10 accumulated tokens. Waiting for the live tier to prove the sweep is
correct means the sweep's own failure modes (deleting a human's PAT; raising when
the endpoint is unavailable) would first be observed against a real instance. So
the PARSING and DEGRADATION paths are pinned here, against a stubbed ``_request``,
and the live job supplies the acceptance evidence that the cap is no longer hit.

The module is loaded by PATH rather than imported as ``conftest``: pytest owns
that name, and a second module claiming it collides.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_CONFTEST = pathlib.Path(__file__).resolve().parents[1] / "external/live_jira_dc/conftest.py"


def _load_harness_conftest():
    spec = importlib.util.spec_from_file_location("_live_jira_dc_conftest_under_test", _CONFTEST)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return _load_harness_conftest()


# ---------------------------------------------------------------------------
# The scope fix itself — the actual bug
# ---------------------------------------------------------------------------


def test_jira_dc_pat_is_session_scoped(harness) -> None:
    """THE BUG. Function scope mints one PAT per requesting test against a fixed
    budget of 10, so the suite dies at setup — in whatever module happened to run
    eleventh. Session scope makes consumption O(1) in test count, which is what
    makes the headroom structural instead of a coincidence of today's count."""
    # pytest 8.4+ exposes the decorator's arguments on ``_fixture_function_marker``
    # (older releases used ``_pytestfixturefunction``); read whichever is present so
    # this pins the SCOPE rather than a pytest internal's spelling.
    marker = getattr(
        harness.jira_dc_pat,
        "_fixture_function_marker",
        getattr(harness.jira_dc_pat, "_pytestfixturefunction", None),
    )
    assert marker is not None, "jira_dc_pat is not a pytest fixture at all"
    scope = marker.scope
    assert scope == "session", (
        f"jira_dc_pat is {scope!r}-scoped: it mints a NEW Jira DC Personal Access "
        f"Token per requesting test and never deletes it, so the suite exhausts "
        f"DC's 10-token-per-user cap and dies at setup"
    )


def test_the_pat_fixture_documents_the_ten_token_cap(harness) -> None:
    """The cap is a non-obvious DC limit with no Cloud analogue. Undocumented, the
    next author widens the scope back out and re-creates the bug."""
    doc = harness.jira_dc_pat.__doc__ or ""
    assert "10" in doc and "SESSION" in doc.upper(), (
        "the fixture docstring does not record the 10-token cap and the session-scope "
        "decision, so the reason for the scope is invisible at the point of change"
    )


# ---------------------------------------------------------------------------
# The sweep — parsing
# ---------------------------------------------------------------------------


def _stub_request(harness, monkeypatch, *, list_response, deleted=None):
    """Replace the module's single HTTP helper. Every call routes through
    ``_request``, so this is the whole network surface."""
    calls: list[tuple] = []

    def _fake(path, *, method="GET", payload=None, token=None, basic_auth=None, timeout=30):
        calls.append((method, path))
        if method == "DELETE":
            return (deleted if deleted is not None else 204), None
        return list_response

    monkeypatch.setattr(harness, "_request", _fake)
    return calls


def test_the_sweep_finds_leftover_harness_tokens(harness, monkeypatch) -> None:
    """A token left by a crashed run must be found so its budget can be reclaimed."""
    _stub_request(
        harness,
        monkeypatch,
        list_response=(
            200,
            [
                {"id": 1, "name": "rebar-j5-harness-aaaa1111"},
                {"id": 2, "name": "rebar-j5-harness-bbbb2222"},
            ],
        ),
    )
    found = harness._leaked_harness_tokens()
    assert sorted(t["name"] for t in found) == [
        "rebar-j5-harness-aaaa1111",
        "rebar-j5-harness-bbbb2222",
    ]


def test_the_sweep_never_touches_a_token_it_did_not_create(harness, monkeypatch) -> None:
    """TEETH, and the failure mode that would make this fix WORSE than the bug.

    The admin account may hold PATs a human created. A sweep that reclaimed budget
    by deleting those would destroy a credential the harness has no claim on. Only
    the ``rebar-j5-harness-`` prefix is ours."""
    _stub_request(
        harness,
        monkeypatch,
        list_response=(
            200,
            [
                {"id": 1, "name": "rebar-j5-harness-aaaa1111"},
                {"id": 2, "name": "my-personal-token"},
                {"id": 3, "name": "jenkins-deploy"},
                {"id": 4, "name": "rebar-j5-harness"},  # prefix-adjacent, not a match
            ],
        ),
    )
    found = harness._leaked_harness_tokens()
    assert [t["name"] for t in found] == ["rebar-j5-harness-aaaa1111"], (
        "the sweep selected a token it did not mint — it would delete a credential "
        "belonging to someone else"
    )


def test_a_token_without_an_id_is_skipped(harness, monkeypatch) -> None:
    """The creation response was only ever asserted to carry ``rawToken``; whether
    the LIST response always carries ``id`` is unverified against a live instance.
    An entry without one cannot be addressed by the DELETE endpoint, so it is
    skipped rather than used to build a malformed URL."""
    _stub_request(
        harness,
        monkeypatch,
        list_response=(200, [{"name": "rebar-j5-harness-aaaa1111"}]),
    )
    assert harness._leaked_harness_tokens() == []


# ---------------------------------------------------------------------------
# The sweep — degradation. A sweep that raises replaces one setup failure
# with another.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "list_response",
    [
        (403, {"message": "forbidden"}),
        (404, None),
        (500, "gateway blew up"),
        (200, {"unexpected": "shape"}),
        (200, None),
    ],
    ids=["forbidden", "missing-endpoint", "server-error", "non-list-body", "empty-body"],
)
def test_the_sweep_returns_empty_when_it_cannot_enumerate(
    harness, monkeypatch, list_response
) -> None:
    """Mirrors ``_leaked_scratch_projects``'s stated posture: cannot enumerate ⇒ do
    not invent a failure, and do not claim cleanliness either. Raising here would
    block every run against an instance whose PAT endpoint is unavailable — a
    reason wholly unrelated to the code under test."""
    _stub_request(harness, monkeypatch, list_response=list_response)
    assert harness._leaked_harness_tokens() == []
    assert harness._sweep_leaked_harness_tokens() == []


def test_a_failed_delete_does_not_abort_the_session(harness, monkeypatch) -> None:
    """Best-effort by design. If the reclaim was genuinely insufficient, the mint
    that follows fails with Jira's own explicit limit error — a better diagnostic
    than an assertion here, and one that cannot fire spuriously."""
    _stub_request(
        harness,
        monkeypatch,
        list_response=(200, [{"id": 1, "name": "rebar-j5-harness-aaaa1111"}]),
        deleted=403,
    )
    assert harness._sweep_leaked_harness_tokens() == []  # reported, not raised


def test_the_sweep_deletes_by_id_and_reports_what_it_reclaimed(harness, monkeypatch) -> None:
    """The reclaim must actually issue DELETEs against the token-id endpoint, and
    name what it swept — a silent sweep is indistinguishable from a no-op in the
    CI log, which is the only place this is ever diagnosed."""
    calls = _stub_request(
        harness,
        monkeypatch,
        list_response=(
            200,
            [
                {"id": 7, "name": "rebar-j5-harness-aaaa1111"},
                {"id": 9, "name": "rebar-j5-harness-bbbb2222"},
            ],
        ),
    )
    swept = harness._sweep_leaked_harness_tokens()
    assert sorted(swept) == ["rebar-j5-harness-aaaa1111", "rebar-j5-harness-bbbb2222"]
    deletes = [path for method, path in calls if method == "DELETE"]
    assert deletes == ["/rest/pat/latest/tokens/7", "/rest/pat/latest/tokens/9"]


def test_an_already_gone_token_counts_as_reclaimed(harness, monkeypatch) -> None:
    """404 means the budget is free, which is the postcondition this owes — the
    same reasoning ``track_issue`` already applies to a cascade-deleted issue."""
    _stub_request(
        harness,
        monkeypatch,
        list_response=(200, [{"id": 1, "name": "rebar-j5-harness-aaaa1111"}]),
        deleted=404,
    )
    assert harness._sweep_leaked_harness_tokens() == ["rebar-j5-harness-aaaa1111"]


# ---------------------------------------------------------------------------
# WHEN the Epic fields can be demanded — after provisioning, never before
# (bug 941b-f049-5f29-4410, correcting bug 9790-cafa-dffa-462e)
#
# 9790 was right that a `serverInfo` 200 does not imply the Epic machinery is
# usable, and wrong about why. It read a system-only field inventory as "the
# GreenHopper plugin is still starting" and made the fields a SESSION-START
# readiness predicate under a timeout. MEASURED on experiment run 30981084637:
#
#     [before]           27 fields, customfield_count=0, EpicLink=False
#     [after-180s-quiet] 27 fields, customfield_count=0, EpicLink=False
#     create project  -> HTTP 201
#     [after-create+0s]  55 fields, customfield_count=13, EpicLink=True
#
# 180 seconds of extra time changed NOTHING; creating the pinned GreenHopper
# project changed everything in 0.05s. GreenHopper provisions its custom fields
# when the first Jira SOFTWARE PROJECT is created. So an empty custom-field
# inventory is the NORMAL state of a fresh instance, not a fault — and demanding
# the fields before any project exists is a deadlock: the gate waits for a
# capability only the action it blocks can create. It expired identically at 600s
# (run 30975323866) and at 1800s (run 30978613228), erroring all 62 cells.
#
# These cells therefore pin WHERE the capability may be demanded: session
# readiness must tolerate the pre-provisioning inventory, and the post-create
# check must be a real bounded WAIT that still fails loudly.
# ---------------------------------------------------------------------------

_SYSTEM_ONLY_FIELDS = [
    {"id": f"sys{i}", "name": name, "custom": False}
    for i, name in enumerate(
        ["Attachment", "Comment", "Created", "Creator", "Key", "Project", "Status", "Updated"]
    )
]
_EPIC_FIELDS = [
    {"id": "customfield_10100", "name": "Epic Link", "custom": True},
    {"id": "customfield_10104", "name": "Epic Name", "custom": True},
]
#: The GENUINE-DEGRADE inventory: `customfield_*` entries ARE present — so a
#: Software project exists and GreenHopper has provisioned — but neither Epic
#: field is among them. This is the axis `_SYSTEM_ONLY_FIELDS` does not vary, and
#: it is the one that still has to fail loudly.
_DEGRADED_CUSTOM_FIELDS = [
    *_SYSTEM_ONLY_FIELDS,
    {"id": "customfield_10200", "name": "Sprint", "custom": True},
    {"id": "customfield_10201", "name": "Story Points", "custom": True},
]


def _shared_readiness(harness):
    """The one shared readiness definition, or a failure that says it is missing.

    Deliberately not a hard attribute access: a bare ``AttributeError`` reads as a
    broken test, while the thing actually being asserted is that the harness routes
    through a shared definition at all.
    """
    module = getattr(harness, "jira_dc_field_readiness", None)
    assert module is not None, (
        "the harness conftest does not import a shared field-readiness module, so its "
        "readiness definition is its own and can drift from the probe's"
    )
    return module


@pytest.fixture
def fast_field_poll(harness, monkeypatch):
    """Collapse the field wait's wall clock so these cells stay sub-second."""
    monkeypatch.setenv("JIRA_DC_FIELD_READY_TIMEOUT", "0.05")
    readiness = getattr(harness, "jira_dc_field_readiness", None)
    if readiness is not None:
        monkeypatch.setattr(readiness, "FIELD_POLL_INTERVAL_S", 0.001)
        monkeypatch.setattr(readiness, "FIELD_READY_BUDGET_S", 0.05)
    return readiness


def _stub_readiness_transport(harness, monkeypatch, field_bodies):
    """Answer serverInfo 200 always; serve ``field_bodies`` in order (last repeats)."""
    seen: list[str] = []
    remaining = list(field_bodies)

    def _fake(path, *, method="GET", payload=None, token=None, basic_auth=None, timeout=30):
        seen.append(path)
        if path == "/rest/api/2/serverInfo":
            return 200, {"version": "8.17.1"}
        if path == "/rest/api/2/field":
            body = remaining[0] if len(remaining) == 1 else remaining.pop(0)
            return 200, body
        if path.startswith("/rest/api/2/project/"):
            return 200, {"issueTypes": [{"name": n} for n in ("Task", "Sub-task", "Epic")]}
        return 404, None

    monkeypatch.setattr(harness, "_request", _fake)
    return seen


def test_session_readiness_tolerates_the_pre_provisioning_inventory(
    harness, monkeypatch, fast_field_poll
) -> None:
    """THE DEADLOCK (bug 941b-f049-5f29-4410). A fresh Jira Software instance with
    no project has ZERO custom fields — measured on run 30981084637, and unchanged after 180s of
    additional quiet time. 9790 read that state as "not ready yet" and blocked
    session start on it, but the fields are provisioned BY the first project
    create, which cannot happen while session start is blocked. So the gate waited
    for something only the action it blocked could produce, and expired identically
    at 600s and at 1800s, erroring all 62 cells at setup.

    Session readiness must therefore RETURN against this inventory. It is the
    normal pre-provisioning state, not a fault."""
    _stub_readiness_transport(harness, monkeypatch, [_SYSTEM_ONLY_FIELDS])

    harness.wait_for_jira_dc_ready(timeout=0.05)


def test_session_readiness_does_not_gate_on_the_epic_fields_at_all(
    harness, monkeypatch, fast_field_poll
) -> None:
    """Teeth for the cell above, and the reason it is not just "loosen the check".

    A gate that merely got a bigger budget would still be waiting on the wrong
    predicate; the point is that the Epic fields are NOT a session-start property
    of the instance. Session readiness must not poll for them at all — the
    inventory it would read cannot answer the question before a project exists."""
    seen = _stub_readiness_transport(harness, monkeypatch, [_SYSTEM_ONLY_FIELDS])

    harness.wait_for_jira_dc_ready(timeout=0.05)

    assert "/rest/api/2/serverInfo" in seen, "session readiness never asked whether Jira answers"
    assert "/rest/api/2/field" not in seen, (
        f"session readiness still polls the field inventory (saw {sorted(set(seen))}), so it is "
        f"still gating on a capability that does not exist until a Software project is created"
    )


def test_the_capability_check_waits_after_the_project_exists(harness, monkeypatch) -> None:
    """The capability guard 9790 wanted, relocated to where it can be answered.

    Moving the check off session start must not delete it. Once the project
    EXISTS the fields can appear, so `_assert_project_capabilities` has to be a
    real bounded WAIT rather than a single read — provisioning is observed at
    0.05s on a quiet runner, but a one-shot read makes that a race. The fields
    arrive on the second poll here."""
    monkeypatch.setattr(_shared_readiness(harness), "FIELD_POLL_INTERVAL_S", 0.001)
    seen = _stub_readiness_transport(
        harness, monkeypatch, [_SYSTEM_ONLY_FIELDS, _SYSTEM_ONLY_FIELDS + _EPIC_FIELDS]
    )

    harness._assert_project_capabilities("RBTEST")

    assert seen.count("/rest/api/2/field") >= 2, (
        f"the capability check read the field inventory {seen.count('/rest/api/2/field')} "
        f"time(s) — it is a one-shot read, so it races provisioning instead of waiting for it"
    )


def test_a_degraded_image_still_fails_the_capability_check_loudly(
    harness, monkeypatch, fast_field_poll
) -> None:
    """The guard 9790 removed must NOT come back. Once `customfield_*` entries are
    present the instance has provisioned, so Epic fields that are still missing is
    a GENUINE DEGRADE and has to fail loudly rather than let 62 cells run against
    an instance whose declared contract cannot hold. This varies the axis the
    system-only fixture holds fixed."""
    _stub_readiness_transport(harness, monkeypatch, [_DEGRADED_CUSTOM_FIELDS])

    with pytest.raises(AssertionError) as excinfo:
        harness._assert_project_capabilities("RBTEST")

    message = str(excinfo.value)
    assert "Epic Link" in message and "Epic Name" in message, (
        "the abort does not name which required field(s) are missing"
    )
    assert "Sprint" in message and "Story Points" in message, (
        "the abort does not dump the OTHER custom fields it saw — the only evidence that "
        "separates 'nothing provisioned yet' from 'this image dropped the Epic fields'"
    )


def test_the_failure_message_does_not_send_the_reader_at_the_budget(
    harness, monkeypatch, fast_field_poll
) -> None:
    """THE MOST EXPENSIVE ARTIFACT OF THE BUG, so it gets its own cell.

    9790's message states a decision rule: *"if it contains no customfield_*
    entries whatsoever the plugin is still starting and the budget
    (JIRA_DC_FIELD_READY_TIMEOUT) is too short"*. That rule is wrong AND
    self-confirming — an empty custom-field inventory is the normal state of an
    instance with no project, so the message points every reader at the budget.
    It misdiagnosed 9790, it misdiagnosed this bug's own opening analysis, and it
    bought a 50-minute run at 1800s that failed byte-identically to the 600s one.

    A message that names the wrong remedy is worse than no message, so the prose
    must name the real precondition and must not blame the budget."""
    _stub_readiness_transport(harness, monkeypatch, [_SYSTEM_ONLY_FIELDS])

    with pytest.raises(AssertionError) as excinfo:
        harness._assert_project_capabilities("RBTEST")

    message = str(excinfo.value)
    lowered = message.lower()
    assert "project" in lowered and (
        "provision" in lowered or "created" in lowered or "creation" in lowered
    ), (
        "the failure does not name the real precondition — that GreenHopper provisions these "
        f"fields when the first Software project is created. Message: {message!r}"
    )
    assert "budget" not in lowered and "JIRA_DC_FIELD_READY_TIMEOUT" not in message, (
        "the failure still points the reader at the readiness budget, which is the diagnosis "
        f"that cost two live runs and is refuted by run 30981084637. Message: {message!r}"
    )


def test_the_probe_creates_its_project_before_it_awaits_the_epic_fields(monkeypatch) -> None:
    """THE PARITY SIBLING. The deterministic probe has the identical ordering bug —
    `_await_named_fields` runs before its project create — which is why probe runs
    30944211742 and 30930839323 both died at `PROBE SETUP FAILED: Epic Link=None
    Epic Name=None`. Fixing only the harness would leave the probe deadlocked, and
    the probe is the cheap tool this class of question gets answered with."""
    import importlib.util
    import pathlib as _pathlib

    repo_root = _pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_probe_order_under_test", repo_root / "scripts/jira_dc_epic_link_clear_probe.py"
    )
    assert spec and spec.loader
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    order: list[str] = []

    def _fake_req(path, method="GET", payload=None, **kw):
        if method == "POST" and path == "/rest/api/2/project":
            order.append("create-project")
            return 201, {"key": payload["key"]}
        if path == "/rest/api/2/field":
            order.append("await-fields")
            # Model the instance faithfully: NO custom fields until the project
            # exists, both Epic fields immediately afterwards.
            if "create-project" in order:
                return 200, _SYSTEM_ONLY_FIELDS + _EPIC_FIELDS
            return 200, _SYSTEM_ONLY_FIELDS
        if method == "POST" and path == "/rest/api/2/issue":
            return 201, {"key": "ELP-1"}
        # Anything else (the idempotent project DELETE, issue reads/writes) is not
        # part of the ordering question — answer it blandly rather than raising,
        # so an unmodelled call can never masquerade as "the probe did nothing".
        return 200, {}

    monkeypatch.setattr(probe, "_req", _fake_req)
    monkeypatch.setattr(probe.jira_dc_field_readiness, "FIELD_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(probe.jira_dc_field_readiness, "FIELD_READY_BUDGET_S", 0.05)

    try:
        probe.main()
    except BaseException:  # noqa: BLE001 - only the ORDER of the first two ops is under test
        pass

    first = [step for step in order if step in ("create-project", "await-fields")]
    assert first, "the probe performed neither a project create nor a field read"
    assert first[0] == "create-project", (
        f"the probe awaits the Epic fields before creating its project (order: {first[:4]}), so "
        f"it waits for fields that only the create it has not reached can provision — the "
        f"deadlock that made runs 30944211742 and 30930839323 fail at setup"
    )


def test_the_probe_and_the_harness_share_one_readiness_definition(harness, monkeypatch) -> None:
    """AC4, the anti-drift pin. Gerrit 1387 fixed the probe alone and by doing so
    CREATED the divergence this criterion exists to prevent. Both tiers must route
    through the same function, so a change to the required names or the budget
    cannot land on one path only."""
    import importlib.util
    import pathlib as _pathlib

    repo_root = _pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_probe_under_test", repo_root / "scripts/jira_dc_epic_link_clear_probe.py"
    )
    assert spec and spec.loader
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    readiness = _shared_readiness(harness)
    assert getattr(probe, "jira_dc_field_readiness", None) is readiness, (
        "the probe does not import the shared readiness module, so the two tiers hold "
        "two definitions and can drift again"
    )
    assert tuple(harness._REQUIRED_FIELDS) == tuple(readiness.REQUIRED_FIELDS), (
        "the harness declares its own required-field list instead of the shared one"
    )

    routed: list[tuple[str, ...]] = []
    real = readiness.await_required_fields

    def _spy(request, **kwargs):
        routed.append(tuple(kwargs.get("names") or readiness.REQUIRED_FIELDS))
        return real(request, **kwargs)

    monkeypatch.setattr(readiness, "await_required_fields", _spy)
    monkeypatch.setattr(probe, "_req", lambda path, **kw: (200, _SYSTEM_ONLY_FIELDS + _EPIC_FIELDS))
    monkeypatch.setattr(readiness, "FIELD_POLL_INTERVAL_S", 0.001)
    _stub_readiness_transport(harness, monkeypatch, [_SYSTEM_ONLY_FIELDS + _EPIC_FIELDS])

    probe._await_named_fields(readiness.REQUIRED_FIELDS)
    harness._assert_project_capabilities("RBTEST")

    assert len(routed) == 2, (
        f"only {len(routed)} of the two tiers routed through the shared wait: "
        f"one of them still has its own readiness definition"
    )


def test_an_unreadable_field_inventory_reads_as_not_ready(
    harness, monkeypatch, fast_field_poll
) -> None:
    """The vacuous-pass guard, same family as 3fe5 and 59b2. "We could not see the
    fields" is NOT evidence that they are there. If an unusable read (a 503, or a
    body that is not a list) collapsed into "nothing missing", readiness would pass
    on exactly the instance it cannot vouch for — and the suite would rediscover it
    as a mid-run capability abort."""

    def _fake(path, *, method="GET", payload=None, token=None, basic_auth=None, timeout=30):
        if path == "/rest/api/2/serverInfo":
            return 200, {"version": "8.17.1"}
        if path == "/rest/api/2/field":
            return 503, {"errorMessages": ["Jira is starting up"]}
        if path.startswith("/rest/api/2/project/"):
            return 200, {"issueTypes": [{"name": n} for n in ("Task", "Sub-task", "Epic")]}
        return 404, None

    monkeypatch.setattr(harness, "_request", _fake)

    with pytest.raises(AssertionError) as excinfo:
        harness._assert_project_capabilities("RBTEST")

    message = str(excinfo.value)
    assert "Epic Link" in message and "Epic Name" in message, (
        "an unreadable field inventory was not reported as leaving the required fields unconfirmed"
    )
    assert "503" in message, "the failure does not record the HTTP status it actually got"
