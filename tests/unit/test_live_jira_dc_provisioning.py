"""Unit coverage for the live_jira_dc harness's PROVISIONING contract (3fe5, epic e369).

WHY THESE ARE UNIT TESTS, and why they are the right tier for this bug.

The defect 3fe5 records is that ``_create_scratch_project`` walked a fallback chain
— every discovered template, then a bare ``software`` project, then no template at
all — and took the first ``201`` **without asserting anything about what it got**.
A degrade to a template lacking the ``Epic`` issue type is indistinguishable on the
wire from a good provision, so the failure surfaced 35 minutes later inside one
cell as ``SETUP FAILED: project offers no 'Epic' issue type``.

The live tier cannot be the first place that regression is caught: a run costs ~35
minutes and the harness image is linux/amd64 only. So the DECLARED CONTRACT and its
drift detector are pinned here against a stubbed ``_request``, and the live job
supplies the acceptance evidence that the epic cells now execute.

Capability-map run 30863672922 (Jira DC 8.17.1) is the source of the declared
values: all three software templates yield ``Epic``, ``Epic Link`` is
``customfield_10001`` and ``Epic Name`` is ``customfield_10003``. That run ALSO
falsified a predicted requirement — ``Epic Link`` is not on the default edit screen
and the REST PUT persists anyway — so provisioning deliberately does NOT touch
screens.

The module is loaded by PATH rather than imported as ``conftest``: pytest owns that
name, and a second module claiming it collides.
"""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

import pytest

_CONFTEST = pathlib.Path(__file__).resolve().parents[1] / "external/live_jira_dc/conftest.py"


def _load_harness_conftest():
    spec = importlib.util.spec_from_file_location(
        "_live_jira_dc_provisioning_under_test", _CONFTEST
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return _load_harness_conftest()


# A project payload shaped like the real ``GET /rest/api/2/project/{key}`` response.
def _project_body(*type_names: str) -> dict[str, Any]:
    return {
        "key": "RBTEST",
        "issueTypes": [{"name": name, "subtask": name == "Sub-task"} for name in type_names],
    }


def _field_body(*field_names: str) -> list[dict[str, Any]]:
    return [{"id": f"customfield_{10000 + i}", "name": name} for i, name in enumerate(field_names)]


_GOOD_TYPES = ("Task", "Sub-task", "Epic")
_GOOD_FIELDS = ("Epic Link", "Epic Name", "Summary")


def _stub_request(
    harness,
    monkeypatch,
    *,
    create_status: int = 201,
    project_body: Any = None,
    project_status: int = 200,
    fields: Any = None,
    fields_status: int = 200,
):
    """Replace the module's single HTTP helper — the whole network surface.

    Records every (method, path, payload) so a test can assert what provisioning
    actually asked the instance for, not merely that it did not raise.
    """
    calls: list[tuple[str, str, Any]] = []
    body_for_project = _project_body(*_GOOD_TYPES) if project_body is None else project_body
    body_for_fields = _field_body(*_GOOD_FIELDS) if fields is None else fields

    def _fake(path, *, method="GET", payload=None, token=None, basic_auth=None, timeout=30):
        calls.append((method, path, payload))
        if method == "POST" and path == "/rest/api/2/project":
            return create_status, ({"key": "RBTEST"} if create_status == 201 else "boom")
        if path.startswith("/rest/api/2/project/"):
            return project_status, body_for_project
        if path == "/rest/api/2/field":
            return fields_status, body_for_fields
        return 200, {}

    monkeypatch.setattr(harness, "_request", _fake)
    # Pin the generated key so the fixture cells can assert on an exact value; the
    # randomness is a collision guard on a live instance, irrelevant under a stub.
    monkeypatch.setattr(harness, "_random_project_key", lambda: "RBTEST")
    return calls


# ---------------------------------------------------------------------------
# HAPPY PATH — the pinned template is used, a conforming project passes
# ---------------------------------------------------------------------------


def test_the_scratch_project_is_created_from_the_pinned_software_template(
    harness, monkeypatch
) -> None:
    """DECLARE, don't discover. The template is a constant read from the capability
    map, so every run provisions the SAME environment and an image that stops
    offering it fails loudly instead of quietly yielding a weaker project."""
    calls = _stub_request(harness, monkeypatch)

    status, _ = harness._create_scratch_project("RBTEST")

    assert status == 201
    creates = [c for c in calls if c[0] == "POST" and c[1] == "/rest/api/2/project"]
    assert len(creates) == 1, f"expected exactly one create attempt, got {creates}"
    payload = creates[0][2]
    assert payload["projectTemplateKey"] == harness._PROJECT_TEMPLATE
    assert payload["projectTypeKey"] == "software"


def test_a_conforming_project_passes_the_capability_assertion(harness, monkeypatch) -> None:
    """The positive control. Without this, an assertion that ALWAYS raises would
    still satisfy every negative test below."""
    _stub_request(harness, monkeypatch)

    harness._assert_project_capabilities("RBTEST")  # must not raise


# ---------------------------------------------------------------------------
# THE DRIFT DETECTOR — held out from the implementer
# ---------------------------------------------------------------------------


def test_a_project_without_epic_aborts_provisioning_and_names_the_gap(harness, monkeypatch) -> None:
    """THE BUG 3fe5 FILED. A project lacking ``Epic`` must abort AT PROVISIONING
    with the diff, not yield a project that cannot host half the suite and surface
    35 minutes later inside one cell."""
    _stub_request(harness, monkeypatch, project_body=_project_body("Task", "Sub-task"))

    with pytest.raises(AssertionError) as excinfo:
        harness._assert_project_capabilities("RBTEST")

    message = str(excinfo.value)
    assert "Epic" in message, "the abort does not name the missing issue type"
    assert "Sub-task" in message and "Task" in message, (
        "the abort does not report what the project ACTUALLY got, so the reader "
        "cannot tell a template change from an outage"
    )


def test_provisioning_never_falls_back_to_a_weaker_project(harness, monkeypatch) -> None:
    """THE SILENT DEGRADE, which is the actual mechanism of this bug. When the
    pinned template is rejected, the old chain retried with a bare ``software``
    project and then with no template at all, taking whatever succeeded. A
    substitute environment must never be provisioned behind the suite's back."""
    calls = _stub_request(harness, monkeypatch, create_status=400)

    status, body = harness._create_scratch_project("RBTEST")

    assert status != 201
    creates = [c for c in calls if c[0] == "POST" and c[1] == "/rest/api/2/project"]
    assert len(creates) == 1, (
        f"provisioning made {len(creates)} create attempts — it is still walking a "
        f"fallback chain, so a degraded project can still be substituted silently: {creates}"
    )
    assert harness._PROJECT_TEMPLATE in str(body), (
        "the failure does not name the pinned template that was refused"
    )


def test_an_unreadable_project_fails_loudly_rather_than_passing_vacuously(
    harness, monkeypatch
) -> None:
    """If the capability read itself fails, the contract is UNVERIFIED — which must
    abort. Treating a non-200 as 'nothing missing' would restore exactly the silent
    success this assertion exists to remove."""
    _stub_request(harness, monkeypatch, project_status=500, project_body="gateway blew up")

    with pytest.raises(AssertionError) as excinfo:
        harness._assert_project_capabilities("RBTEST")

    assert "500" in str(excinfo.value), "the abort does not report the failing status"


def test_a_missing_epic_field_aborts_provisioning(harness, monkeypatch) -> None:
    """``Epic Name`` is required by DC to CREATE an epic and ``Epic Link`` is the
    field the outbound parent write targets. Both are instance-global, so they are
    verified once here rather than as a mid-run SETUP failure in one cell."""
    _stub_request(harness, monkeypatch, fields=_field_body("Summary", "Epic Name"))

    with pytest.raises(AssertionError) as excinfo:
        harness._assert_project_capabilities("RBTEST")

    assert "Epic Link" in str(excinfo.value), "the abort does not name the missing field"


def test_the_declared_contract_actually_requires_epic(harness) -> None:
    """Guards the constant itself. Every assertion above is satisfiable by quietly
    dropping ``Epic`` from the required set — this is the cell that notices."""
    assert "Epic" in harness._REQUIRED_ISSUE_TYPES
    assert "Sub-task" in harness._REQUIRED_ISSUE_TYPES
    assert "Epic Link" in harness._REQUIRED_FIELDS
    assert "Epic Name" in harness._REQUIRED_FIELDS


# ---------------------------------------------------------------------------
# E2E — the fixture actually WIRES the assertion
# ---------------------------------------------------------------------------


def test_the_project_fixture_aborts_when_the_capability_contract_is_unmet(
    harness, monkeypatch
) -> None:
    """The integration cell. Every test above passes against a contract function
    that exists but is never called; this one drives the fixture itself, so
    'implemented but not wired' is caught here rather than by a live run."""
    _stub_request(harness, monkeypatch, project_body=_project_body("Task", "Sub-task"))

    generator = harness.jira_dc_project.__wrapped__(lambda key: None)
    with pytest.raises(AssertionError) as excinfo:
        next(generator)

    assert "Epic" in str(excinfo.value)


def test_the_project_fixture_yields_a_key_when_the_contract_holds(harness, monkeypatch) -> None:
    """Positive control for the cell above: the abort must be caused by the missing
    capability, not by the fixture being unable to run under a stub at all."""
    _stub_request(harness, monkeypatch)

    generator = harness.jira_dc_project.__wrapped__(lambda key: None)
    assert next(generator) == "RBTEST"
