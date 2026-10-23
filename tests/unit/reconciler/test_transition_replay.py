"""Transition replay follows recorded status events when a direct Jira hop fails.

Use chronological STATUS events and forward-mapped Jira names to resume from
the current state. Never invent an unrecorded hop. A rejected hop falls back to
drift, while transient transport failures propagate. Tests observe transition
calls and the update result through the configured tracker-store seam."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from rebar.reconciler._errors import JiraAPIError, RetryExhaustedError
from rebar.reconciler.adapters.jira_datacenter.transitions import IllegalTransitionError
from rebar.reconciler.dispatch_one import update_one

pytestmark = pytest.mark.unit


# Import replay helpers inside tests so a missing helper fails at its call site
# without stopping collection of the other replay oracles.
def _replay_transition():
    from rebar.reconciler.transition_replay import replay_transition

    return replay_transition


def _recorded_status_hops():
    from rebar.reconciler.transition_replay import recorded_status_hops

    return recorded_status_hops


def _replay_should_drift():
    from rebar.reconciler.transition_replay import replay_should_drift

    return replay_should_drift


# ── On-disk STATUS-event store, byte-shaped like rebar._commands.txn writes ───
def _write_status_event(ticket_dir: Path, ts: int, target: str, current: str) -> None:
    """Append a timestamped STATUS event with the target local status and recorded prior status."""
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
    """Write local status hops in order under the ticket tracker directory."""
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
    """Point the reconciler tracker-directory resolver at the test store."""
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(tracker))
    monkeypatch.setenv("REBAR_ROOT", str(tracker.parent))


# ── Fake transports (record calls; raise the real per-backend rejection types) ─
class _FakeTransport:
    """Record transition calls and expose configured Jira status and hop failures."""

    def __init__(
        self,
        *,
        current_status: str,
        hop_error: tuple[str, BaseException] | None = None,
        update_error: BaseException | None = None,
        get_issue_error: BaseException | None = None,
        snapshot_override: dict[str, Any] | None = None,
    ) -> None:
        self._current_status = current_status
        self._hop_error = hop_error
        self._update_error = update_error
        self._get_issue_error = get_issue_error
        self._snapshot_override = snapshot_override
        self.transitions: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []

    def get_issue(self, remote_id: str) -> dict[str, Any]:
        if self._get_issue_error is not None:
            raise self._get_issue_error
        if self._snapshot_override is not None:
            return self._snapshot_override
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
    """Resume on forward-mapped Jira status and send only the remaining Done hop."""
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
    """When two local hops map to In Progress, resume after its last recorded occurrence."""
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
    """A direct open-to-closed trail cannot invent an intermediate In Progress hop."""
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
    """A rejected intermediate hop aborts replay and leaves update_one in drift."""
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
    """Read STATUS targets in filename order and forward-map each local status to Jira."""
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
    """Abort replay when the current Jira status matches no recorded forward-mapped hop."""
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
    """Without a local id, skip replay and retain the comment-fallback drift result."""
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


# ── 9. BLOCKING: successful replay drives update_one to report the update landed ──
def test_update_one_reports_landed_when_replay_reaches_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed direct hop lands only if recorded replay reaches its target."""
    tracker = _make_tracker(tmp_path, "tkt-e2e", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(
        current_status="To Do",
        update_error=RuntimeError("no transition reaches 'Done'"),
    )
    fields_synced: dict[str, Any] = {}
    mutation = {
        "action": "update",
        "key": "DIG-E2E",
        "fields": {"status": "Done"},
        "local_id": "tkt-e2e",
    }

    result = update_one(mutation, client, fields_synced=fields_synced)

    assert result == {"key": "DIG-E2E"}, "a completed replay must report the update landed"
    assert fields_synced == {"status": "Done"}, "the replayed status field must be reported applied"
    assert client.transitions == [("DIG-E2E", "In Progress"), ("DIG-E2E", "Done")]
    assert client.comments == [], "a landed replay must NOT post a drift comment"


# ── 10. Zero-transition replay (resume point is the last hop) is NOT landed ────
def test_replay_not_landed_when_remaining_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty remaining trail performs no transition and cannot report a landed replay."""
    tracker = _make_tracker(tmp_path, "tkt-last", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(current_status="Done")

    ok = _replay_transition()(client, "DIG-10", "tkt-last", "Done")

    assert ok is False, "a zero-transition replay must NOT report the update landed"
    assert client.transitions == []


def test_update_one_drifts_when_replay_performs_zero_transitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed direct update with zero replay transitions remains drift, not success."""
    tracker = _make_tracker(tmp_path, "tkt-last-e2e", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(
        current_status="Done",
        update_error=RuntimeError("no transition reaches 'Done'"),
    )
    mutation = {
        "action": "update",
        "key": "DIG-10E",
        "fields": {"status": "Done"},
        "local_id": "tkt-last-e2e",
    }

    result = update_one(mutation, client)

    assert result is None, "a zero-transition replay must drift, not report landed"
    assert client.transitions == []


# ── 11. Recorded trail whose final hop is NOT the target is NOT landed ─────────
def test_replay_not_landed_when_final_hop_is_not_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay cannot report landed if its last recorded hop differs from the desired status."""
    tracker = _make_tracker(tmp_path, "tkt-shorttrail", ["open", "in_progress"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(current_status="To Do")

    ok = _replay_transition()(client, "DIG-11", "tkt-shorttrail", "Done")

    assert ok is False, "replay must not report landed when the recorded trail never reaches target"
    sent = [name for _key, name in client.transitions]
    assert "Done" not in sent


# ── 12. A corrupt STATUS event is surfaced, not silently swallowed ────────────
def test_recorded_status_hops_surfaces_corrupt_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupt STATUS event raises instead of silently shortening the replay trail."""
    tracker = _make_tracker(tmp_path, "tkt-corrupt", ["open", "in_progress"])
    ticket_dir = tracker / "tkt-corrupt"
    bad = ticket_dir / f"{999:020d}-uuidcorrupt-STATUS.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")

    hops = _recorded_status_hops()("tkt-corrupt", tracker)

    captured = capsys.readouterr()
    assert bad.name in captured.err, (
        "a corrupt STATUS file must be surfaced, not silently swallowed"
    )
    # The readable events still forward-map; only the unreadable one is dropped (with a warning).
    assert hops == ["To Do", "In Progress"]


# ── 13. replay_should_drift honours a threaded (pre-parsed) hop trail ──────────
def test_replay_should_drift_uses_threaded_hops() -> None:
    """Reuse parsed recorded hops for the drift decision without reading the store again."""
    should_drift = _replay_should_drift()

    assert should_drift("tkt-any", hops=["To Do", "Done"]) is True
    assert should_drift("tkt-any", hops=[]) is False
    assert should_drift(None, hops=["To Do", "Done"]) is True


# ── 14. Original transition error propagates when replay/drift do not apply ────
def test_update_one_propagates_original_error_without_recorded_trail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a recorded trail, a bare transition error propagates from update_one."""
    tracker = tmp_path / "tracker"
    (tracker / "tkt-notrail").mkdir(parents=True, exist_ok=True)  # ticket dir, no STATUS events
    _point_config_at(monkeypatch, tracker)
    original = RuntimeError("no transition reaches 'Done'")
    client = _FakeTransport(current_status="To Do", update_error=original)
    mutation = {
        "action": "update",
        "key": "DIG-14",
        "fields": {"status": "Done"},
        "local_id": "tkt-notrail",
    }

    with pytest.raises(RuntimeError) as excinfo:
        update_one(mutation, client)

    assert excinfo.value is original, "the ORIGINAL transition error must propagate unchanged"
    assert client.transitions == []


# ── 15. Non-transition HTTP/API errors PROPAGATE (never routed into replay/drift) ──
@pytest.mark.parametrize(
    ("key", "update_exc"),
    [
        pytest.param(
            "DIG-15A",
            JiraAPIError("field 'customfield_10001' is required", status_code=400),
            id="non-illegal-400",
        ),
        pytest.param(
            "DIG-15B",
            JiraAPIError("issue does not exist", status_code=404),
            id="stale-binding-404",
        ),
    ],
)
def test_non_transition_http_error_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    update_exc: JiraAPIError,
) -> None:
    """Field 400 and stale-binding 404 failures propagate without replay drift."""
    tracker = _make_tracker(tmp_path, "tkt-15", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(current_status="To Do", update_error=update_exc)
    mutation = {
        "action": "update",
        "key": key,
        "fields": {"status": "Done"},
        "local_id": "tkt-15",
    }

    with pytest.raises(JiraAPIError) as excinfo:
        update_one(mutation, client)

    assert excinfo.value is update_exc, "the original non-transition error must propagate unchanged"
    assert client.transitions == [], "a propagating error must NOT drive any replay hop"
    assert client.comments == [], "a propagating error must NOT post a drift comment"


def test_5xx_http_error_propagates_not_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exhausted HTTP 500 retry re-raises the original HTTPError instead of recording drift."""
    import rebar.reconciler.dispatch_apply_phases as dap

    monkeypatch.setattr(dap.time, "sleep", lambda *_a, **_k: None)
    tracker = _make_tracker(tmp_path, "tkt-5xx", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    err = urllib.error.HTTPError("http://x", 500, "Internal Server Error", {}, None)
    client = _FakeTransport(current_status="To Do", update_error=err)
    mutation = {
        "action": "update",
        "key": "DIG-5XX",
        "fields": {"status": "Done"},
        "local_id": "tkt-5xx",
    }

    with pytest.raises(urllib.error.HTTPError):
        update_one(mutation, client)

    assert client.transitions == [], "a 5xx must NOT drive any replay hop"
    assert client.comments == [], "a 5xx must NOT post a drift comment"


# ── 16. RetryExhaustedError (transient exhaustion) PROPAGATES, never drifts ────
def test_retry_exhausted_error_from_update_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient update exhausted by retry raises RetryExhaustedError, not replay drift."""
    tracker = _make_tracker(tmp_path, "tkt-retry", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    exhausted = RetryExhaustedError("all retries exhausted", attempts=4)
    client = _FakeTransport(current_status="To Do", update_error=exhausted)
    mutation = {
        "action": "update",
        "key": "DIG-16",
        "fields": {"status": "Done"},
        "local_id": "tkt-retry",
    }

    with pytest.raises(RetryExhaustedError) as excinfo:
        update_one(mutation, client)

    assert excinfo.value is exhausted, "the transient-exhaustion error must propagate unchanged"
    assert client.transitions == [], "a transient exhaustion must NOT drive any replay hop"
    assert client.comments == [], "a transient exhaustion must NOT post a drift comment"


def test_replay_reraises_retry_exhausted_error_from_hop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry exhaustion in a replay hop propagates rather than becoming drift."""
    tracker = _make_tracker(tmp_path, "tkt-hopretry", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    exhausted = RetryExhaustedError("hop retries exhausted", attempts=4)
    client = _FakeTransport(current_status="To Do", hop_error=("In Progress", exhausted))

    with pytest.raises(RetryExhaustedError) as excinfo:
        _replay_transition()(client, "DIG-16H", "tkt-hopretry", "Done")

    assert excinfo.value is exhausted
    # The rejected hop was attempted; no hop AFTER it runs (replay unwinds by propagation).
    assert client.transitions == [("DIG-16H", "In Progress")]


# ── 17. Malformed / absent snapshot -> safe drift, never a crash ──────────────
def test_replay_drifts_on_malformed_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot without a usable status aborts replay to drift without a parsing exception."""
    tracker = _make_tracker(tmp_path, "tkt-malformed", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(
        current_status="To Do", snapshot_override={"key": "DIG-17", "fields": {}}
    )

    ok = _replay_transition()(client, "DIG-17", "tkt-malformed", "Done")

    assert ok is False, "a malformed snapshot must abort to drift, not crash"
    assert client.transitions == []


def test_replay_drifts_when_get_issue_raises_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A status read returning 404 aborts replay to drift without inventing a hop."""
    tracker = _make_tracker(tmp_path, "tkt-getfail", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)
    client = _FakeTransport(
        current_status="To Do",
        get_issue_error=JiraAPIError("issue does not exist", status_code=404),
    )

    ok = _replay_transition()(client, "DIG-17B", "tkt-getfail", "Done")

    assert ok is False, "a get_issue failure must abort to drift, not crash"
    assert client.transitions == []


# ── 18. Illegal-transition 400 DRIFTS even without a recorded trail ───────────
def test_illegal_transition_400_drifts_without_recorded_trail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An illegal-transition 400 with no STATUS trail remains drift."""
    tracker = tmp_path / "tracker"
    (tracker / "tkt-notrail-400").mkdir(parents=True, exist_ok=True)  # dir exists, no STATUS events
    _point_config_at(monkeypatch, tracker)
    illegal = JiraAPIError("illegal transition from 'To Do' to 'Done'", status_code=400)
    client = _FakeTransport(current_status="To Do", update_error=illegal)
    mutation = {
        "action": "update",
        "key": "DIG-18",
        "fields": {"status": "Done"},
        "local_id": "tkt-notrail-400",
    }

    result = update_one(mutation, client)

    assert result is None, "an illegal-400 must drift even with no recorded trail to replay"
    assert client.transitions == [], "no replay hop is possible with an empty trail"


# ── 19. In-scope helper coverage: recorded_hops_for via the self-resolved store ──
def test_recorded_hops_for_reads_self_resolved_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read hops from the configured store for a local id and return an empty trail without one."""
    from rebar.reconciler.transition_replay import recorded_hops_for

    tracker = _make_tracker(tmp_path, "tkt-hopsfor", ["open", "in_progress", "closed"])
    _point_config_at(monkeypatch, tracker)

    assert recorded_hops_for("tkt-hopsfor") == ["To Do", "In Progress", "Done"]
    assert recorded_hops_for(None) == []
    assert recorded_hops_for("") == []
