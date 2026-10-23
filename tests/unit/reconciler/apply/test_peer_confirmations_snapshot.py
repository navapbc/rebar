"""Confirm links only from authoritative snapshots: absent issuelinks is unobserved, an empty
list is observed absence, and present links provide evidence. Preserve direction,
idempotence, fail-open persistence, and no sidecar rewrite on unchanged passes."""

from __future__ import annotations

import importlib
import time

import pytest

import rebar.reconciler.pass_support as pass_support
import rebar.reconciler.persist_phase as persist_phase


@pytest.fixture
def pc():
    return importlib.import_module("rebar.reconciler.peer_confirmations")


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

    written = pc.confirm_from_snapshot(store, snapshot, bindings)

    assert written == 1
    record = store.get("src-local", "dst-local", "blocks")
    assert record is not None
    assert record["direction"] == pc.DIRECTION_SNAPSHOT
    assert record["source"] == pc.SOURCE_SNAPSHOT
    assert "confirmed_pass" not in record  # bug 266c: per-pass telemetry removed
    assert record["link_id"] == "500"


def test_unobserved_issue_confirms_nothing(pc, store):
    """An absent issuelinks key is an incomplete observation and must confirm nothing."""
    snapshot = {"PROJ-1": {"summary": "no links were read for this issue"}}
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    written = pc.confirm_from_snapshot(store, snapshot, bindings)

    assert written == 0
    assert len(store) == 0


def test_authoritative_empty_confirms_nothing_and_unconfirms_nothing(pc, store):
    """An observed empty issuelinks list confirms nothing and does not retract earlier
    evidence."""
    store.record("src-local", "dst-local", "blocks", link_id="500")
    snapshot = {"PROJ-1": {"issuelinks": []}}
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    written = pc.confirm_from_snapshot(store, snapshot, bindings)

    assert written == 0
    assert store.is_confirmed("src-local", "dst-local", "blocks") is True


def test_get_or_empty_idiom_would_break_the_trichotomy(pc, store):
    """Keep absent, observed-empty, and observed-present link states distinct; only present
    links persist evidence."""
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})
    assert pc.confirm_from_snapshot(store, {"PROJ-1": {}}, bindings) == 0
    assert pc.confirm_from_snapshot(store, {"PROJ-1": {"issuelinks": []}}, bindings) == 0
    assert pc.confirm_from_snapshot(store, {"PROJ-1": {"issuelinks": [_outward()]}}, bindings) == 1


# ---------------------------------------------------------------------------
# Direction and binding resolution
# ---------------------------------------------------------------------------


def test_inward_blocks_is_recorded_as_depends_on(pc, store):
    """An inward Blocks link maps to local depends_on; recording blocks would strand
    relation-scoped evidence."""
    snapshot = {"PROJ-1": {"issuelinks": [_inward()]}}
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    pc.confirm_from_snapshot(store, snapshot, bindings)

    assert store.is_confirmed("src-local", "dst-local", "depends_on") is True
    assert store.is_confirmed("src-local", "dst-local", "blocks") is False


def test_unbound_source_or_target_is_skipped(pc, store):
    bindings = _Bindings({"PROJ-1": "src-local"})  # PROJ-9 unbound
    assert pc.confirm_from_snapshot(store, {"PROJ-1": {"issuelinks": [_outward()]}}, bindings) == 0

    bindings2 = _Bindings({"PROJ-9": "dst-local"})  # PROJ-1 unbound
    assert pc.confirm_from_snapshot(store, {"PROJ-1": {"issuelinks": [_outward()]}}, bindings2) == 0
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

    assert pc.confirm_from_snapshot(store, snapshot, bindings) == 0


def test_non_list_issuelinks_is_treated_as_unobserved(pc, store):
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})
    assert pc.confirm_from_snapshot(store, {"PROJ-1": {"issuelinks": "nope"}}, bindings) == 0


def test_empty_or_missing_snapshot_is_safe(pc, store):
    bindings = _Bindings({})
    assert pc.confirm_from_snapshot(store, {}, bindings) == 0
    assert pc.confirm_from_snapshot(store, None, bindings) == 0


def test_reconfirmation_is_idempotent_and_does_not_rewrite(pc, store):
    """Reobserving the same link preserves its first confirmation and leaves the store clean."""
    snapshot = {"PROJ-1": {"issuelinks": [_outward()]}}
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    pc.confirm_from_snapshot(store, snapshot, bindings)
    first_seen = store.get("src-local", "dst-local", "blocks")["first_confirmed_at"]
    store.save()
    assert store._dirty is False

    pc.confirm_from_snapshot(store, snapshot, bindings)

    assert len(store) == 1
    record = store.get("src-local", "dst-local", "blocks")
    assert record["first_confirmed_at"] == first_seen  # provenance preserved
    assert "confirmed_pass" not in record and "confirmed_at" not in record
    assert store._dirty is False  # unchanged evidence never re-dirties the store


# ---------------------------------------------------------------------------
# The reconcile persist-phase seam
# ---------------------------------------------------------------------------


def test_persist_seam_writes_and_saves(tmp_path, pc):
    """``persist_phase.confirm_peer_links`` is the named seam the persist phase calls."""
    reconcile = importlib.import_module("rebar.reconciler.reconcile")
    (tmp_path / ".tickets-tracker" / ".bridge_state").mkdir(parents=True)

    ctx = reconcile._PassContext(repo_root=tmp_path, pass_id="pass-9")
    ctx.curr_snapshot = {"PROJ-1": {"issuelinks": [_outward()]}}
    ctx.binding_store = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    written = persist_phase.confirm_peer_links(ctx)

    assert written == 1
    assert (tmp_path / ".tickets-tracker" / ".bridge_state" / "peer_confirmations.json").exists()
    assert pc.open_store(tmp_path).is_confirmed("src-local", "dst-local", "blocks") is True


def test_confirmation_failure_does_not_break_the_pass(tmp_path, monkeypatch, capsys):
    """A confirmation-store failure is isolated by the real persist caller and cannot abort
    the pass."""
    reconcile = importlib.import_module("rebar.reconciler.reconcile")

    def _boom(_ctx):
        raise RuntimeError("snapshot confirmation exploded")

    monkeypatch.setattr(persist_phase, "confirm_peer_links", _boom)
    saved: list[bool] = []

    class _BS:
        def save(self):
            saved.append(True)

    monkeypatch.setattr(pass_support, "_commit_binding_store_snapshot", lambda *a, **k: True)

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
    reconcile = importlib.import_module("rebar.reconciler.reconcile")
    calls: list[str] = []
    monkeypatch.setattr(
        persist_phase, "confirm_peer_links", lambda _ctx: calls.append("called") or 0
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
    reconcile = importlib.import_module("rebar.reconciler.reconcile")
    (tmp_path / ".tickets-tracker" / ".bridge_state").mkdir(parents=True)

    ctx = reconcile._PassContext(repo_root=tmp_path, pass_id="pass-9")
    ctx.curr_snapshot = {"PROJ-1": {"summary": "unobserved"}}
    ctx.binding_store = _Bindings({"PROJ-1": "src-local"})

    assert persist_phase.confirm_peer_links(ctx) == 0
    assert not (
        tmp_path / ".tickets-tracker" / ".bridge_state" / "peer_confirmations.json"
    ).exists()


def test_two_passes_over_unchanged_snapshot_do_not_rewrite_the_sidecar(pc, tmp_path):
    """Two complete passes over the same authoritative links leave the sidecar unchanged."""
    (tmp_path / ".tickets-tracker" / ".bridge_state").mkdir(parents=True)
    tracker = str(tmp_path / ".tickets-tracker")
    snapshot = {"PROJ-1": {"issuelinks": [_outward()]}}
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    def one_pass():
        s = pc.PeerConfirmationStore(tracker)
        pc.confirm_from_snapshot(s, snapshot, bindings)
        s.save()
        with open(s.path, "rb") as handle:
            return handle.read()

    first = one_pass()
    time.sleep(0.01)  # guarantee a distinct wall clock so any per-pass timestamp diverges
    second = one_pass()

    assert first == second, "unchanged link evidence re-persisted the full sidecar"


def test_reconfirming_unchanged_link_leaves_the_store_not_dirty(pc, store):
    """AC1 (bug 266c). Re-``record()``-ing byte-identical evidence is a no-op: the
    store stays clean so ``save()`` writes nothing on a converged pass.
    """
    snapshot = {"PROJ-1": {"issuelinks": [_outward()]}}
    bindings = _Bindings({"PROJ-1": "src-local", "PROJ-9": "dst-local"})

    assert pc.confirm_from_snapshot(store, snapshot, bindings) == 1
    assert store._dirty is True  # first observation is genuinely new
    store.save()
    assert store._dirty is False

    # Second identical observation must not re-dirty the store.
    assert pc.confirm_from_snapshot(store, snapshot, bindings) == 1
    assert store._dirty is False
