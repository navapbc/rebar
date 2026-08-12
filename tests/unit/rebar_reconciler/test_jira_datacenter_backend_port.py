"""HELD-OUT port-conformance tests for ``JiraDataCenterBackend`` (story J7, epic e369).

Held out from the implementation subagent: the subagent is given only the happy-path
case, so these edge assertions cannot be satisfied by over-fitting to a shown example.

WHAT THIS PINS, and why it is not test bookkeeping. On merged ``main`` the DC backend
satisfied SEVEN of the ``Backend`` Protocol's nine members — ``query_project`` and
``assert_env_ready`` were absent, so ``isinstance(backend, Backend)`` was ``False`` and
the contract suite's ``test_isinstance_backend_facade`` went red the moment the DC
backend entered its fixture params. Both absences are real defects:

* ``query_project`` is the READ/query scope the inbound fetcher scopes its search to.
  It must NOT substitute a create-time default: an empty value has to stay empty so the
  fetcher FAILS CLOSED rather than querying every project (bug 626d). A "helpful"
  default here silently widens an inbound scan across the whole instance.
* ``assert_env_ready`` is the fail-fast that raises the neutral ``BackendEnvError``
  naming the missing connection essential BEFORE the transport is used in
  bootstrap-band execution, instead of a cryptic downstream failure (ticket 97f2).

Patching note (a trap this repo has hit before): the backend resolves settings through
a FUNCTION-LOCAL import, which binds at CALL time — patching a local alias silently
no-ops and the test passes for the wrong reason. These tests patch the attribute on its
DEFINING module, ``…jira_datacenter.settings.resolve_jira_datacenter_settings``.
"""

from __future__ import annotations

import pytest

from rebar_reconciler._backend import (
    Backend,
    BackendEnvError,
    SupportsComments,
    SupportsLinks,
)
from rebar_reconciler.adapters.jira_datacenter import settings as dc_settings
from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

from .backend_support import FakeTransport


def _settings(**overrides):
    """A fully-populated DC settings tuple, minus whatever the caller blanks out."""
    base = {
        "url": "https://jira.example.internal",
        "project": "DCPROJ",
        "allow_insecure": False,
        "ca_bundle": "",
        "pat": "a-personal-access-token",
    }
    base.update(overrides)
    return dc_settings.JiraDataCenterSettings(**base)


def _backend() -> JiraDataCenterBackend:
    return JiraDataCenterBackend(transport=FakeTransport())


def _pin(monkeypatch, settings) -> None:
    monkeypatch.setattr(
        dc_settings, "resolve_jira_datacenter_settings", lambda: settings, raising=True
    )


# ---------------------------------------------------------------------------
# Port conformance — the facade
# ---------------------------------------------------------------------------


def test_dc_backend_satisfies_the_backend_facade() -> None:
    """The whole point of the story: nine-of-nine, not seven-of-nine."""
    assert isinstance(_backend(), Backend)


def test_dc_backend_advertises_its_capabilities() -> None:
    backend = _backend()
    assert isinstance(backend, SupportsLinks)
    assert isinstance(backend, SupportsComments)


# ---------------------------------------------------------------------------
# query_project — the fail-closed read scope (bug 626d)
# ---------------------------------------------------------------------------


def test_query_project_returns_the_configured_project(monkeypatch) -> None:
    _pin(monkeypatch, _settings(project="DCPROJ"))
    assert _backend().query_project == "DCPROJ"


def test_query_project_stays_empty_when_unset(monkeypatch) -> None:
    """HELD-OUT edge. No create-time default may be substituted here — the fetcher
    fails closed on an empty value rather than querying every project (bug 626d).
    A backend that helpfully defaults this widens an inbound scan instance-wide."""
    _pin(monkeypatch, _settings(project=""))
    assert _backend().query_project == ""


def test_query_project_is_not_the_create_scope_default(monkeypatch) -> None:
    """HELD-OUT edge. ``project`` (write scope) and ``query_project`` (read scope)
    are different contracts; the empty read scope must not borrow the write one."""
    _pin(monkeypatch, _settings(project=""))
    backend = _backend()
    assert backend.query_project == ""
    # the transport-backed WRITE scope is unaffected by the empty read scope
    assert backend.project == "FAKE"


# ---------------------------------------------------------------------------
# assert_env_ready — fail fast, name the missing essential (ticket 97f2)
# ---------------------------------------------------------------------------


def test_assert_env_ready_is_quiet_when_everything_is_present(monkeypatch) -> None:
    _pin(monkeypatch, _settings())
    _backend().assert_env_ready()  # must not raise


@pytest.mark.parametrize(
    ("blank", "expected_token"),
    [({"url": ""}, "url"), ({"pat": ""}, "JIRA_PAT")],
)
def test_assert_env_ready_names_each_missing_essential(
    monkeypatch, blank: dict, expected_token: str
) -> None:
    """HELD-OUT edge. The error must NAME the missing setting — the whole value of
    the fail-fast is that an operator learns which knob is unset, rather than
    meeting a cryptic connection error later."""
    _pin(monkeypatch, _settings(**blank))
    with pytest.raises(BackendEnvError) as exc:
        _backend().assert_env_ready()
    assert expected_token.lower() in str(exc.value).lower()


def test_assert_env_ready_reports_every_missing_essential_at_once(monkeypatch) -> None:
    """HELD-OUT edge. Reporting only the FIRST missing var forces an operator through
    a fix-one-rerun loop; Cloud's equivalent collects them all."""
    _pin(monkeypatch, _settings(url="", pat=""))
    with pytest.raises(BackendEnvError) as exc:
        _backend().assert_env_ready()
    message = str(exc.value).lower()
    assert "url" in message and "jira_pat" in message


def test_assert_env_ready_raises_the_neutral_error_type(monkeypatch) -> None:
    """HELD-OUT edge. ``BackendEnvError`` is the vendor-neutral type the core
    catches; a bare RuntimeError/ValueError would escape that handling. (It
    subclasses RuntimeError, so assert the specific type, not the base.)"""
    _pin(monkeypatch, _settings(pat=""))
    with pytest.raises(BackendEnvError):
        _backend().assert_env_ready()


# ---------------------------------------------------------------------------
# error path — a transport failure PROPAGATES (one behaviour, one assertion)
# ---------------------------------------------------------------------------


class _ExplodingTransport(FakeTransport):
    """A transport whose write side fails, as a real one does on 5xx/timeout."""

    def add_comment(self, remote_id: str, body: str):
        raise RuntimeError("DC transport write failed: 503 Service Unavailable")

    def set_relationship(self, from_id: str, to_id: str, link_type: str = "Blocks"):
        raise RuntimeError("DC transport write failed: connection reset")


def test_a_write_failure_propagates_rather_than_being_swallowed() -> None:
    """HELD-OUT edge. A swallowed write error still lets a pass 'converge', which
    is silent data loss: the local side believes it synced. Propagation only —
    a caught-and-logged variant does NOT satisfy this."""
    backend = JiraDataCenterBackend(transport=_ExplodingTransport())
    with pytest.raises(RuntimeError, match="503"):
        backend.add_comment("DC-1", "body")
    with pytest.raises(RuntimeError, match="connection reset"):
        backend.set_relationship("DC-1", "DC-2")
