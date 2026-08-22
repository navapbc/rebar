"""HELD-OUT test oracle for S6: Transition replay fallback (invalid direct Jira hop).

A Jira workflow may forbid the DIRECT end-state transition rebar wants (e.g. ``open ->
done`` when only ``open -> in_progress -> done`` is allowed). Today the outbound sync
runs ONE hop, gives up, and ``_update_one_scalar_update`` turns the failure into
comment-fallback + drift. S6 adds a *grounded* replay: mirror rebar's own recorded
status hops (the append-only ``*-STATUS.json`` events) to reach the end state via the
allowed intermediate hops, aborting to the existing comment-fallback the moment any hop
is rejected. Self-healing (inventing hops rebar never took) is OUT of scope.

These tests pin OBSERVABLE behaviour only:

* fake Cloud / DC transports that RECORD their ``transition_issue_by_name`` calls (and
  raise the real per-backend rejection types: ``RuntimeError`` for Cloud acli,
  ``ValueError`` / ``IllegalTransitionError`` for Data Center);
* an on-disk ``<tracker>/<local_id>/<ts>-STATUS.json`` event store, exactly the shape
  ``rebar._commands.txn`` writes (``{"data": {"status": <local>, "current_status":
  <from>}}``);
* the store located through the SAME config seam the module self-resolves
  (``config.tracker_dir(config.reconciler_repo_root())``), pointed at the on-disk store
  via the ``REBAR_TRACKER_DIR`` / ``REBAR_ROOT`` env overrides that seam honours.

No private structure is asserted: only the recorded transition-call sequence, the
``replay_transition`` boolean contract, the forward-mapped hop list, and ``update_one``'s
``result is None`` drift signal.

Target module (does NOT yet exist — its absence is the correct RED state):
``rebar_reconciler.transition_replay`` with ``recorded_status_hops(local_id, tracker)``
and ``replay_transition(client, remote_id, local_id, target_status)``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rebar_reconciler.adapters.jira_datacenter.transitions import IllegalTransitionError
from rebar_reconciler.dispatch_one import update_one

pytestmark = pytest.mark.unit


# ── Lazy imports of the module under test ─────────────────────────────────────
# Imported INSIDE each test (not at module top) so that, before S6 lands, each
# replay/hops test fails INDEPENDENTLY with ``ModuleNotFoundError`` at its own call
# site — the correct RED signal — rather than collapsing the whole file into a single
# collection error. Once ``transition_replay`` exists, the conftest ``rebar_reconciler``
# namespace bridge resolves these the same way it resolves every other engine submodule.
def _replay_transition():
    from rebar_reconciler.transition_replay import replay_transition

    return replay_transition


def _recorded_status_hops():
    from rebar_reconciler.transition_replay import recorded_status_hops

    return recorded_status_hops


# ── On-disk STATUS-event store, byte-shaped like rebar._commands.txn writes ───
def _write_status_event(ticket_dir: Path, ts: int, target: str, current: str) -> None:
    """Append one ``{ts}-{uuid}-STATUS.json`` event whose ``data.status`` is *target*.

    ``target`` / ``current`` are LOCAL status strings (``open``/``in_progress``/...), which
    is exactly what ``txn`` records in ``data`` (``txn.py`` ~L333) and what
    ``recorded_status_hops`` forward-maps to Jira names. The timestamp is zero-padded to a
    fixed width so the filename's lexicographic order and its integer-prefix order AGREE —
    the helper is neutral about which sort the implementation chooses, while still letting
    the ordering test discriminate a filename sort from insertion/mtime order.
    """
    uuid_str = f"uuid{ts:020d}"
    fname = f"{ts:020d}-{uuid_str}-STATUS.json"
    event = {
        "timestamp": ts,
        "uuid": uuid_str,
        "event_type": "STATUS",
        "env_id": "test-env",
        "author": "test-author",
        "parent_status_uuid": None,
        "data": {
            "status": target,
            "current_status": current,
            "parent_status_uuid": None,
        },
    }
    (ticket_dir / fname).write_text(json.dumps(event), encoding="utf-8")


def _make_tracker(
    root: Path,
    local_id: str,
    local_hops: list[str],
    *,
    start_ts: int = 100,
    step: int = 100,
) -> Path:
    """Materialise ``<tracker>/<local_id>/*-STATUS.json`` for *local_hops* in order.

    Returns the tracker root (the dir that HOLDS ``<local_id>/``). ``local_hops`` is the
    ordered sequence of ``data.status`` targets, e.g. ``["open", "in_progress", "closed"]``.
    """
    tracker = root / "tracker"
    ticket_dir = tracker / local_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    prev = "open"
    for i, target in enumerate(local_hops):
        ts = start_ts + i * step
        _write_status_event(ticket_dir, ts, target=target, current=prev)
        prev = target
    return tracker


def _point_config_at(monkeypatch: pytest.MonkeyPatch, tracker: Path) -> None:
    """Route ``config.tracker_dir(config.reconciler_repo_root())`` at *tracker*.

    ``REBAR_TRACKER_DIR`` is the verbatim store override honoured by
    ``config.tracker_dir`` (via ``tracker_dir_override``); ``REBAR_ROOT`` makes
    ``reconciler_repo_root`` deterministic. Using the real env seam — not a
    monkeypatched function object — makes the wiring robust to HOW the implementation
    imports ``config``.
    """
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(tracker))
    monkeypatch.setenv("REBAR_ROOT", str(tracker.parent))


# ── Fake transports (record calls; raise the real per-backend rejection types) ─
class _FakeTransport:
    """Minimal transport recording ``transition_issue_by_name`` calls.

    * ``get_issue`` returns the canonical Jira issue JSON so the implementation reads
      ``fields.status.name`` (``["fields"]["status"]["name"]``).
    * ``transition_issue_by_name`` records ``(remote_id, target_status)`` and, when
      ``hop_error`` is set for that Jira-name target, raises the configured backend
      rejection (mid-chain abort).
    * ``update_issue`` simulates the DIRECT end-state hop that fails first: when a
      ``status`` field is present and ``update_error`` is set, it raises — the trigger
      that must route into replay.
    * ``add_comment`` records the drift comment-fallback (capability-gated in dispatch).
    """

    def __init__(
        self,
        *,
        current_status: str,
        hop_error: tuple[str, BaseException] | None = None,
        update_error: BaseException | None = None,
    ) -> None:
        self._current_status = current_status
        self._hop_error = hop_error
        self._update_error = update_error
        self.transitions: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []

    def get_issue(self, remote_id: str) -> dict[str, Any]:
        return {"key": remote_id, "fields": {"status": {"name": self._current_status}}}

    def transition_issue_by_name(self, remote_id: str, target_status: str) -> None:
        self.transitions.append((remote_id, target_status))
        if self._hop_error is not None and target_status == self._hop_error[0]:
            raise self._hop_error[1]

    def update_issue(self, remote_id: str, **kwargs: Any) -> dict[str, Any]:
        if "status" in kwargs and self._update_error is not None:
            raise self._update_error
        return {"key": remote_id, "fields": {"status": {"name": self._current_status}}}

    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]:
        self.comments.append((remote_id, body))
        return {"id": "cmt-1"}


# ── 1. Walk the recorded intermediate hops (the happy path) ───────────────────
def test_replay_walks_recorded_intermediate_hops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded ``open -> in_progress -> closed`` with the direct hop rejected: replay
    sends ``transition_issue_by_name("In Progress")`` THEN ``("Done")``, in that order.
    """
    tracker = _make_tracker(tmp_path, "tkt-walk", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(current_status="To Do")

    ok = _replay_transition()(client, "DIG-1", "tkt-walk", "Done")

    assert ok is True
    assert client.transitions == [("DIG-1", "In Progress"), ("DIG-1", "Done")]


# ── 2. Resume point matches in Jira-name space (no inverse map) ───────────────
def test_replay_resume_point_matches_in_jira_name_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get_issue`` reports ``In Progress``; replay resumes by matching that Jira NAME
    directly against the forward-mapped hop list and sends ONLY ``Done``.

    ``LOCAL_STATUS_TO_JIRA`` is non-injective, so the resume point must be found WITHOUT
    inverting it: comparison happens entirely in Jira-name space.
    """
    tracker = _make_tracker(tmp_path, "tkt-resume", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(current_status="In Progress")

    ok = _replay_transition()(client, "DIG-2", "tkt-resume", "Done")

    assert ok is True
    assert client.transitions == [("DIG-2", "Done")]


# ── 3. Duplicate Jira name anchors on the LAST occurrence ─────────────────────
def test_replay_duplicate_jira_name_anchors_on_last_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``blocked`` and ``in_progress`` both forward-map to ``In Progress``, so the hop
    list ``open, in_progress, blocked, closed`` -> ``To Do, In Progress, In Progress,
    Done`` has ``In Progress`` at TWO positions. A current status of ``In Progress`` must
    anchor on the LAST match (index 2) and replay only ``Done`` — anchoring on the first
    (index 1) would wrongly re-send ``In Progress``.
    """
    tracker = _make_tracker(tmp_path, "tkt-dup", ["open", "in_progress", "blocked", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(current_status="In Progress")

    ok = _replay_transition()(client, "DIG-3", "tkt-dup", "Done")

    assert ok is True
    assert client.transitions == [("DIG-3", "Done")]


# ── 4. Never invents an unrecorded hop ────────────────────────────────────────
def test_replay_never_invents_unrecorded_hops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded hops are ``open -> closed`` only (Jira ``To Do, Done``). Even though a
    real workflow might require an intermediate ``In Progress``, replay is GROUNDED: it
    replays only recorded hops, so it sends ``Done`` and NEVER the un-recorded
    ``In Progress`` (self-healing is out of scope).
    """
    tracker = _make_tracker(tmp_path, "tkt-noinvent", ["open", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(current_status="To Do")

    _replay_transition()(client, "DIG-4", "tkt-noinvent", "Done")

    sent = [name for _key, name in client.transitions]
    assert "In Progress" not in sent, f"replay invented an unrecorded hop: {sent!r}"
    assert client.transitions == [("DIG-4", "Done")]


# ── 5. Abort + drift on an illegal hop, over BOTH backends ────────────────────
@pytest.mark.parametrize(
    ("key", "hop_exc", "update_exc"),
    [
        pytest.param(
            "DIG-5C",
            RuntimeError("no transition reaches 'In Progress'"),
            RuntimeError("no transition reaches 'Done'"),
            id="cloud-runtimeerror",
        ),
        pytest.param(
            "DIG-5V",
            ValueError("no transition named 'In Progress' is available"),
            ValueError("no transition named 'Done' is available"),
            id="dc-valueerror",
        ),
        pytest.param(
            "DIG-5I",
            IllegalTransitionError("illegal transition from 'To Do' to 'In Progress'"),
            IllegalTransitionError("illegal transition from 'To Do' to 'Done'"),
            id="dc-illegaltransition",
        ),
    ],
)
def test_replay_aborts_and_drifts_on_illegal_hop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    hop_exc: BaseException,
    update_exc: BaseException,
) -> None:
    """A mid-chain hop rejection (Cloud ``RuntimeError``; DC ``ValueError`` /
    ``IllegalTransitionError``) aborts replay and falls through to the existing
    comment-fallback: ``update_one`` returns ``None`` (drift), and no hop AFTER the
    rejected one is attempted.

    Driven end-to-end through ``update_one`` so the observable is the real drift signal:
    the direct end-state hop (``update_issue`` with ``status``) fails first, routing into
    replay; replay resumes at ``To Do`` and its FIRST remaining hop (``In Progress``) is
    rejected, so ``Done`` must never be sent.
    """
    tracker = _make_tracker(tmp_path, "tkt-abort", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(
        current_status="To Do",
        hop_error=("In Progress", hop_exc),
        update_error=update_exc,
    )
    mutation = {
        "action": "update",
        "key": key,
        "fields": {"status": "Done"},
        "local_id": "tkt-abort",
    }

    result = update_one(mutation, client)

    assert result is None, "an aborted replay must drift via comment-fallback (result None)"
    sent = [name for _key, name in client.transitions]
    assert "Done" not in sent, f"no hop after the rejected one may be sent; got {sent!r}"
    assert client.transitions == [(key, "In Progress")]


# ── 6. recorded_status_hops orders by filename, forward-mapped ────────────────
def test_recorded_status_hops_orders_by_filename(tmp_path: Path) -> None:
    """``recorded_status_hops(local_id, tracker)`` returns the ``data.status`` targets in
    FILENAME (chronological) order, forward-mapped local -> Jira via
    ``LOCAL_STATUS_TO_JIRA``.

    The events are written in an order DIFFERENT from their timestamp prefixes (closed
    first, then open, then in_progress), so an implementation that returned
    insertion/mtime order would fail while a filename sort yields
    ``["To Do", "In Progress", "Done"]``.
    """
    tracker = tmp_path / "tracker"
    ticket_dir = tracker / "tkt-order"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    # Deliberately out of order on disk: ts 300 (closed) written first.
    _write_status_event(ticket_dir, 300, target="closed", current="in_progress")
    _write_status_event(ticket_dir, 100, target="open", current="open")
    _write_status_event(ticket_dir, 200, target="in_progress", current="open")

    hops = _recorded_status_hops()("tkt-order", tracker)

    assert hops == ["To Do", "In Progress", "Done"]


# ── 7. Abort to drift when the current status is not in the sequence ──────────
def test_replay_aborts_when_current_status_not_in_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``get_issue`` reports a Jira status name absent from the recorded hop list
    (here ``In Review``, which no recorded hop maps to), replay cannot ground a resume
    point: it aborts (returns ``False``) and sends NO hops, leaving the ticket to drift.
    """
    tracker = _make_tracker(tmp_path, "tkt-missing", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(current_status="In Review")

    ok = _replay_transition()(client, "DIG-7", "tkt-missing", "Done")

    assert ok is False
    assert client.transitions == []


# ── 8. Replay skipped when the mutation carries no local_id ───────────────────
def test_replay_skipped_when_local_id_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mutation with no ``local_id`` has no recorded-hop provenance to replay, so replay
    is SKIPPED and the widened failure path behaves as today: comment-fallback + drift
    (``result is None``), with ``transition_issue_by_name`` never called.
    """
    # A populated tracker exists, but with NO local_id on the mutation it must not be used.
    tracker = _make_tracker(tmp_path, "tkt-unused", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(
        current_status="To Do",
        update_error=RuntimeError("no transition reaches 'Done'"),
    )
    mutation = {
        "action": "update",
        "key": "DIG-8",
        "fields": {"status": "Done"},
        # no "local_id"
    }

    result = update_one(mutation, client)

    assert result is None, "falsy local_id must still drift via comment-fallback"
    assert client.transitions == [], "replay must be skipped entirely when local_id absent"
