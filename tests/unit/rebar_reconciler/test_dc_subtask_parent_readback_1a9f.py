"""Verify Data Center sub-task parent writes by reading the field back (bug 1a9f).

DC 8.17.1 can return 204 while ignoring ``fields.parent``. Since dispatch softens the
transport failure, status alone creates silent success; the direct readback must show the
requested parent. An unchanged value raises ``NotImplementedError`` so dispatch classifies
the relation as unrepresentable rather than retryable—repeating an accepted-but-ignored
write cannot help. Readback preserves compatibility if a future DC version honors the
operation instead of hardcoding a version-wide decline.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport


class _FakeIssue:
    def __init__(self, raw: dict[str, Any], on_update: Any) -> None:
        self.raw = raw
        self._on_update = on_update

    def update(self, fields: dict[str, Any]) -> None:
        self._on_update(fields)


class _FakeClient:
    """A ``jira.JIRA``-shaped double that can model DC's accept-and-ignore honestly.

    ``applies_writes=False`` IS the bug: ``update`` is accepted, records the call, and leaves the
    stored parent untouched — exactly what DC 8.17.1 does. ``applies_writes=True`` is a
    hypothetical well-behaved instance, and exists so the positive control is not vacuous.

    The stored parent is rendered into every ``issue()`` response, so a read-back sees current
    state rather than a fixed snapshot. Without that, a read-back-based fix could not be
    distinguished from one that never re-read anything.
    """

    def __init__(
        self,
        *,
        parent_key: str | None = "RBJ-1",
        applies_writes: bool = False,
        subtask: bool = True,
        update_raises: Exception | None = None,
    ) -> None:
        self.parent_key = parent_key
        self.applies_writes = applies_writes
        self.updates: list[dict[str, Any]] = []
        self.issue_calls: list[str] = []
        self._subtask = subtask
        self._update_raises = update_raises

    def issue(self, remote_id: str) -> _FakeIssue:
        self.issue_calls.append(remote_id)
        fields: dict[str, Any] = {"issuetype": {"subtask": self._subtask}}
        if self.parent_key is not None:
            # Shaped like the real payload — a nested object with more than the key, so a
            # comparison that comes down to "is this the string I wrote" fails here.
            fields["parent"] = {
                "id": "10029",
                "key": self.parent_key,
                "self": f"http://localhost:2990/jira/rest/api/2/issue/{self.parent_key}",
                "fields": {"summary": "the parent"},
            }
        return _FakeIssue({"key": remote_id, "fields": fields}, self._apply)

    def _apply(self, fields: dict[str, Any]) -> None:
        if self._update_raises is not None:
            raise self._update_raises
        self.updates.append(fields)
        if not self.applies_writes:
            return  # DC's accept-and-ignore: 204, no change.
        parent = fields.get("parent")
        self.parent_key = parent["key"] if isinstance(parent, dict) else None

    def fields(self) -> list[dict[str, Any]]:
        return [
            {"id": "summary", "name": "Summary", "custom": False},
            {"id": "customfield_10014", "name": "Epic Link", "custom": True},
        ]


def _transport(client: _FakeClient) -> JiraDataCenterTransport:
    return JiraDataCenterTransport(client=client, project="RBJ")


# ---------------------------------------------------------------------------
# HAPPY PATH — a write that lands is accepted, and the read-back happens at all
# ---------------------------------------------------------------------------


def test_a_subtask_reparent_that_takes_effect_is_accepted() -> None:
    """The positive control. An instance that honours the write must not raise.

    Without this cell, a fix that raises unconditionally — or that declines the sub-task branch
    outright — would satisfy every negative cell below.
    """
    client = _FakeClient(parent_key="RBJ-1", applies_writes=True)

    _transport(client).set_parent("RBJ-3", "RBJ-2")

    assert client.parent_key == "RBJ-2"
    assert {"parent": {"key": "RBJ-2"}} in client.updates, (
        f"the sub-task branch did not write fields.parent: {client.updates!r}"
    )


def test_the_subtask_branch_reads_the_parent_back_after_writing() -> None:
    """The write must be followed by a fresh read of the issue.

    Asserted as a second ``issue()`` round trip. A fix that trusts the 204 makes exactly one
    call, so this is the cheapest direct evidence that verification is happening at all rather
    than being inferred from the absence of an exception.
    """
    client = _FakeClient(parent_key="RBJ-1", applies_writes=True)

    _transport(client).set_parent("RBJ-3", "RBJ-2")

    assert len(client.issue_calls) >= 2, (
        "set_parent issued no read-back: it called client.issue() "
        f"{len(client.issue_calls)} time(s) ({client.issue_calls!r}), so it is trusting the "
        "status code — which on Data Center is a lie for this field"
    )


# ---------------------------------------------------------------------------
# THE DEFECT — held out from the implementer
# ---------------------------------------------------------------------------


def test_an_ignored_subtask_reparent_raises_instead_of_reporting_success() -> None:
    """THE BUG. DC returns 204 and leaves the parent alone; that must not read as success.

    This is the whole ticket. Pre-fix ``set_parent`` returns normally here and the pass reports a
    mutation that never happened.
    """
    client = _FakeClient(parent_key="RBJ-1", applies_writes=False)

    with pytest.raises(Exception) as excinfo:
        _transport(client).set_parent("RBJ-3", "RBJ-2")

    message = str(excinfo.value)
    assert "RBJ-2" in message, "the error does not name the parent that was REQUESTED"
    assert "RBJ-1" in message, (
        "the error does not name the parent still OBSERVED on the issue, so an operator cannot "
        "tell an ignored write from a rejected one"
    )


def test_the_ignored_write_is_classified_unrepresentable_not_retryable() -> None:
    """The classification is load-bearing, and it is the easy thing to get wrong.

    ``dispatch_one`` maps ``NotImplementedError`` to ``outbound-parent-unrepresentable`` and
    everything else to the RETRYABLE ``outbound-parent-failed``. DC ignores this write by design,
    so every retry will be ignored identically — a retryable classification would spin against a
    platform that is not going to change its mind.
    """
    client = _FakeClient(parent_key="RBJ-1", applies_writes=False)

    with pytest.raises(NotImplementedError):
        _transport(client).set_parent("RBJ-3", "RBJ-2")


def test_an_ignored_subtask_parent_CLEAR_also_raises() -> None:
    """A clear routes through the same call with a falsy key, and DC ignores it the same way.

    Verifying only the SET would leave the detach path reporting success while the sub-task keeps
    its parent — which is precisely what the live outbound-clear cell observed.
    """
    client = _FakeClient(parent_key="RBJ-1", applies_writes=False)

    with pytest.raises(NotImplementedError) as excinfo:
        _transport(client).set_parent("RBJ-3", None)

    assert "RBJ-1" in str(excinfo.value), (
        "the clear's error does not name the parent that is still attached"
    )


def test_a_SET_whose_readback_shows_NO_parent_at_all_still_raises() -> None:
    """An absent ``parent`` means "no parent", not "we could not tell".

    This cell exists because the first implementation treated a read-back that did not CARRY the
    field as inconclusive and returned — reasoning that a fields-limited projection and a
    genuinely parentless issue look identical. That is true in general but not at THIS call site:
    the read-back fetches the whole issue with no ``fields=`` projection, so the key is present
    whenever a parent is. Letting the absence pass reintroduces exactly the silent success this
    method exists to remove, and it would be reachable the moment a Jira variant permitted a
    parentless sub-task. A SET that produced no parent is a FAILED set.
    """
    client = _FakeClient(parent_key=None, applies_writes=False)

    with pytest.raises(NotImplementedError) as excinfo:
        _transport(client).set_parent("RBJ-3", "RBJ-2")

    assert "RBJ-2" in str(excinfo.value), "the error does not name the parent that was requested"


def test_a_CLEAR_is_satisfied_by_a_readback_with_no_parent() -> None:
    """The mirror of the cell above, so "absent means no parent" is not read as "absent always
    fails". A clear that genuinely detached the sub-task must be accepted."""
    client = _FakeClient(parent_key="RBJ-1", applies_writes=True)

    _transport(client).set_parent("RBJ-3", None)

    assert client.parent_key is None


def test_a_failing_update_surfaces_its_own_error_not_the_readback_verdict() -> None:
    """When the write itself raises, that exception must propagate unchanged.

    A verification step bolted on carelessly can swallow the real error and replace it with its
    own "the parent did not move" — which is true but useless, and would hide a genuine 4xx
    behind a message about read-back.
    """
    boom = RuntimeError("HTTP 503 from Data Center")
    client = _FakeClient(parent_key="RBJ-1", update_raises=boom)

    with pytest.raises(RuntimeError) as excinfo:
        _transport(client).set_parent("RBJ-3", "RBJ-2")

    assert "503" in str(excinfo.value), (
        f"the original transport error was replaced rather than propagated: {excinfo.value!r}"
    )


def test_the_epic_link_branch_is_unchanged_by_the_subtask_verification() -> None:
    """REGRESSION GUARD for the non-sub-task path (1311 / 9bb9).

    The Epic Link write is a different field on a different branch and was proven to take effect
    on this instance. The read-back added for sub-tasks must not leak into it — a verification
    that looked for ``fields.parent`` on an epic child would fail every epic parent, undoing the
    only outbound parent path that actually works on DC.
    """
    client = _FakeClient(subtask=False, applies_writes=False)

    _transport(client).set_parent("RBJ-5", "RBJ-1")

    assert {"customfield_10014": "RBJ-1"} in client.updates, (
        f"the epic-link write was lost or rerouted: {client.updates!r}"
    )
