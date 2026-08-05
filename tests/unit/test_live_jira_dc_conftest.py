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
# Readiness is the FIELD INVENTORY, not a serverInfo 200 (bug 9790-cafa-dffa-462e)
#
# WHY HERE. `wait_for_jira_dc_ready` used to return on the first
# `/rest/api/2/serverInfo` 200 and never read `/rest/api/2/field` at all, while
# `_assert_project_capabilities` — running later in the same session — asserts
# `_REQUIRED_FIELDS` as a hard precondition. Measured on probe run 30944211742:
# `serverInfo` went green at 8m32s and ONE SECOND later the inventory was 27
# fields, every one a SYSTEM field, no `customfield_*`. The suite survived only
# because it does slower work afterwards, so the margin was incidental. These
# cells pin the CAPABILITY as the readiness predicate, and the negative one is
# load-bearing: a system-only inventory must read as NOT ready rather than as a
# degraded image.
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


def test_a_system_only_field_inventory_reads_as_not_ready(
    harness, monkeypatch, fast_field_poll
) -> None:
    """THE LOAD-BEARING ASSERTION (AC5). An inventory of only system fields is the
    state the probe measured one second after `serverInfo` went green. It means the
    GreenHopper plugin has not registered its fields YET — it does not mean the
    image stopped offering them — so readiness must refuse rather than hand the
    suite an instance whose declared contract cannot hold."""
    _stub_readiness_transport(harness, monkeypatch, [_SYSTEM_ONLY_FIELDS])

    with pytest.raises(RuntimeError) as excinfo:
        harness.wait_for_jira_dc_ready(timeout=0.05)

    message = str(excinfo.value)
    assert "Epic Link" in message and "Epic Name" in message, (
        "readiness failed without naming which required field(s) never arrived"
    )


def test_the_not_ready_failure_dumps_the_observed_inventory(
    harness, monkeypatch, fast_field_poll
) -> None:
    """AC2. The failure has to be self-diagnosing: the reader must be able to tell
    'no custom fields at all yet' from 'the image renamed them' without re-running
    anything, which takes the observed inventory in the message."""
    _stub_readiness_transport(harness, monkeypatch, [_SYSTEM_ONLY_FIELDS])

    with pytest.raises(RuntimeError) as excinfo:
        harness.wait_for_jira_dc_ready(timeout=0.05)

    message = str(excinfo.value)
    assert "Attachment" in message and "Status" in message, (
        "the expiry message does not dump the field inventory it actually observed"
    )
    assert "never registered" in message.lower(), (
        "the expiry message does not say the fields never REGISTERED, which is what "
        "distinguishes a timing failure from an image degrade"
    )


def test_readiness_actually_polls_the_field_inventory(harness, monkeypatch) -> None:
    """The discriminator. A gate that only ever touches `/rest/api/2/serverInfo` is
    structurally blind to the capability, whatever its budget is."""
    seen = _stub_readiness_transport(harness, monkeypatch, [_SYSTEM_ONLY_FIELDS + _EPIC_FIELDS])

    harness.wait_for_jira_dc_ready(timeout=5)

    assert "/rest/api/2/field" in seen, (
        f"readiness declared the instance ready having polled only {sorted(set(seen))} — "
        f"it never asked whether the Epic fields exist"
    )


def test_readiness_returns_once_the_epic_fields_register(harness, monkeypatch) -> None:
    """Positive control for the negative above: the refusal must be caused by the
    missing capability, not by the wait being unable to succeed at all. The fields
    arrive on the second poll, exactly as they do on a live cold start."""
    monkeypatch.setattr(_shared_readiness(harness), "FIELD_POLL_INTERVAL_S", 0.001)
    _stub_readiness_transport(
        harness, monkeypatch, [_SYSTEM_ONLY_FIELDS, _SYSTEM_ONLY_FIELDS + _EPIC_FIELDS]
    )

    harness.wait_for_jira_dc_ready(timeout=5)


def test_the_capability_abort_distinguishes_not_registered_from_not_offered(
    harness, monkeypatch, fast_field_poll
) -> None:
    """AC3. `_assert_project_capabilities`' old message sent the reader straight at
    the image ('update `_REQUIRED_FIELDS` if the instance genuinely renamed them'),
    which is the wrong diagnosis when the plugin simply had not finished starting.
    The message must name BOTH candidate causes."""
    _stub_readiness_transport(harness, monkeypatch, [_SYSTEM_ONLY_FIELDS])

    with pytest.raises(AssertionError) as excinfo:
        harness._assert_project_capabilities("RBTEST")

    message = str(excinfo.value).lower()
    assert "never registered" in message, (
        "the abort does not offer 'the fields never registered' as a candidate cause"
    )
    assert "does not offer" in message, (
        "the abort does not offer 'this image does not offer them' as a candidate cause"
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
    harness.wait_for_jira_dc_ready(timeout=5)

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
        return 404, None

    monkeypatch.setattr(harness, "_request", _fake)

    with pytest.raises(RuntimeError) as excinfo:
        harness.wait_for_jira_dc_ready(timeout=0.05)

    message = str(excinfo.value)
    assert "Epic Link" in message and "Epic Name" in message, (
        "an unreadable field inventory was not reported as leaving the required fields unconfirmed"
    )
    assert "503" in message, "the failure does not record the HTTP status it actually got"
