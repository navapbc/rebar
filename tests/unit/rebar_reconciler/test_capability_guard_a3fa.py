"""Ticket a3fa: capability call sites are GUARDED at runtime, and a designed skip is
reported distinctly from a real failure.

Four dispatch call sites reach for OPT-IN capability members (`add_comment`,
`set_relationship`) that the base `TicketTransport` port does not declare. They were
resolved with `cast("SupportsComments"/"SupportsLinks", client)` — a STATIC assertion with
no runtime effect — and every one of them sits inside a broad `except Exception`. So a
transport that does not implement the capability raised an `AttributeError` that was
SWALLOWED: the sub-op silently never applied and the pass reported success. That conflated
two states an operator must act on differently:

  * capability ABSENT  — designed, fine to skip (a second backend may legitimately not
    implement the Protocol). Expected signal: an INFO log + a durable `bridge_alerts`
    record under `outbound-<capability>-capability-absent`.
  * capability PRESENT but the call RAISED — a real failure. Expected signal: the
    pre-existing warning/stderr with the real exception, plus the in-band `comment_errors`
    capture on the comment paths.

These tests pin the split at all four sites, and pin that a designed skip does NOT trip the
failure detectors (`comment_errors`, and the `*_computed>0 / *_applied==0` silent-no-op
canary in `apply_handlers`, which is a FAILURE detector).

MUTATION CHECK (AC5): deleting a guard turns these RED on the alert-kind assertion — the
capability-absent record is never written — rather than on an incidental `AttributeError`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from rebar_reconciler import dispatch_apply_phases, dispatch_one, pass_io

# The guard logs through the module that owns it. Target that logger by NAME when asserting:
# a sibling test in the same session can leave the module logger above INFO, and
# ``caplog.at_level(INFO)`` alone only adjusts the ROOT logger, so the record would be
# filtered before it ever reaches caplog.
_GUARD_LOGGER = "rebar_reconciler.dispatch_apply_phases"


@pytest.fixture(autouse=True)
def _reset_capability_dedupe():
    """`record_capability_gap` dedupes per process per (capability, site) so a permanently
    non-capable backend cannot append one record per sub-op per pass, unbounded. Tests must
    start from a clean set or the second test would see its record suppressed."""
    pass_io._CAPABILITY_GAPS_SEEN.clear()
    yield
    pass_io._CAPABILITY_GAPS_SEEN.clear()


@pytest.fixture
def alert_root(tmp_path, monkeypatch):
    """Point the bridge alert store at a temp repo root."""
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    return tmp_path


def _alerts(root: Path) -> list[dict]:
    store = root / "bridge_state" / "bridge_alerts"
    if not store.exists():
        return []
    out: list[dict] = []
    for f in sorted(store.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _kinds(root: Path) -> list[str]:
    return [a.get("kind") for a in _alerts(root)]


class _NoCapabilityTransport:
    """A transport implementing the BASE port only.

    Deliberately NOT a MagicMock: a MagicMock auto-creates every attribute, so it SATISFIES
    a runtime-checkable Protocol and would never exercise the absent branch. This is what a
    second backend that does not implement the capability Protocols actually looks like.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def search_issues(self, *_a, **_k):
        return []

    def create_issue(self, *_a, **_k):
        self.calls.append("create_issue")
        return {"key": "DIG-1"}

    def update_issue(self, key, **_fields):
        self.calls.append("update_issue")
        return {"key": key, "ok": True}

    def get_issue_links(self, _key):
        return []

    def add_label(self, _key, _label):
        self.calls.append("add_label")
        return {}

    def set_entity_property(self, *_a, **_k):
        self.calls.append("set_entity_property")
        return {}


class _RaisingCommentTransport(_NoCapabilityTransport):
    """HAS the comments capability, but the call fails — a REAL failure, not a skip."""

    def add_comment(self, _remote_id, _body):
        raise RuntimeError("jira exploded")

    def get_comment_map(self, _project_key):
        return {}


class _RaisingLinkTransport(_NoCapabilityTransport):
    """HAS the links capability, but the call fails — a REAL failure, not a skip."""

    def set_relationship(self, _f, _t, _type="Blocks"):
        raise RuntimeError("jira exploded")

    def get_issuelinks_map(self, _project_key):
        return {}

    def map_remote_links(self, _remote_fields):
        return []

    def link_payload_for_relation(self, _relation):
        return ("Blocks", False)


# --------------------------------------------------------------------------------------
# Capability ABSENT — a designed skip: logged, alerted, and NOT counted as a failure
# --------------------------------------------------------------------------------------


def test_dispatch_comments_absent_capability_is_a_designed_skip(alert_root, caplog):
    """`_update_one_dispatch_comments` (the dispatch_apply_phases site)."""
    client = _NoCapabilityTransport()
    mutation = {"key": "DIG-2", "comments": [{"body": "hello"}, {"body": "again"}]}
    comment_errors: list[str] = []

    with caplog.at_level(logging.INFO, logger=_GUARD_LOGGER):
        computed, applied = dispatch_apply_phases._update_one_dispatch_comments(
            mutation, client, "DIG-2", comment_errors
        )

    assert "outbound-comments-capability-absent" in _kinds(alert_root), (
        "a capability-absent skip must be recorded on the EXISTING bridge_alerts channel "
        "under its own kind — without it the skip is invisible, which is the whole bug"
    )
    assert any("does not implement comments" in r.getMessage() for r in caplog.records), (
        "the skip must name the missing capability in an INFO log"
    )
    # AC2b: the silent-no-op canary is a FAILURE detector — a DESIGNED skip must not feed it.
    assert (computed, applied) == (0, 0), (
        "a capability-absent skip must increment NEITHER _computed NOR _applied, or "
        "apply_handlers' silent-no-op canary would report a designed skip as the bug-3f04 "
        "failure mode (and hard-fail the mutation under RECONCILER_FAIL_SILENT_NOOP=1)"
    )
    # AC3b: comment_errors is the FAILURE channel — a skip must not appear there.
    assert comment_errors == [], "a designed skip must not be recorded as a comment failure"


def test_dispatch_links_absent_capability_is_a_designed_skip(alert_root, caplog):
    """`_update_one_dispatch_links` (the set_relationship site)."""
    client = _NoCapabilityTransport()
    mutation = {
        "key": "DIG-3",
        "links": [{"action": "add", "type": "Blocks", "to_key": "DIG-9"}],
    }

    with caplog.at_level(logging.INFO, logger=_GUARD_LOGGER):
        computed, applied = dispatch_one._update_one_dispatch_links(mutation, client, "DIG-3")

    assert "outbound-links-capability-absent" in _kinds(alert_root)
    assert any("does not implement links" in r.getMessage() for r in caplog.records)
    assert (computed, applied) == (0, 0), (
        "a capability-absent link skip must not feed the silent-no-op canary"
    )


def test_create_one_absent_capability_is_a_designed_skip(alert_root, tmp_path, caplog):
    """`create_one` — no local computed/applied counter here, so the log + alert ARE the
    whole signal, and the create itself must still succeed."""
    client = _NoCapabilityTransport()
    mutation = {
        "local_id": "cap-create",
        "action": "create",
        "fields": {"summary": "s", "issuetype": {"name": "Task"}},
        "comments": [{"body": "hello"}],
    }
    comment_errors: list[str] = []

    with caplog.at_level(logging.INFO, logger=_GUARD_LOGGER):
        result = dispatch_one.create_one(
            mutation, client, repo_root=tmp_path, comment_errors=comment_errors
        )

    assert result == {"key": "DIG-1"}, "the create must still land; only the comment is skipped"
    assert "outbound-comments-capability-absent" in _kinds(alert_root)
    assert comment_errors == [], "a designed skip is not a comment failure"


def test_absent_capability_alert_is_deduped_per_site(alert_root):
    """A permanently non-capable backend must not append one record per sub-op per pass."""
    client = _NoCapabilityTransport()
    mutation = {"key": "DIG-4", "comments": [{"body": f"c{i}"} for i in range(25)]}

    for _ in range(3):
        dispatch_apply_phases._update_one_dispatch_comments(mutation, client, "DIG-4", [])

    absent = [k for k in _kinds(alert_root) if k == "outbound-comments-capability-absent"]
    assert len(absent) == 1, (
        f"expected the capability gap recorded ONCE per (capability, site); got {len(absent)}"
    )


# --------------------------------------------------------------------------------------
# Capability PRESENT but raising — a REAL failure, reported as before and never conflated
# --------------------------------------------------------------------------------------


def test_present_but_raising_comment_is_a_failure_not_a_skip(alert_root):
    client = _RaisingCommentTransport()
    mutation = {"key": "DIG-5", "comments": [{"body": "hello"}]}
    comment_errors: list[str] = []

    computed, applied = dispatch_apply_phases._update_one_dispatch_comments(
        mutation, client, "DIG-5", comment_errors
    )

    assert len(comment_errors) == 1 and "add_comment failed" in comment_errors[0], (
        "a real exception must still surface in-band with the exception text"
    )
    assert "jira exploded" in comment_errors[0], "the REAL exception must be surfaced"
    assert (computed, applied) == (1, 0), (
        "a real failure MUST still feed the silent-no-op canary — that detector stays live"
    )
    assert "outbound-comments-capability-absent" not in _kinds(alert_root), (
        "a failure must never be reported as a designed capability skip"
    )


def test_present_but_raising_link_is_a_failure_not_a_skip(alert_root):
    client = _RaisingLinkTransport()
    mutation = {
        "key": "DIG-6",
        "links": [{"action": "add", "type": "Blocks", "to_key": "DIG-9"}],
    }

    computed, applied = dispatch_one._update_one_dispatch_links(mutation, client, "DIG-6")

    assert (computed, applied) == (1, 0), (
        "a real link failure MUST still feed the silent-no-op canary"
    )
    assert "outbound-links-capability-absent" not in _kinds(alert_root)


# --------------------------------------------------------------------------------------
# The getattr_static hazard: a PROXYING transport is capable and must not be skipped
# --------------------------------------------------------------------------------------


class _ProxyTransport:
    """A transport that FORWARDS to an inner client through ``__getattr__``.

    This shape is why the guard cannot trust ``isinstance`` alone. Since Python 3.12
    (gh-102433) a ``@runtime_checkable`` ``isinstance`` resolves members with
    ``inspect.getattr_static``, which deliberately does NOT see attributes served by
    ``__getattr__``. So this object HAS a working ``add_comment`` and yet fails
    ``isinstance(self, SupportsComments)``. Every ``MagicMock`` in the suite is the same
    shape, but the production hazard is the real one: a decorator/wrapper transport would be
    declared "capability absent" and its writes skipped BY DESIGN — reinstating the very
    silent-skip defect this ticket removes, only now blessed and alerted as intentional.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.proxied: list[str] = []

    def __getattr__(self, name):
        self.proxied.append(name)
        return getattr(self._inner, name)


class _FullCommentTransport(_NoCapabilityTransport):
    def __init__(self) -> None:
        super().__init__()
        self.comments: list[tuple[str, str]] = []

    def add_comment(self, remote_id, body):
        self.comments.append((remote_id, body))
        return {}

    def get_comment_map(self, _project_key):
        return {}


def test_proxying_transport_is_capable_and_is_not_skipped(alert_root):
    """REGRESSION: a __getattr__-proxying transport must NOT be reported capability-absent."""
    from rebar_reconciler._backend import SupportsComments

    inner = _FullCommentTransport()
    client = _ProxyTransport(inner)

    # The hazard itself, pinned: the proxy fails isinstance despite having the member.
    assert not isinstance(client, SupportsComments), (
        "if this ever becomes True, Python's runtime-protocol semantics changed; the "
        "hasattr fallback in _capability_present may then be redundant"
    )
    assert hasattr(client, "add_comment")

    mutation = {"key": "DIG-7", "comments": [{"body": "hello"}]}
    comment_errors: list[str] = []
    computed, applied = dispatch_apply_phases._update_one_dispatch_comments(
        mutation, client, "DIG-7", comment_errors
    )

    assert inner.comments == [("DIG-7", "hello")], (
        "the proxied add_comment must actually be CALLED — an isinstance-only guard would "
        "have skipped this write and called the skip 'designed'"
    )
    assert (computed, applied) == (1, 1)
    assert comment_errors == []
    assert "outbound-comments-capability-absent" not in _kinds(alert_root), (
        "a capable transport must never produce a capability-absent alert"
    )


# --------------------------------------------------------------------------------------
# The static enforcement cc77 added must survive (AC6)
# --------------------------------------------------------------------------------------


def test_capability_protocols_are_runtime_checkable():
    """The guard shape ADR-0083 prescribes only works if the Protocols are runtime-checkable."""
    from rebar_reconciler._backend import SupportsComments, SupportsLinks

    assert isinstance(_RaisingCommentTransport(), SupportsComments)
    assert not isinstance(_NoCapabilityTransport(), SupportsComments)
    assert isinstance(_RaisingLinkTransport(), SupportsLinks)
    assert not isinstance(_NoCapabilityTransport(), SupportsLinks)
