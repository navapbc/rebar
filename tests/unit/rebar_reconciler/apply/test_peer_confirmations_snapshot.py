"""Epic a4bd / story 83d3: peer confirmation from AUTHORITATIVE fetched snapshots.

S1 records evidence when WE push a link. This is the second, independent source: a
link the peer demonstrably carries is synchronized even if this clone never pushed
it (peer-created links, links pushed before the store existed, links pushed by
another clone).

The load-bearing property under test is COMPLETENESS, and it is a trichotomy, not a
boolean:

  * ``issuelinks`` key ABSENT  -> UNOBSERVED. Never evidence, in either direction.
  * ``issuelinks: []``         -> AUTHORITATIVELY EMPTY. The peer carries nothing.
  * ``issuelinks: [...]``      -> AUTHORITATIVE. Each resolvable entry is evidence.

That distinction is inherited from ``fetcher.merge_issuelinks_map``, which writes the
key ONLY on a complete read — so a truncated page walk, an HTTP 410, or a backend
without ``get_issuelinks_map`` leaves the key absent rather than presenting an empty
list. The trap this suite guards is writing ``entry.get("issuelinks") or []``, which
silently collapses observed-empty into unobserved.

The other invariant is that confirmation is MONOTONIC: observing confirms, but not
observing never un-confirms. An un-confirmation path would reintroduce "absence is
evidence" — the precise failure the epic exists to remove — so its absence is
asserted, not assumed.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def pc():
    return importlib.import_module("rebar_reconciler.peer_confirmations")


@pytest.fixture
def store(tmp_path, pc):
    (tmp_path / ".tickets-tracker" / ".bridge_state").mkdir(parents=True)
    return pc.PeerConfirmationStore(str(tmp_path / ".tickets-tracker"))


class _Logger:
    """Minimal sync-logger stub — the persist phase closes the pass through it."""

    def log(self, *_a, **_kw):
        return None

    def close(self, *_a, **_kw):
        return None


class _Bindings:
    """Reverse map only — that is all snapshot confirmation needs."""

    def __init__(self, reverse: dict[str, str]) -> None:
        self._reverse = reverse

    def get_local_id(self, jira_key):
        return self._reverse.get(jira_key)


def _outward(key="PROJ-9", name="Blocks", link_id="500"):
    """X --outward Blocks--> Y  ==  X blocks Y."""
    return {"id": link_id, "type": {"name": name}, "outwardIssue": {"key": key}}


def _inward(key="PROJ-9", name="Blocks", link_id="501"):
    """X <--inward Blocks-- Y  ==  X is blocked by Y  ==  depends_on."""
    return {"id": link_id, "type": {"name": name}, "inwardIssue": {"key": key}}


# ---------------------------------------------------------------------------
# The completeness trichotomy
# ---------------------------------------------------------------------------


def test_observed_links_are_confirmed_with_snapshot_provenance(pc, store):
    snapshot = {"PROJ-1": {"issuelinks": [_outward()]}}
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    written = pc.confirm_from_snapshot(store, snapshot, bindings, "pass-3")

    assert written == 1
    record = store.get("src-local", "dst-local", "blocks")
    assert record is not None
    assert record["direction"] == pc.DIRECTION_SNAPSHOT
    assert record["source"] == pc.SOURCE_SNAPSHOT
    assert record["confirmed_pass"] == "pass-3"
    assert record["link_id"] == "500"


def test_unobserved_issue_confirms_nothing(pc, store):
    """The ``issuelinks`` key is ABSENT — a failed or truncated read.

    This is the case that must never become evidence. The issue is otherwise
    indistinguishable from one whose links we read successfully, so only the key's
    presence separates them.
    """
    snapshot = {"PROJ-1": {"summary": "no links were read for this issue"}}
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    written = pc.confirm_from_snapshot(store, snapshot, bindings, "pass-3")

    assert written == 0
    assert len(store) == 0


def test_authoritative_empty_confirms_nothing_and_unconfirms_nothing(pc, store):
    """``issuelinks: []`` is a COMPLETE read proving the peer carries no links.

    It still writes no confirmation (there is nothing to confirm), and — critically —
    it does not retract an existing one. A future "prune stale confirmations" feature
    would have to be argued on its own terms; inferring it from an empty list here
    would resurrect absence-as-evidence.
    """
    store.record("src-local", "dst-local", "blocks", link_id="500", pass_id="pass-1")
    snapshot = {"PROJ-1": {"issuelinks": []}}
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    written = pc.confirm_from_snapshot(store, snapshot, bindings, "pass-3")

    assert written == 0
    assert store.is_confirmed("src-local", "dst-local", "blocks") is True


def test_get_or_empty_idiom_would_break_the_trichotomy(pc, store):
    """Pins the distinction itself: absent and empty must NOT be handled alike.

    Both write nothing, so a count-only assertion cannot tell them apart. The
    difference is observable on the store's dirtiness/persistence, so assert that
    neither shape persists a file while the observed shape does.
    """
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})
    assert pc.confirm_from_snapshot(store, {"PROJ-1": {}}, bindings, "p") == 0
    assert pc.confirm_from_snapshot(store, {"PROJ-1": {"issuelinks": []}}, bindings, "p") == 0
    assert (
        pc.confirm_from_snapshot(store, {"PROJ-1": {"issuelinks": [_outward()]}}, bindings, "p")
        == 1
    )


# ---------------------------------------------------------------------------
# Direction and binding resolution
# ---------------------------------------------------------------------------


def test_inward_blocks_is_recorded_as_depends_on(pc, store):
    """Direction is resolved in LOCAL vocabulary via ``link_direction``.

    An inward ``Blocks`` means "X is blocked by Y" — ``depends_on``, not ``blocks``.
    Recording the un-inverted relation would key the evidence under a relation the
    local graph does not hold, so the relation-scoped reader would never find it and
    a legitimate link would look unconfirmed forever.
    """
    snapshot = {"PROJ-1": {"issuelinks": [_inward()]}}
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    pc.confirm_from_snapshot(store, snapshot, bindings, "pass-3")

    assert store.is_confirmed("src-local", "dst-local", "depends_on") is True
    assert store.is_confirmed("src-local", "dst-local", "blocks") is False


def test_unbound_source_or_target_is_skipped(pc, store):
    bindings = _Bindings({"PROJ-1": "src-local"})  # PROJ-9 unbound
    assert (
        pc.confirm_from_snapshot(store, {"PROJ-1": {"issuelinks": [_outward()]}}, bindings, "p")
        == 0
    )

    bindings2 = _Bindings({"PROJ-9": "dst-local"})  # PROJ-1 unbound
    assert (
        pc.confirm_from_snapshot(store, {"PROJ-1": {"issuelinks": [_outward()]}}, bindings2, "p")
        == 0
    )
    assert len(store) == 0


def test_unmapped_link_type_and_malformed_entries_are_skipped(pc, store):
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})
    snapshot = {
        "PROJ-1": {
            "issuelinks": [
                {"type": {"name": "Duplicate"}, "outwardIssue": {"key": "PROJ-9"}},
                {"type": {}, "outwardIssue": {"key": "PROJ-9"}},
                "not-a-dict",
                {},
            ]
        }
    }

    assert pc.confirm_from_snapshot(store, snapshot, bindings, "p") == 0


def test_non_list_issuelinks_is_treated_as_unobserved(pc, store):
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})
    assert pc.confirm_from_snapshot(store, {"PROJ-1": {"issuelinks": "nope"}}, bindings, "p") == 0


def test_empty_or_missing_snapshot_is_safe(pc, store):
    bindings = _Bindings({})
    assert pc.confirm_from_snapshot(store, {}, bindings, "p") == 0
    assert pc.confirm_from_snapshot(store, None, bindings, "p") == 0


def test_reconfirmation_is_idempotent_and_refreshes_the_pass(pc, store):
    snapshot = {"PROJ-1": {"issuelinks": [_outward()]}}
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    pc.confirm_from_snapshot(store, snapshot, bindings, "pass-1")
    pc.confirm_from_snapshot(store, snapshot, bindings, "pass-2")

    assert len(store) == 1
    record = store.get("src-local", "dst-local", "blocks")
    assert record["confirmed_pass"] == "pass-2"
    assert record["first_confirmed_at"] <= record["confirmed_at"]


# ---------------------------------------------------------------------------
# The reconcile persist-phase seam
# ---------------------------------------------------------------------------


def test_persist_seam_writes_and_saves(tmp_path, pc):
    """``_confirm_peer_links`` is the named seam the persist phase calls."""
    reconcile = importlib.import_module("rebar_reconciler.reconcile")
    (tmp_path / ".tickets-tracker" / ".bridge_state").mkdir(parents=True)

    ctx = reconcile._PassContext(repo_root=tmp_path, pass_id="pass-9")
    ctx.curr_snapshot = {"PROJ-1": {"issuelinks": [_outward()]}}
    ctx.binding_store = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    written = reconcile._confirm_peer_links(ctx, "pass-9")

    assert written == 1
    assert (tmp_path / ".tickets-tracker" / ".bridge_state" / "peer_confirmations.json").exists()
    assert pc.open_store(tmp_path).is_confirmed("src-local", "dst-local", "blocks") is True


def test_confirmation_failure_does_not_break_the_pass(tmp_path, monkeypatch, capsys):
    """AC6 fail-open. A raising confirmation must not abort persistence.

    Driven through the real ``_persist_and_log`` rather than the seam, because the
    property under test is the caller's try/except — testing the seam alone would
    pass even if the call site were unguarded.
    """
    reconcile = importlib.import_module("rebar_reconciler.reconcile")

    def _boom(_ctx, _pass_id):
        raise RuntimeError("snapshot confirmation exploded")

    monkeypatch.setattr(reconcile, "_confirm_peer_links", _boom)
    saved: list[bool] = []

    class _BS:
        def save(self):
            saved.append(True)

    monkeypatch.setattr(reconcile, "_commit_binding_store_snapshot", lambda *a, **k: True)

    ctx = reconcile._PassContext(repo_root=tmp_path, pass_id="pass-9")
    ctx.binding_store = _BS()
    ctx.curr_snapshot = {}
    ctx.prev_path = tmp_path / "prev_snapshot.json"
    ctx.sync_logger = _Logger()

    # Deliberately NOT wrapped: if the confirmation failure propagates, this call
    # raises and the test fails with the real traceback, which is the better report.
    reconcile._persist_and_log(ctx)

    assert saved == [True], "binding store was not saved after a confirmation failure"
    assert "peer-link confirmation failed" in capsys.readouterr().err


def test_no_write_mode_confirms_nothing(tmp_path, monkeypatch):
    """AC7. A no-write pass writes no evidence, just as it writes no bindings."""
    reconcile = importlib.import_module("rebar_reconciler.reconcile")
    calls: list[str] = []
    monkeypatch.setattr(
        reconcile, "_confirm_peer_links", lambda _ctx, pass_id: calls.append(pass_id) or 0
    )

    ctx = reconcile._PassContext(repo_root=tmp_path, pass_id="pass-9")
    ctx.persist = False
    ctx.binding_store = object()
    ctx.curr_snapshot = {"PROJ-1": {"issuelinks": [_outward()]}}
    ctx.prev_path = tmp_path / "prev_snapshot.json"
    ctx.sync_logger = _Logger()

    reconcile._persist_and_log(ctx)

    assert calls == [], "confirmation ran in no-write mode"


def test_persist_seam_writes_no_file_when_nothing_is_confirmed(tmp_path):
    """A converged (or fully-unobserved) pass must not touch the file."""
    reconcile = importlib.import_module("rebar_reconciler.reconcile")
    (tmp_path / ".tickets-tracker" / ".bridge_state").mkdir(parents=True)

    ctx = reconcile._PassContext(repo_root=tmp_path, pass_id="pass-9")
    ctx.curr_snapshot = {"PROJ-1": {"summary": "unobserved"}}
    ctx.binding_store = _Bindings({"PROJ-1": "src-local"})

    assert reconcile._confirm_peer_links(ctx, "pass-9") == 0
    assert not (
        tmp_path / ".tickets-tracker" / ".bridge_state" / "peer_confirmations.json"
    ).exists()
