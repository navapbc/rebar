"""Backend-port contract suite — happy path (S2, epic bbf1).

Backend-agnostic contract tests that MUST hold for every backend. Run against both
the in-memory ``FakeBackend`` and the real ``JiraBackend`` (constructed with an
injected fake transport). These assert the *generic* port contract — the exact
Jira-specific values live in the held-out characterization tests, so nothing here
can be satisfied by hard-coding Jira outputs.

The engine dir is on ``sys.path`` via the package conftest (flat imports).
"""

from __future__ import annotations

import pytest

from rebar_reconciler._backend import Backend, FieldSanitizer, RemoteRef
from rebar_reconciler.adapters.jira.backend import JiraBackend

from .backend_support import FakeBackend, FakeTransport


@pytest.fixture(params=["fake", "jira"])
def backend(request):
    """One live backend per param — both drive the same contract assertions."""
    if request.param == "fake":
        return FakeBackend()
    return JiraBackend(transport=FakeTransport())


def test_backend_exposes_the_five_role_protocols(backend):
    # Every backend is one object exposing the five roles + a vendor name.
    assert isinstance(backend.vendor, str) and backend.vendor
    assert backend.transport is not None
    assert backend.outbound is not None
    assert backend.inbound is not None
    assert backend.sanitizer is not None
    assert backend.identity is not None


def test_isinstance_backend_facade(backend):
    # The Backend facade is runtime-checkable; a well-formed backend satisfies it.
    assert isinstance(backend, Backend)


def test_outbound_carries_the_synced_local_fields(backend):
    # A local ticket maps to a remote field dict that carries the fields the
    # reconciler syncs. (Exact value maps are Jira-specific → characterization.)
    ticket = {
        "ticket_id": "abc1-2345-6789-0abc",
        "title": "Add widget",
        "description": "Body text",
        "ticket_type": "story",
        "priority": 1,
        "status": "in_progress",
        "assignee": "me@example.com",
    }
    remote = backend.outbound.map_local_to_remote(ticket)
    assert isinstance(remote, dict)
    assert remote["summary"] == "Add widget"
    assert remote["description"] == "Body text"
    # priority + status are present (mapped to the backend's own value space).
    assert "priority" in remote
    assert "status" in remote


def test_identity_label_round_trips_the_local_id(backend):
    # The identity convention stamps a local id into a back-pointer label and
    # recovers it — the round-trip property, backend-agnostic.
    local_id = "abc1-2345-6789-0abc"
    label = backend.identity.format_label(local_id)
    assert isinstance(label, str) and local_id in label
    assert backend.identity.is_identity_label(label) is True
    assert backend.identity.parse_label(label) == local_id


def test_sanitizer_is_idempotent_on_a_valid_value(backend):
    # Sanitizing an already-valid short value twice yields the same result once.
    sanitizer: FieldSanitizer = backend.sanitizer
    once = sanitizer.sanitize_summary("Short title")
    twice = sanitizer.sanitize_summary(once)
    assert once == twice == "Short title"


def test_jira_backend_transport_is_the_injected_one():
    # JiraBackend takes its transport by injection (the seam S4 patches).
    t = FakeTransport()
    backend = JiraBackend(transport=t)
    assert backend.transport is t
    assert backend.vendor == "jira"


def test_remote_ref_is_value_equal():
    a = RemoteRef(vendor="jira", instance="dig", remote_id="DIG-1234")
    b = RemoteRef(vendor="jira", instance="dig", remote_id="DIG-1234")
    assert a == b and hash(a) == hash(b)
