"""Live coverage for the Data Center transport against the J5 harness (story J6,
epic e369).

Drives the REAL ``JiraDataCenterTransport`` (built on ``pycontribs/jira``) against
a real Jira 8.17.1 Data Center instance, asserting the SAME raw-shape contract the
unit tier asserts against a fake client (``tests/_jira_shape_contract.py`` — see
the execution-decision comment on ticket 9fd4-a94c-156e-4a56): if the DC transport
ever leaked a ``jira.Issue`` instead of a raw dict, both tiers would fail on a
shared assertion rather than a test-specific one.

Tier notes (inherited from ``tests/external/`` — see
``tests/external/live_jira_dc/test_harness_smoke.py``'s module docstring for the
full rationale, reproduced here only where it differs):

* the module-level ``_live_jira_ready`` sentinel below is what makes
  ``tests/external/conftest.py`` attach the ``jira_live`` marker and enrol this
  module in the all-skip canary;
* absent harness ⇒ SKIP with an actionable message, never a hard failure — the
  ``external`` CI job runs with no Docker and no Jira at all. A missing
  ``[jira-datacenter]`` extra is a skip ONLY when the harness is absent too; when
  the harness IS reachable, a missing extra is a LOUD FAILURE, because in that
  environment this module is the acceptance evidence for the DC transport and a
  skip would let the job report green having validated nothing (the all-skip canary
  cannot catch it — ``test_harness_smoke.py``'s tests execute in the same session
  and mask it);
* every test here sets ``allow_insecure=true`` explicitly (the harness serves
  plain ``http://localhost:2990/jira``), so the loopback path exercises the
  config's TLS-override branch rather than bypassing the validator (epic AC13).
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any

import pytest
from _jira_shape_contract import assert_comment_map_shape, assert_search_shape

_BASE = os.environ.get("JIRA_DC_BASE_URL", "http://localhost:2990/jira")
# Mirrors conftest.py's own constant: the harness image's built-in admin account,
# which is the one user guaranteed to exist for the live user-search assertions.
_ADMIN_USER = os.environ.get("JIRA_DC_ADMIN", "admin")


def _live_jira_ready() -> bool:
    """The sentinel ``tests/external/conftest.py`` keys on to apply ``jira_live``
    (enrolling this module in the all-skip canary) — also the readiness predicate
    for the ``skipif`` below."""
    try:
        req = urllib.request.Request(f"{_BASE.rstrip('/')}/rest/api/2/serverInfo")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _jira_extra_installed() -> bool:
    try:
        import jira  # noqa: F401
    except ImportError:
        return False
    return True


_skip = pytest.mark.skipif(
    not _live_jira_ready(),
    reason=(
        "Jira DC harness not reachable at "
        f"{_BASE} — start it with `make jira-dc-up` and run with REBAR_RUN_EXTERNAL=1"
    ),
)
# A missing extra is a legitimate SKIP only when there is no harness to test
# against either (a plain dev checkout). When the harness IS reachable, this module
# is the acceptance evidence for the DC transport, and skipping it would let a
# green run certify code that never executed — the CI job installs the extra
# (external-integration.yml), so its absence here is a broken environment, not a
# tier that does not apply. The all-skip canary cannot catch this on its own:
# it counts collected-vs-executed globally per session, and test_harness_smoke.py's
# tests DO execute in the same job, masking an all-skip of this module.
_extra_missing_but_harness_up = _live_jira_ready() and not _jira_extra_installed()

_skip_no_extra = pytest.mark.skipif(
    not _jira_extra_installed() and not _extra_missing_but_harness_up,
    reason="the 'jira-datacenter' extra (pycontribs/jira) is not installed — "
    "pip install 'nava-rebar[jira-datacenter]'",
)


@pytest.fixture(autouse=True)
def _fail_if_extra_missing_while_harness_is_up() -> None:
    """Turn "harness reachable but extra absent" into a LOUD failure.

    Without this, that combination silently skips every test below and the job
    reports green — the exact false-negative the external tier exists to prevent.
    """
    if _extra_missing_but_harness_up:
        pytest.fail(
            "the Jira DC harness is reachable at "
            f"{_BASE} but the 'jira-datacenter' extra (pycontribs/jira) is NOT "
            "installed, so the DC transport tests would silently skip and this run "
            "would report green having validated nothing. Install it with: "
            "pip install -e '.[dev,jira-datacenter]'"
        )


@pytest.fixture
def dc_transport(jira_dc_pat: str) -> Any:
    """A REAL ``JiraDataCenterTransport`` against the live harness.

    Builds the client directly from harness fixtures (rather than through
    ``load_config()``) so this test suite does not depend on process-wide config
    discovery — ``allow_insecure=True`` mirrors what a ``[tool.rebar.reconciler]``
    config pointed at this loopback harness would need, exercised here as a
    direct constructor argument instead.
    """
    from rebar_reconciler.adapters.jira_datacenter.settings import JiraDataCenterSettings
    from rebar_reconciler.adapters.jira_datacenter.transport import (
        JiraDataCenterTransport,
        build_client_from_settings,
    )

    settings = JiraDataCenterSettings(
        url=_BASE,
        project="",  # overridden per-test via jira_dc_project
        allow_insecure=True,
        ca_bundle="",
        resolved_statuses=frozenset({"Resolved", "Done", "Cancelled"}),
        pat=jira_dc_pat,
    )
    client = build_client_from_settings(settings)
    return JiraDataCenterTransport(client=client, project="")


@_skip
@_skip_no_extra
def test_create_get_update_transition_roundtrip(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """create -> read -> update -> transition-by-name, each asserted against the
    raw shape contract and its observable postcondition on the server."""
    dc_transport.project = jira_dc_project

    created = dc_transport.create_issue(
        {"summary": "rebar J6 live — roundtrip", "issuetype": "Task"}
    )
    assert isinstance(created, dict)
    key = created["key"]
    track_issue(key)

    fetched = dc_transport.get_issue(key)
    assert isinstance(fetched, dict)
    assert fetched["key"] == key
    assert fetched["fields"]["summary"] == "rebar J6 live — roundtrip"
    assert isinstance(fetched["fields"].get("description"), (str, type(None))), (
        "DC descriptions must be plain text, never an ADF dict"
    )

    updated = dc_transport.update_issue(key, summary="rebar J6 live — updated")
    assert updated["fields"]["summary"] == "rebar J6 live — updated"

    # A transition's NAME is not its destination STATUS name: Jira's classic
    # workflow offers "Start Progress" -> status "In Progress", "Resolve Issue" ->
    # "Resolved". Asserting the status equals the transition name therefore fails
    # against a real instance. The transitions payload declares the destination in
    # `to.name`, so drive by name (the transport's contract) and assert the
    # postcondition against the destination the server itself declared.
    transitions = dc_transport._client.transitions(key)
    target = next(
        (
            t
            for t in transitions
            if isinstance(t, dict)
            and t.get("name")
            and isinstance(t.get("to"), dict)
            and t["to"].get("name")
        ),
        None,
    )
    assert target is not None, (
        f"no transition declaring a destination status is available for {key}; got {transitions!r}"
    )
    dc_transport.transition_issue_by_name(key, target["name"])
    after = dc_transport.get_issue(key)
    assert after["fields"]["status"]["name"] == target["to"]["name"]


@_skip
@_skip_no_extra
def test_transition_to_an_unavailable_name_raises(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """A transition name the workflow does not offer raises rather than silently
    no-oping."""
    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue(
        {"summary": "rebar J6 live — bad transition", "issuetype": "Task"}
    )
    key = created["key"]
    track_issue(key)

    with pytest.raises(ValueError):
        dc_transport.transition_issue_by_name(key, "definitely-not-a-real-status-name")


@_skip
@_skip_no_extra
def test_comment_and_search_shapes_match_the_shared_contract(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue({"summary": "rebar J6 live — comment", "issuetype": "Task"})
    key = created["key"]
    track_issue(key)

    comment = dc_transport.add_comment(key, "a live comment")
    assert isinstance(comment, dict)

    assert_search_shape(dc_transport.search_issues(f"project = {jira_dc_project}"))
    assert_comment_map_shape(dc_transport.get_comment_map(jira_dc_project))


@_skip
@_skip_no_extra
def test_probe_remote_classifies_a_deleted_issue_as_archived_or_moved(
    dc_transport: Any, jira_dc_project: str
) -> None:
    """Absence-probe edge case: a deleted issue classifies as ARCHIVED_OR_MOVED,
    not merely "some error"."""
    from rebar_reconciler.inbound_probe import ProbeBranch

    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue(
        {"summary": "rebar J6 live — to delete", "issuetype": "Task"}
    )
    key = created["key"]
    dc_transport._client.issue(key).delete()

    result = dc_transport.probe_remote(key)
    assert result.branch == ProbeBranch.ARCHIVED_OR_MOVED


@_skip
@_skip_no_extra
def test_name_identity_user_search_resolves_a_real_user_authoritatively(
    dc_transport: Any,
) -> None:
    """The live half of the ``NameIdentity`` wire J4 left dangling.

    J4 shipped ``NameIdentity`` taking its resolver as an EXPLICIT constructor
    parameter so this story could supply a REAL lookup; a resolver that is absent
    (or silently non-authoritative) makes the outbound diff re-emit an assignee
    change it can never converge — the churn class J4's anti-churn oracle exists
    to prevent. Asserting it against a fake would prove nothing about DC's
    ``user/search`` endpoint, which is why this lives in the live tier.
    """
    from rebar_reconciler.adapters.jira_datacenter.backend import _search_users_by_username
    from rebar_reconciler.adapters.jira_family.identity_model import NameIdentity

    resolved, authoritative, is_account_id = _search_users_by_username(
        dc_transport._client, _ADMIN_USER
    )
    assert resolved == _ADMIN_USER
    assert authoritative is True, (
        "the live user search IS the authoritative path — a False here is the "
        "permanently-non-authoritative assignee that causes unconvergeable churn"
    )
    assert is_account_id is False, "Data Center has no accountId concept at all"

    # …and the same lookup driving the real identity model: a resolved-but-
    # mismatched DC name emits the freshly resolved username (DC's `name` IS the
    # identity, so `trust_resolved_on_mismatch` is True for NameIdentity).
    model = NameIdentity(resolver=lambda n: _search_users_by_username(dc_transport._client, n))
    assert model.resolve(_ADMIN_USER, {"name": "somebody-else"}) == (_ADMIN_USER, True, False)
    # Converged: the resolved value already matches the remote identity.
    assert model.resolve(_ADMIN_USER, {"name": _ADMIN_USER}) == (None, True, False)


@_skip
@_skip_no_extra
def test_assigning_an_unknown_user_raises_backend_assignee_not_found(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """An assignee that resolves to no DC user surfaces as the VENDOR-NEUTRAL
    ``BackendAssigneeNotFoundError``, not a bare ``JIRAError``.

    This is the other half of the AC. Note the deliberate division of labour,
    confirmed live here rather than assumed:

    * the RESOLVER reports an unknown user as ``(None, True, False)`` — the
      "authoritative but unmappable" state, which ``NameIdentity``/``_resolve``
      maps to ``("", True, False)`` (desired-unassigned). It does not raise,
      because raising inside the resolver would break that state machine;
    * the APPLY path (``transport._assign``) is where an unknown user becomes an
      error, raised as ``BackendAssigneeNotFoundError`` so core ``except``
      clauses catch it without importing anything DC-specific.
    """
    from rebar_reconciler._backend import BackendAssigneeNotFoundError
    from rebar_reconciler.adapters.jira_datacenter.backend import _search_users_by_username

    unknown = "definitely-not-a-real-dc-user-9fd4"

    resolved, authoritative, is_account_id = _search_users_by_username(
        dc_transport._client, unknown
    )
    assert (resolved, authoritative, is_account_id) == (None, True, False)

    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue(
        {"summary": "rebar J6 live — unknown assignee", "issuetype": "Task"}
    )
    key = created["key"]
    track_issue(key)

    with pytest.raises(BackendAssigneeNotFoundError):
        dc_transport.update_issue(key, assignee=unknown)


@_skip
@_skip_no_extra
def test_select_backend_resolves_after_importing_adapters() -> None:
    """``select_backend("jira-datacenter")`` resolves once
    ``rebar_reconciler.adapters`` is imported — pinning the self-registration
    import (``adapters/__init__.py``) rather than a transport-only test that
    would never catch a missing registration."""
    import rebar_reconciler.adapters  # noqa: F401 — registers the DC factory
    from rebar_reconciler._backend_registry import _REGISTRY

    assert "jira-datacenter" in _REGISTRY
