"""Epic a4bd / story f6e9: grandfather pre-upgrade managed links as confirmed.

S3 declines any inbound link removal whose link carries no peer-confirmation record.
On the FIRST run after this feature ships the store is empty, so without a backfill
EVERY legitimate peer deletion would be declined — a worse regression than the blind
spot the epic set out to close. This suite pins the grandfathering that buys that
first pass, and the three properties that keep it from becoming a liability:

1. ONE-SHOT ON FILE ABSENCE, not on emptiness. An operator who deliberately empties
   the store must not have it silently repopulated on the next pass.
2. NEVER DOWNGRADES. ``record()`` overwrites unconditionally, so backfill must read
   before writing. An existing ``push``/``snapshot`` record is strictly stronger than
   an assumption, and clobbering it would discard the vendor link id and the
   confirming pass.
3. ONE STORE INSTANCE. Backfill and snapshot confirmation share an instance. Two
   would each load a pre-write copy, so the same-pass upgrade could not happen and —
   worse — whichever saved last would silently discard the other's records. The
   lost-update test below is what stops that regression from being reintroduced.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def pc():
    return importlib.import_module("rebar_reconciler.peer_confirmations")


@pytest.fixture
def tracker(tmp_path):
    (tmp_path / ".tickets-tracker" / ".bridge_state").mkdir(parents=True)
    return tmp_path


class _Bindings:
    """Forward map (local -> jira) for boundness, reverse map for snapshot confirm."""

    def __init__(self, forward: dict[str, str]) -> None:
        self._forward = forward
        self._reverse = {v: k for k, v in forward.items()}

    def get_jira_key(self, local_id):
        return self._forward.get(local_id)

    def get_local_id(self, jira_key):
        return self._reverse.get(jira_key)


def _ticket(local_id="src-local", refs=(("blocks", "dst-local"),)):
    return {"ticket_id": local_id, "managed_refs": [[k, t] for k, t in refs]}


# ---------------------------------------------------------------------------
# One-shot behaviour
# ---------------------------------------------------------------------------


def test_fresh_upgrade_backfills_every_bound_managed_ref(pc, tracker):
    store = pc.open_store(tracker)
    assert store.was_absent is True

    written = pc.backfill_from_managed_refs(
        store, [_ticket()], _Bindings({"dst-local": "PROJ-9"}), "pass-1"
    )

    assert written == 1
    record = store.get("src-local", "dst-local", "blocks")
    assert record["source"] == pc.SOURCE_BACKFILL
    assert record["direction"] == pc.DIRECTION_BACKFILL
    assert record["link_id"] is None
    assert record["confirmed_pass"] == "pass-1"
    assert store.is_confirmed("src-local", "dst-local", "blocks") is True


def test_unbound_target_is_not_backfilled(pc, tracker):
    store = pc.open_store(tracker)
    written = pc.backfill_from_managed_refs(store, [_ticket()], _Bindings({}), "pass-1")
    assert written == 0
    assert len(store) == 0


def test_existing_but_empty_store_is_not_backfilled(pc, tracker):
    """Emptiness is an operator's choice; absence is an upgrade. Only absence backfills."""
    path = tracker / ".tickets-tracker" / ".bridge_state" / "peer_confirmations.json"
    path.write_text('{"version": 1, "records": {}}')

    store = pc.open_store(tracker)
    assert store.was_absent is False

    written = pc.backfill_from_managed_refs(
        store, [_ticket()], _Bindings({"dst-local": "PROJ-9"}), "pass-1"
    )

    assert written == 0
    assert len(store) == 0


def test_second_pass_adds_no_backfill_records(pc, tracker):
    bindings = _Bindings({"dst-local": "PROJ-9"})
    first = pc.open_store(tracker)
    assert pc.backfill_from_managed_refs(first, [_ticket()], bindings, "pass-1") == 1
    first.save()

    second = pc.open_store(tracker)
    assert second.was_absent is False
    assert pc.backfill_from_managed_refs(second, [_ticket()], bindings, "pass-2") == 0
    assert len(second) == 1


def test_non_managed_kinds_and_malformed_tickets_are_ignored(pc, tracker):
    """``managed_ref_set`` already constrains kinds; no second filter is wanted."""
    store = pc.open_store(tracker)
    tickets = [
        {"ticket_id": "src-local", "managed_refs": [["duplicates", "dst-local"]]},
        {"managed_refs": [["blocks", "dst-local"]]},  # no id
        "not-a-dict",
        {"ticket_id": "other", "managed_refs": "garbage"},
    ]

    bindings = _Bindings({"dst-local": "PROJ-9"})
    assert pc.backfill_from_managed_refs(store, tickets, bindings, "p") == 0


# ---------------------------------------------------------------------------
# Provenance: never downgrade, always upgrade
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("existing", ["push", "snapshot"])
def test_backfill_never_overwrites_proven_evidence(pc, tracker, existing):
    """AC5, no-downgrade half.

    ``record()`` overwrites unconditionally, so this property lives entirely in
    backfill's read-before-write skip. Without it the vendor link id and confirming
    pass of a real confirmation would be replaced by an assumption.
    """
    store = pc.open_store(tracker)
    store.record(
        "src-local",
        "dst-local",
        "blocks",
        link_id="10042",
        direction=pc.DIRECTION_OUTBOUND,
        pass_id="pass-0",
        source_kind=existing,
    )

    written = pc.backfill_from_managed_refs(
        store, [_ticket()], _Bindings({"dst-local": "PROJ-9"}), "pass-1"
    )

    assert written == 0
    record = store.get("src-local", "dst-local", "blocks")
    assert record["source"] == existing
    assert record["link_id"] == "10042", "a real confirmation's vendor link id was clobbered"


def test_snapshot_confirmation_upgrades_a_backfilled_record(pc, tracker):
    """AC5, upgrade half — needs no new code, only the shared instance."""
    store = pc.open_store(tracker)
    bindings = _Bindings({"src-local": "PROJ-1", "dst-local": "PROJ-9"})
    pc.backfill_from_managed_refs(store, [_ticket()], bindings, "pass-1")
    assert store.get("src-local", "dst-local", "blocks")["source"] == pc.SOURCE_BACKFILL

    snapshot = {
        "PROJ-1": {
            "issuelinks": [
                {"id": "777", "type": {"name": "Blocks"}, "outwardIssue": {"key": "PROJ-9"}}
            ]
        }
    }
    pc.confirm_from_snapshot(store, snapshot, bindings, "pass-1")

    record = store.get("src-local", "dst-local", "blocks")
    assert record["source"] == pc.SOURCE_SNAPSHOT
    assert record["link_id"] == "777"


def test_backfilled_record_counts_as_confirmed(pc, tracker):
    """Deliberately NOT weaker at the decision point — that would re-open the blind spot."""
    store = pc.open_store(tracker)
    pc.backfill_from_managed_refs(store, [_ticket()], _Bindings({"dst-local": "PROJ-9"}), "p")
    assert store.is_confirmed("src-local", "dst-local", "blocks") is True


# ---------------------------------------------------------------------------
# The single-instance persist seam
# ---------------------------------------------------------------------------


class _Logger:
    def log(self, *_a, **_kw):
        return None

    def close(self, *_a, **_kw):
        return None


def test_ordering_backfill_runs_before_snapshot_confirmation(tracker, pc):
    """Both halves run on ONE instance, backfill first, so the upgrade lands."""
    reconcile = importlib.import_module("rebar_reconciler.reconcile")

    ctx = reconcile._PassContext(repo_root=tracker, pass_id="pass-5")
    ctx.local_tickets = [_ticket()]
    ctx.binding_store = _Bindings({"src-local": "PROJ-1", "dst-local": "PROJ-9"})
    ctx.curr_snapshot = {
        "PROJ-1": {
            "issuelinks": [
                {"id": "777", "type": {"name": "Blocks"}, "outwardIssue": {"key": "PROJ-9"}}
            ]
        }
    }

    reconcile._confirm_peer_links(ctx, "pass-5")

    record = pc.open_store(tracker).get("src-local", "dst-local", "blocks")
    assert record is not None
    assert record["source"] == pc.SOURCE_SNAPSHOT, "same-pass upgrade did not happen"


def test_lost_update_backfill_and_snapshot_records_both_survive(tracker, pc):
    """The regression the single instance exists to prevent.

    Backfill writes ref A; snapshot confirms a DIFFERENT link B. With two store
    instances each loading a pre-write copy, whichever saved last would drop the
    other's record. Both must be on disk after the single save.
    """
    reconcile = importlib.import_module("rebar_reconciler.reconcile")

    ctx = reconcile._PassContext(repo_root=tracker, pass_id="pass-5")
    ctx.local_tickets = [_ticket("src-local", (("blocks", "dst-local"),))]
    ctx.binding_store = _Bindings(
        {"src-local": "PROJ-1", "dst-local": "PROJ-9", "other-local": "PROJ-2"}
    )
    ctx.curr_snapshot = {
        "PROJ-2": {
            "issuelinks": [
                {"id": "888", "type": {"name": "Relates"}, "outwardIssue": {"key": "PROJ-9"}}
            ]
        }
    }

    reconcile._confirm_peer_links(ctx, "pass-5")

    reopened = pc.open_store(tracker)
    assert reopened.is_confirmed("src-local", "dst-local", "blocks"), "backfill record was lost"
    assert reopened.is_confirmed("other-local", "dst-local", "relates_to"), (
        "snapshot record was lost"
    )


def test_no_write_mode_backfills_nothing(tracker, monkeypatch):
    reconcile = importlib.import_module("rebar_reconciler.reconcile")
    calls: list[str] = []
    monkeypatch.setattr(
        reconcile, "_confirm_peer_links", lambda _ctx, pass_id: calls.append(pass_id) or 0
    )

    ctx = reconcile._PassContext(repo_root=tracker, pass_id="pass-5")
    ctx.persist = False
    ctx.binding_store = object()
    ctx.local_tickets = [_ticket()]
    ctx.curr_snapshot = {}
    ctx.prev_path = tracker / "prev_snapshot.json"
    ctx.sync_logger = _Logger()

    reconcile._persist_and_log(ctx)

    assert calls == []


def test_fail_open_backfill_failure_does_not_break_the_pass(tracker, monkeypatch, capsys):
    reconcile = importlib.import_module("rebar_reconciler.reconcile")

    def _boom(_ctx, _pass_id):
        raise RuntimeError("backfill exploded")

    monkeypatch.setattr(reconcile, "_confirm_peer_links", _boom)
    monkeypatch.setattr(reconcile, "_commit_binding_store_snapshot", lambda *a, **k: True)
    saved: list[bool] = []

    class _BS:
        def save(self):
            saved.append(True)

    ctx = reconcile._PassContext(repo_root=tracker, pass_id="pass-5")
    ctx.binding_store = _BS()
    ctx.local_tickets = []
    ctx.curr_snapshot = {}
    ctx.prev_path = tracker / "prev_snapshot.json"
    ctx.sync_logger = _Logger()

    # Unwrapped on purpose: a propagating failure fails here with a real traceback.
    reconcile._persist_and_log(ctx)

    assert saved == [True]
    assert "peer-link confirmation failed" in capsys.readouterr().err
