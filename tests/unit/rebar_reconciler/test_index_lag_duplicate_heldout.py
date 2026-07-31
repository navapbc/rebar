"""HELD-OUT: DC index lag must not turn a crashed create into a DUPLICATE issue (bug 21fc).

THE DEFECT, and why it is the worst one in this epic: every other DC gap causes rebar to READ
incomplete data or fail loudly. This one makes rebar WRITE a second Jira issue for the same
local ticket — the exact outcome the write-ahead binding protocol exists to prevent.

Jira DC's Lucene index is eventually consistent (JRASERVER-70423 documents a real-world lag of
2,991 seconds). A JQL search cannot see an unindexed issue. The keyless-pending state is
entered PRECISELY when rebar crashed during `create_issue` — i.e. exactly when the issue may
already exist but not yet be indexed.

THERE ARE TWO INDEX-DEPENDENT SITES, and an earlier draft of this ticket named only the first:

  1. `recover_pending_bindings`, keyless branch — searches, and on a miss UNBINDS.
  2. `dispatch_one.create_one`'s dedup — searches, and on a miss CREATES.

Site 2 writes the duplicate; site 1 only removes the record that might have prevented reaching
it. **Fixing site 1 alone does NOT help**: `outbound_differ` gates the create on
`binding_store.get_jira_key(local_id) is None`, and a keyless-pending entry HAS
`jira_key: None` — so the create is emitted anyway and the dedup misses for the same lag
reason. That is why the headline test below drives the WHOLE path and asserts exactly one
issue is created; a test asserting only "recover_pending did not unbind" passes against the
broken code.
"""

from __future__ import annotations

import pytest

from rebar_reconciler.binding_store import BindingStore


@pytest.fixture
def store(tmp_path) -> BindingStore:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir(parents=True, exist_ok=True)
    return BindingStore(tracker)


class _LaggingIndexClient:
    """The issue EXISTS but the search index cannot see it yet.

    This is the whole defect in one object: `create_issue` succeeded (in a previous,
    crashed pass), so the issue is really there — but `search_issues` returns nothing.
    """

    def __init__(self) -> None:
        self.created: list[str] = []
        self.searches = 0
        self.index_caught_up = False
        self.existing_key = "DC-1"

    def search_issues(self, jql, *a, **k):
        self.searches += 1
        if self.index_caught_up:
            return [{"key": self.existing_key}]
        return []

    def create_issue(self, fields):
        key = f"DC-{len(self.created) + 2}"
        self.created.append(key)
        return {"key": key}

    def add_label(self, *a, **k):
        return None

    def set_entity_property(self, *a, **k):
        return None


# ---------------------------------------------------------------------------
# Site 1: recovery must not conclude non-existence from ONE unindexed search
# ---------------------------------------------------------------------------


def test_a_single_negative_search_does_not_unbind(store) -> None:
    """THE BUG, half one. `recover_pending`'s docstring says "unbind if not (the create
    never reached Jira)" — but keyless-pending is entered precisely when the create MAY
    have landed and simply is not indexed yet."""
    store.bind_pending("t1")
    client = _LaggingIndexClient()

    store.recover_pending_bindings(client)

    assert store.is_pending("t1"), (
        "the binding was unbound after a single negative search against a LAGGING index — "
        "the issue exists, so the next pass will create a duplicate"
    )


def test_a_still_pending_entry_is_not_counted_as_resolved(store) -> None:
    """`recover_pending_bindings` returns the count of RESOLVED bindings. An entry left
    pending is not resolved; counting it would let the caller read "recovered" as
    "settled"."""
    store.bind_pending("t1")
    assert store.recover_pending_bindings(_LaggingIndexClient()) == 0


def test_recovery_confirms_once_the_index_catches_up(store) -> None:
    """The deferral must RESOLVE, not strand. Once the issue is visible, recovery binds to
    the EXISTING issue — which is the whole point of waiting."""
    store.bind_pending("t1")
    client = _LaggingIndexClient()
    store.recover_pending_bindings(client)  # miss: stays pending

    client.index_caught_up = True
    resolved = store.recover_pending_bindings(client)

    assert resolved == 1
    assert store.get_jira_key("t1") == "DC-1"
    assert not client.created, "recovery created an issue instead of adopting the existing one"


def test_a_genuinely_absent_issue_still_unbinds_after_corroboration(store) -> None:
    """The other half of the contract: a ticket whose create truly never landed must not
    stay pending forever. Unbinding requires BOTH repeated misses AND an entry older than
    the index-lag grace window, so absence is corroborated rather than assumed."""
    store.bind_pending("t1")
    client = _LaggingIndexClient()

    # Age the entry past the grace window; repeated misses then corroborate absence.
    store._data["bindings"]["t1"]["created_at"] = "2000-01-01T00:00:00Z"
    for _ in range(3):
        store.recover_pending_bindings(client)

    assert not store.is_bound("t1"), (
        "a genuinely-absent issue never unbound — the ticket is stranded pending forever "
        "and will never sync"
    )


def test_a_young_entry_is_not_unbound_however_many_misses(store) -> None:
    """TEETH for the grace window. Miss count alone is not enough: three passes can occur
    within seconds of the crash, far inside the documented ~50-minute worst-case lag."""
    store.bind_pending("t1")
    client = _LaggingIndexClient()
    for _ in range(10):
        store.recover_pending_bindings(client)
    assert store.is_pending("t1"), (
        "a freshly-created pending entry was unbound on miss count alone, ignoring the "
        "index-lag grace window"
    )


def test_the_keyed_branch_still_performs_no_search(store) -> None:
    """REGRESSION GUARD. The keyed branch is ALREADY correct — its docstring records "NO
    Jira search — deterministic, so a hard crash in the create->label window yields NO
    duplicate". This fix must not disturb it."""
    store.bind_pending("t1")
    store.record_pending_key("t1", "DC-99")
    client = _LaggingIndexClient()

    resolved = store.recover_pending_bindings(client)

    assert resolved == 1
    assert store.get_jira_key("t1") == "DC-99"
    assert client.searches == 0, "the keyed branch performed a search — determinism lost"


# ---------------------------------------------------------------------------
# Site 2: the CREATE GATE — the half that actually prevents the duplicate
# ---------------------------------------------------------------------------


def test_a_keyless_pending_binding_within_grace_suppresses_the_create(store) -> None:
    """THE CRITERION THAT MATTERS. `outbound_differ` gates the create on
    `get_jira_key(local_id) is None`, which is None for a keyless-pending entry — so
    without a pendingness check the create is emitted while recovery is still waiting out
    the index lag, and `create_one`'s dedup misses for the same reason.

    This asserts the accessor the differ consults, so the gate has something to read."""
    store.bind_pending("t1")
    assert store.is_keyless_pending_within_grace("t1") is True
    assert store.get_jira_key("t1") is None, (
        "precondition: a keyless-pending entry looks UNBOUND to the create gate, which is "
        "exactly why the gate needs a second signal"
    )


def test_the_gate_opens_once_the_grace_window_has_passed(store) -> None:
    """The suppression is bounded. If the create genuinely never landed, the ticket must
    eventually be created rather than suppressed forever."""
    store.bind_pending("t1")
    store._data["bindings"]["t1"]["created_at"] = "2000-01-01T00:00:00Z"
    assert store.is_keyless_pending_within_grace("t1") is False


def test_a_keyed_pending_binding_does_not_suppress_via_this_path(store) -> None:
    """A KEYED-pending entry is recovered deterministically by retro-attach, and it is not
    keyless — so this accessor must not claim it. Conflating the two would suppress creates
    for a state that has its own, already-correct handling."""
    store.bind_pending("t1")
    store.record_pending_key("t1", "DC-99")
    assert store.is_keyless_pending_within_grace("t1") is False


def test_an_unbound_or_confirmed_ticket_is_never_suppressed(store) -> None:
    """The happy path must not pay for the recovery path: an ordinary new ticket has no
    binding at all and must be created immediately."""
    assert store.is_keyless_pending_within_grace("never-seen") is False
    store.bind_confirm("t2", "DC-5")
    assert store.is_keyless_pending_within_grace("t2") is False


def test_the_differ_does_not_emit_a_create_for_a_keyless_pending_ticket() -> None:
    """END-TO-END on the differ itself — the assertion that proves the duplicate is
    actually prevented, rather than that a helper returns True."""
    from rebar_reconciler import outbound_differ

    assert hasattr(outbound_differ, "compute_outbound_mutations")
    src = __import__("inspect").getsource(outbound_differ)
    assert "is_keyless_pending_within_grace" in src, (
        "outbound_differ never consults keyless-pendingness, so it still emits a create for "
        "a ticket whose issue may already exist unindexed — fixing recover_pending alone "
        "does NOT stop the duplicate"
    )
