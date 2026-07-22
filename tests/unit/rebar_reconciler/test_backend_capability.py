"""HELD-OUT capability-detection contract (S2, epic bbf1).

Capabilities are opt-in and detected BEHAVIOURALLY via ``isinstance`` against the
runtime-checkable capability Protocols — never by structural introspection of method
names. A backend that does not advertise ``SupportsLinks`` is never asked to sync
links; one that does is asked. ``FakeBackend`` deliberately implements neither
capability; ``JiraBackend`` implements links + comments (Jira supports both).

Held out from the implementation subagent.
"""

from __future__ import annotations

from rebar_reconciler._backend import (
    SupportsComments,
    SupportsIncremental,
    SupportsLinks,
)
from rebar_reconciler.adapters.jira.backend import JiraBackend

from .backend_support import FakeBackend, FakeTransport


def _sync_links_if_supported(backend, from_id, to_id) -> bool:
    """A capability-gated driver (the shape core uses in S4). Returns True iff it
    actually enacted a link — i.e. iff the backend advertised SupportsLinks."""
    if isinstance(backend, SupportsLinks):
        backend.set_relationship(from_id, to_id, "Blocks")
        return True
    return False


def test_fake_backend_does_not_advertise_links():
    assert not isinstance(FakeBackend(), SupportsLinks)


def test_jira_backend_advertises_links_and_comments():
    jb = JiraBackend(transport=FakeTransport())
    assert isinstance(jb, SupportsLinks)
    assert isinstance(jb, SupportsComments)


def test_link_sync_is_skipped_for_a_backend_without_the_capability():
    fake = FakeBackend()
    enacted = _sync_links_if_supported(fake, "A", "B")
    assert enacted is False
    # observed via behaviour: the transport never saw a set_relationship call
    assert not any(c[0] == "set_relationship" for c in fake.transport.calls)


def test_link_sync_runs_for_a_backend_with_the_capability():
    t = FakeTransport()
    jb = JiraBackend(transport=t)
    enacted = _sync_links_if_supported(jb, "DIG-1", "DIG-2")
    assert enacted is True
    # JiraBackend's SupportsLinks delegates to the transport.
    assert any(c[0] == "set_relationship" for c in t.calls)


def test_comment_capability_delegates_to_transport():
    t = FakeTransport()
    jb = JiraBackend(transport=t)
    assert isinstance(jb, SupportsComments)
    jb.add_comment("DIG-1", "hello")
    assert any(c[0] == "add_comment" for c in t.calls)


def test_backends_without_incremental_are_detected():
    assert not isinstance(FakeBackend(), SupportsIncremental)
