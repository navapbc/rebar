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


def test_create_one_defers_instead_of_writing_a_duplicate() -> None:
    """THE ASSERTION THAT PROVES THE DUPLICATE IS PREVENTED — behavioural, not a source
    scan, and driven through the function that actually issues the write.

    `create_one` is reached with the create already planned (the differ's branch fires on
    `get_jira_key(...) is None`, which is also true of a keyless-pending entry). Its dedup
    search then misses because the issue is unindexed. Without the guard it CREATES.

    Guarding at the write rather than at plan time also reuses the existing deferral
    contract: the mutation is deferred, not dropped, so it is retried on a later pass.
    """
    from rebar_reconciler.dispatch_one import create_one

    class _Store:
        def is_keyless_pending_within_grace(self, local_id):
            return True

    client = _LaggingIndexClient()
    deferred: list = []
    result = create_one(
        {"local_id": "t1", "fields": {"summary": "s"}},
        client,
        deferred_creates=deferred,
        binding_store=_Store(),
    )

    assert not client.created, (
        "create_one wrote a SECOND Jira issue for a ticket whose create may already have "
        "landed unindexed — this is the duplicate 21fc exists to prevent"
    )
    assert client.searches == 0, "the guard ran AFTER the dedup search instead of before it"
    assert result is None
    assert deferred == [{"local_id": "t1", "fields": {"summary": "s"}}], (
        "the create was DROPPED rather than deferred — it must be retried once the index "
        "catches up or the grace window expires"
    )


def test_create_one_proceeds_normally_once_the_grace_window_has_passed() -> None:
    """The suppression is bounded: a ticket whose create truly never landed must still be
    created. Without this, the fix would trade a duplicate for a permanent omission."""
    from rebar_reconciler.dispatch_one import create_one

    class _Store:
        def is_keyless_pending_within_grace(self, local_id):
            return False

        def bind_pending(self, *a, **k):
            return None

        def record_pending_key(self, *a, **k):
            return None

        def save(self):
            return None

        def bind_confirm(self, *a, **k):
            return None

    client = _LaggingIndexClient()
    create_one(
        {"local_id": "t1", "fields": {"summary": "s"}},
        client,
        deferred_creates=[],
        binding_store=_Store(),
    )
    assert client.created, "the create was suppressed even outside the grace window"


# ---------------------------------------------------------------------------
# The WHOLE path — the criterion the ticket warned a partial test would fake
# ---------------------------------------------------------------------------


def test_the_whole_crash_recover_differ_create_path_creates_exactly_one_issue(store) -> None:
    """THE HEADLINE CRITERION: drive crash -> recover -> differ -> create as ONE sequence
    against the REAL ``BindingStore`` and assert EXACTLY ONE issue exists at the end.

    Why this test has to exist even though the two sites are already covered separately:
    the ticket's own analysis warns that "a test asserting only 'recover_pending did not
    unbind' passes today's code path while the duplicate is still written". The same is
    true in reverse — driving ``create_one`` with a hand-written double that returns True
    for the gate proves the gate works, not that the REAL store ever reports True at the
    moment the differ reaches the create branch. Only the continuous sequence, on the real
    store, rules out a fix that is correct at each site and broken at the seam between
    them.
    """
    from rebar_reconciler.dispatch_one import create_one

    client = _LaggingIndexClient()  # DC-1 already landed in the crashed pass, unindexed

    # (a) CRASH during create_issue -> the write-ahead record is keyless-pending.
    store.bind_pending("t1")

    # (b) RECOVER: the search cannot see DC-1, so recovery must NOT unbind.
    assert store.recover_pending_bindings(client) == 0
    assert "t1" in store.pending_bindings(), (
        "recovery unbound on one unindexed search — the next pass would now create a duplicate"
    )

    # (c) DIFFER: its gate is `get_jira_key(local_id) is None`, which is TRUE for a
    #     keyless-pending entry — so the differ really does reach the create branch.
    #     This is the step that makes fixing recovery alone insufficient.
    assert store.get_jira_key("t1") is None

    # (d) CREATE: the write site must defer rather than write the second issue.
    deferred: list = []
    create_one(
        {"local_id": "t1", "fields": {"summary": "s"}},
        client,
        deferred_creates=deferred,
        binding_store=store,
    )

    total_issues = 1 + len(client.created)  # the pre-existing DC-1 + anything written now
    assert total_issues == 1, (
        f"EXACTLY ONE issue must exist for this ticket; found {total_issues} "
        f"(new writes: {client.created}) — this is the duplicate 21fc exists to prevent"
    )
    assert deferred, "the create was dropped rather than deferred; it would never be retried"

    # (e) ONCE THE INDEX CATCHES UP the pending entry binds to the issue that was always
    #     there, and the differ's create branch closes — still exactly one issue.
    client.index_caught_up = True
    assert store.recover_pending_bindings(client) == 1
    assert store.get_jira_key("t1") == "DC-1"
    assert 1 + len(client.created) == 1


def test_the_cloud_path_is_unaffected_beyond_the_deferral(store) -> None:
    """CLOUD-PATH REGRESSION GUARD. ``create_one`` and ``BindingStore`` are SHARED core,
    not DC-only, so this change reaches the Cloud path too and has to be held to that.

    The intended blast radius is exactly one thing: a create is deferred while a
    KEYLESS-PENDING binding is inside the grace window. Everything else must be
    byte-for-byte the old behaviour. The two cases below are the ones a Cloud deployment
    actually runs — an ordinary new ticket, and a ticket whose create genuinely never
    landed — and neither may pay for the recovery path.
    """
    from rebar_reconciler.dispatch_one import create_one

    # 1. The overwhelmingly common Cloud case: a brand-new ticket, no binding at all.
    #    It must be created IMMEDIATELY — no deferral, no extra latency.
    client = _LaggingIndexClient()
    deferred: list = []
    create_one(
        {"local_id": "cloud-new", "fields": {"summary": "s"}},
        client,
        deferred_creates=deferred,
        binding_store=store,
    )
    assert client.created, "an ordinary new ticket was deferred; the happy path regressed"
    assert deferred == [], "an ordinary new ticket must not be deferred at all"

    # 2. A create that genuinely never landed: suppressed only until the window expires,
    #    then created. The fix must not trade a rare duplicate for a permanent omission.
    store.bind_pending("cloud-orphan")
    store._data["bindings"]["cloud-orphan"]["created_at"] = "2000-01-01T00:00:00Z"
    before = len(client.created)
    create_one(
        {"local_id": "cloud-orphan", "fields": {"summary": "s"}},
        client,
        deferred_creates=[],
        binding_store=store,
    )
    assert len(client.created) == before + 1, (
        "a ticket past the grace window was still suppressed — the deferral is unbounded, "
        "which is a permanent silent omission rather than a delay"
    )


# ---------------------------------------------------------------------------
# S4 T3 (2863-c335): AC3 — the keyless negative unbind requires BOTH thresholds
# (three corroborating misses AND the 3600s index-lag grace), and the coordinated
# create route prevents an index-lag duplicate.
# ---------------------------------------------------------------------------


def test_ac3_aged_entry_needs_three_misses_not_fewer(store) -> None:
    """AC3, grace-satisfied half: an entry older than the 3600s grace still is NOT
    unbound before the miss count is corroborated (fewer than three misses)."""
    store.bind_pending("t1")
    store._data["bindings"]["t1"]["created_at"] = "2000-01-01T00:00:00Z"  # past grace
    client = _LaggingIndexClient()

    # Two misses (below _MISSES_BEFORE_UNBIND = 3) must NOT unbind, despite the grace.
    store.recover_pending_bindings(client)
    store.recover_pending_bindings(client)
    assert store.is_pending("t1"), "unbound before three corroborating misses"

    # The third miss corroborates absence → now (and only now) it unbinds.
    store.recover_pending_bindings(client)
    assert not store.is_bound("t1")


def test_ac3_young_entry_never_unbinds_regardless_of_misses(store) -> None:
    """AC3, misses-satisfied half: many misses inside the 3600s grace window must NOT
    unbind — the grace threshold is independently required."""
    store.bind_pending("t1")  # created now → inside grace
    client = _LaggingIndexClient()
    for _ in range(10):
        store.recover_pending_bindings(client)
    assert store.is_pending("t1"), "unbound on miss count alone, ignoring the grace window"


def test_ac3_coordinated_route_defers_within_grace_no_duplicate(store) -> None:
    """AC3 duplicate-prevention through the coordinated path: while a keyless-pending
    binding is inside the grace window the create is SUPPRESSED (deferred), so a lagging
    index cannot cause a second issue."""
    store.bind_pending("dc-orphan")  # keyless-pending, freshly created → within grace
    assert store.is_keyless_pending_within_grace("dc-orphan") is True

    from rebar_reconciler.dispatch_one import create_one

    client = _LaggingIndexClient()
    deferred: list = []
    mutation = {
        "local_id": "dc-orphan",
        "action": "create",
        "fields": {"summary": "s", "issuetype": {"name": "Task"}},
    }
    result = create_one(
        mutation,
        client,
        deferred_creates=deferred,
        repo_root=store._repo_root if hasattr(store, "_repo_root") else None,
        binding_store=store,
    )
    assert result is None  # deferred, not created
    assert client.created == []  # NO physical create while inside the grace window
    assert deferred == [mutation]
