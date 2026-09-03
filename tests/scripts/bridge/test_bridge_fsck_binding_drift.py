"""bridge fsck binding-level drift audit (epic 3006-e198, child 8de5).

The pre-fix ``audit_bridge_mappings`` walked local event dirs ONLY (orphan /
duplicate / stale SYNC) and was structurally blind to binding-level drift: a
confirmed binding whose local ticket is archived/deleted, a binding whose local
ticket vanished, or a live/retired overlap. This asserts the new offline arm —
the SECOND consumer of the one convergence classifier — reads bindings.json and
flags them (the old checks return clean over the same store: RED before the arm).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def fsck() -> ModuleType:
    from rebar._engine_support import bridge_fsck

    return bridge_fsck


def _write_bindings(tracker: Path, bindings: dict, reverse: dict) -> None:
    state = tracker / ".bridge_state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "bindings.json").write_text(
        json.dumps({"version": 2, "bindings": bindings, "reverse": reverse})
    )


def _confirmed(jira_key: str) -> dict:
    return {"jira_key": jira_key, "state": "confirmed"}


def _init_tickets_repo(tracker: Path) -> None:
    tracker.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(tracker), "init", "-q", "-b", "tickets"], check=True)
    subprocess.run(
        ["git", "-C", str(tracker), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(tracker), "config", "user.name", "Test"], check=True)


def _commit_known_event(tracker: Path) -> None:
    ticket_dir = tracker / "fixture-ticket"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "1-known-CREATE.json").write_text(
        json.dumps(
            {
                "event_type": "CREATE",
                "uuid": "11111111-1111-4111-8111-111111111111",
                "timestamp": 1,
                "author": "test",
                "env_id": "22222222-2222-4222-8222-222222222222",
                "data": {"ticket_type": "task", "title": "fixture", "parent_id": None},
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(tracker), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tracker), "commit", "-q", "-m", "fixture"], check=True)


@pytest.mark.unit
@pytest.mark.scripts
def test_binding_drift_flags_archived_and_deleted_and_gone(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(
        tracker,
        bindings={
            "loc-active": _confirmed("REB-1"),
            "loc-arch": _confirmed("REB-2"),
            "loc-del": _confirmed("REB-3"),
            "loc-gone": _confirmed("REB-4"),
        },
        reverse={
            "REB-1": "loc-active",
            "REB-2": "loc-arch",
            "REB-3": "loc-del",
            "REB-4": "loc-gone",
        },
    )
    # Injected local states (identical shape to reduce_all_tickets output).
    local_states = [
        {"ticket_id": "loc-active", "status": "in_progress", "archived": False},
        {"ticket_id": "loc-arch", "status": "open", "archived": True},
        {"ticket_id": "loc-del", "status": "deleted", "archived": True},
        # loc-gone: deliberately ABSENT from the reduced store.
    ]
    drift = fsck.audit_binding_drift(tracker, local_states=local_states)

    wt = {e["local_id"] for e in drift["would_terminal"]}
    assert wt == {"loc-arch", "loc-del"}, drift
    lg = {e["local_id"] for e in drift["local_gone"]}
    assert lg == {"loc-gone"}, drift
    # The active binding is NOT drift (needs Jira to decide field-level sync).
    assert "loc-active" not in wt and "loc-active" not in lg


@pytest.mark.unit
@pytest.mark.scripts
def test_binding_drift_flags_live_retired_overlap(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(tracker, bindings={"loc-1": _confirmed("REB-9")}, reverse={"REB-9": "loc-1"})
    state = tracker / ".bridge_state"
    (state / "bindings-retired.json").write_text(
        json.dumps({"version": 1, "retired": {"REB-9": {"local_id": "loc-1"}}})
    )
    drift = fsck.audit_binding_drift(
        tracker, local_states=[{"ticket_id": "loc-1", "status": "open", "archived": False}]
    )
    assert [e["jira_key"] for e in drift["retired_overlap"]] == ["REB-9"]


@pytest.mark.unit
@pytest.mark.scripts
def test_no_snapshot_skips_jira_requiring_cells(fsck, tmp_path):
    # Without a Jira snapshot artifact, dangling/unbound_jira cannot be decided.
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(tracker, bindings={"loc-1": _confirmed("REB-1")}, reverse={"REB-1": "loc-1"})
    drift = fsck.audit_binding_drift(
        tracker,
        local_states=[{"ticket_id": "loc-1", "status": "open", "archived": False}],
        use_prev_snapshot=False,
    )
    assert drift["dangling"] == []
    assert drift["unbound_jira"] == []


@pytest.mark.unit
@pytest.mark.scripts
def test_absent_from_snapshot_without_confirmed_404_is_not_dangling(fsck, tmp_path):
    # ADR 0028 §1 (bug f436) — a confirmed binding merely absent from the windowed
    # snapshot is NOT a deletion candidate: the offline audit never probed it.
    # (Historically this exact case was wrongly reported as ``dangling``.) With no
    # persisted confirmed-404 state it is surfaced only informationally.
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(tracker, bindings={"f8b5": _confirmed("REB-530")}, reverse={"REB-530": "f8b5"})
    drift = fsck.audit_binding_drift(
        tracker,
        local_states=[{"ticket_id": "f8b5", "status": "in_progress", "archived": False}],
        jira_snapshot={},  # REB-530 absent from the window — un-probed, not gone
    )
    assert drift["dangling"] == []
    assert drift["absent_in_window_unprobed"] == [{"local_id": "f8b5", "jira_key": "REB-530"}]


@pytest.mark.unit
@pytest.mark.scripts
def test_alive_out_of_window_binding_is_not_dangling(fsck, tmp_path):
    # ADR 0028 §1/§2 regression (bug f436): the reconciler snapshot is
    # DELIBERATELY windowed — a Done binding aged out beyond the Done-recent cap
    # is ALIVE in Jira but intentionally absent from prev_snapshot.json. The
    # OFFLINE audit has no Jira client and never probes, so it must NOT report
    # snapshot-window absence as a deletion candidate (``dangling``) — else an
    # alive aged-out binding is reported dangling every pass forever (unhealable).
    #
    # Synthetic store: TWO confirmed bindings; the windowed snapshot contains
    # only ONE (the other aged out of the Done-recent window while still alive).
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(
        tracker,
        bindings={"loc-in": _confirmed("REB-100"), "loc-out": _confirmed("REB-200")},
        reverse={"REB-100": "loc-in", "REB-200": "loc-out"},
    )
    drift = fsck.audit_binding_drift(
        tracker,
        local_states=[
            {"ticket_id": "loc-in", "status": "in_progress", "archived": False},
            {"ticket_id": "loc-out", "status": "in_progress", "archived": False},
        ],
        # Windowed snapshot: only REB-100 is inside the window; REB-200 aged out.
        jira_snapshot={"REB-100": {"status": "In Progress"}},
    )
    # The alive out-of-window binding must NOT be dangling (un-probed absence is
    # not a deletion signal); it is surfaced only informationally.
    assert {e["jira_key"] for e in drift["dangling"]} == set(), drift
    assert {e["jira_key"] for e in drift["absent_in_window_unprobed"]} == {"REB-200"}, drift
    # The in-window binding is unaffected — present + bound + active → no drift.
    assert "REB-100" not in {e["jira_key"] for e in drift["absent_in_window_unprobed"]}


@pytest.mark.unit
@pytest.mark.scripts
def test_confirmed_404_state_sources_dangling(fsck, tmp_path):
    # Option 1 (ADR 0028 §2): ``dangling`` is sourced ONLY from persisted
    # confirmed-404 state that binding_store records on the binding entry
    # (``absent_404_count`` via note_absent). An out-of-window binding the live
    # pass HAS confirmed absent (count > 0) IS a real deletion candidate.
    tracker = tmp_path / ".tickets-tracker"
    entry = _confirmed("REB-300")
    entry["absent_404_count"] = 1
    _write_bindings(tracker, bindings={"loc-x": entry}, reverse={"REB-300": "loc-x"})
    drift = fsck.audit_binding_drift(
        tracker,
        local_states=[{"ticket_id": "loc-x", "status": "in_progress", "archived": False}],
        jira_snapshot={},  # REB-300 absent AND confirmed-404 → dangling
    )
    assert [e["jira_key"] for e in drift["dangling"]] == ["REB-300"], drift
    assert drift["absent_in_window_unprobed"] == [], drift


@pytest.mark.unit
@pytest.mark.scripts
def test_unbound_jira_native_flagged_with_snapshot(fsck, tmp_path):
    # AC1(c) — a Jira-native issue in the snapshot with no binding is unbound_jira.
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(tracker, bindings={"loc-1": _confirmed("REB-1")}, reverse={"REB-1": "loc-1"})
    drift = fsck.audit_binding_drift(
        tracker,
        local_states=[{"ticket_id": "loc-1", "status": "in_progress", "archived": False}],
        jira_snapshot={
            "REB-1": {"status": "In Progress"},
            "REB-532": {"status": "To Do"},  # native, unbound → adopt candidate
        },
    )
    assert drift["unbound_jira"] == [{"jira_key": "REB-532"}]
    assert drift["dangling"] == []  # REB-1 is present + bound + active → no drift


@pytest.mark.unit
@pytest.mark.scripts
def test_reconcile_check_compatibility_buckets_surface_in_fsck(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    entry = _confirmed("REB-404")
    entry["absent_404_count"] = 1
    _write_bindings(
        tracker,
        bindings={
            "loc-bound": _confirmed("REB-1"),
            "loc-gone": _confirmed("REB-2"),
            "loc-confirmed-absent": entry,
        },
        reverse={
            "REB-1": "loc-bound",
            "REB-2": "loc-gone",
            "REB-404": "loc-confirmed-absent",
        },
    )
    drift = fsck.audit_binding_drift(
        tracker,
        local_states=[
            {"ticket_id": "loc-bound", "status": "in_progress", "archived": False},
            {"ticket_id": "loc-unbound", "status": "open", "archived": False},
        ],
        jira_snapshot={
            "REB-1": {"status": "In Progress"},
            "REB-777": {
                "summary": "orphaned",
                "labels": ["team:platform", "rebar-id-lost-binding"],
            },
        },
    )

    assert drift["orphaned_bindings"] == [
        {"local_id": "loc-confirmed-absent", "jira_key": "REB-404", "reason": "confirmed_404"},
        {"local_id": "loc-gone", "jira_key": "REB-2", "reason": "local_gone"},
    ]
    assert drift["orphaned_jira"] == [{"jira_key": "REB-777"}]
    assert drift["unbound_local"] == [{"local_id": "loc-unbound"}]

    report = fsck._format_report(
        {"unknown_event_types": [], "binding_drift": drift, "store_integrity": []}
    )
    assert "orphaned_jira: jira_key=REB-777" in report
    assert "unbound_local: local=loc-unbound" in report


@pytest.mark.unit
@pytest.mark.scripts
def test_would_terminal_via_snapshot_when_jira_live(fsck, tmp_path):
    # An archived-local binding whose Jira is present + not Done → would_terminal.
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(
        tracker, bindings={"loc-a": _confirmed("REB-464")}, reverse={"REB-464": "loc-a"}
    )
    drift = fsck.audit_binding_drift(
        tracker,
        local_states=[{"ticket_id": "loc-a", "status": "archived", "archived": True}],
        jira_snapshot={"REB-464": {"status": "To Do"}},
    )
    assert drift["would_terminal"] == [{"local_id": "loc-a", "jira_key": "REB-464"}]
    assert drift["dangling"] == []


@pytest.mark.unit
@pytest.mark.scripts
def test_keyset_snapshot_marks_terminal_status_classification_indeterminate(fsck, tmp_path):
    """A key-only snapshot proves presence, but not Jira terminal status."""
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(
        tracker,
        bindings={"loc-a": _confirmed("REB-464")},
        reverse={"REB-464": "loc-a"},
    )

    drift = fsck.audit_binding_drift(
        tracker,
        local_states=[{"ticket_id": "loc-a", "status": "archived", "archived": True}],
        jira_snapshot={"REB-464": {}},
    )

    assert drift["would_terminal"] == []
    assert drift["indeterminate"] == [
        {
            "local_id": "loc-a",
            "jira_key": "REB-464",
            "reason": "jira status unavailable in key-set snapshot",
        }
    ]


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_fsck_schema_catalogs_indeterminate_drift(fsck):
    schema_path = Path(fsck.__file__).resolve().parents[1] / "schemas" / "bridge_fsck.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    indeterminate = schema["properties"]["binding_drift"]["properties"]["indeterminate"]
    assert indeterminate["type"] == "array"
    assert "informational; never alerts" in indeterminate["description"]


@pytest.mark.unit
@pytest.mark.scripts
def test_audit_bridge_mappings_includes_binding_drift_and_sets_exit(fsck, tmp_path):
    # The offline event-scan checks return clean, but binding_drift is non-empty →
    # the finding surfaces AND main() exits non-zero (class-D blindness healed).
    tracker = tmp_path / ".tickets-tracker"
    _init_tickets_repo(tracker)
    _commit_known_event(tracker)
    _write_bindings(
        tracker,
        bindings={"loc-arch": _confirmed("REB-2")},
        reverse={"REB-2": "loc-arch"},
    )
    # No local ticket dirs at all → reduce yields nothing → loc-arch is local_gone.
    findings = fsck.audit_bridge_mappings(tracker)
    assert "binding_drift" in findings
    drift = findings["binding_drift"]
    assert drift["local_gone"] == [{"local_id": "loc-arch", "jira_key": "REB-2"}]
    assert findings["unknown_event_types"] == []
    assert findings["store_integrity"] == []

    # The report renders the section; main() exits 1 on drift.
    report = fsck._format_report(findings)
    assert "Binding-Level Drift" in report
    rc = fsck.main(["--tickets-tracker", str(tracker), "--output", "json"])
    assert rc == 1


@pytest.mark.unit
@pytest.mark.scripts
def test_audit_bridge_mappings_degrades_binding_drift_failure(fsck, tmp_path, monkeypatch):
    tracker = tmp_path / ".tickets-tracker"
    _init_tickets_repo(tracker)
    _commit_known_event(tracker)

    def fail_binding_drift(_tracker):
        raise ValueError("corrupt drift input")

    monkeypatch.setattr(fsck, "audit_binding_drift", fail_binding_drift)

    findings = fsck.audit_bridge_mappings(tracker)

    assert findings == {
        "unknown_event_types": [],
        "binding_drift": fsck._empty_binding_drift(),
        "store_integrity": [],
    }


@pytest.mark.unit
@pytest.mark.scripts
def test_no_bindings_store_is_clean(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    drift = fsck.audit_binding_drift(tracker, local_states=[])
    assert drift == fsck._empty_binding_drift()


# ---------------------------------------------------------------------------
# Ticket 030f held-out oracle: committed-ref scanning and index integrity.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_unknown_event_scan_accepts_spacing_and_rejects_nested_text(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    _init_tickets_repo(tracker)
    ticket_dir = tracker / "future-ticket"
    ticket_dir.mkdir()
    base = {
        "uuid": "33333333-3333-4333-8333-333333333333",
        "timestamp": 2,
        "author": "future",
        "env_id": "22222222-2222-4222-8222-222222222222",
        "data": {},
    }
    compact = {"event_type": "FUTURE_COMPACT", **base}
    spaced = {"event_type": "FUTURE_SPACED", **base}
    nested = {
        "event_type": "CREATE",
        **base,
        "data": {
            "note": 'text containing "event_type":"TEXT_ONLY"',
            "nested": {"event_type": "NESTED_ONLY"},
        },
    }
    alert = {"event_type": "BRIDGE_ALERT", **base}
    (ticket_dir / "1-compact.json").write_text(
        json.dumps(compact, separators=(",", ":")), encoding="utf-8"
    )
    (ticket_dir / "2-spaced.json").write_text(json.dumps(spaced), encoding="utf-8")
    (ticket_dir / "3-nested.json").write_text(json.dumps(nested), encoding="utf-8")
    (ticket_dir / "4-alert.json").write_text(json.dumps(alert), encoding="utf-8")
    subprocess.run(["git", "-C", str(tracker), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tracker), "commit", "-q", "-m", "future fixtures"], check=True)
    # Prove the production path reads the committed ref, not the checked-out tree:
    # the blobs remain reachable from refs/heads/tickets but are absent on disk.
    subprocess.run(["git", "-C", str(tracker), "rm", "-q", "-r", "future-ticket"], check=True)

    findings = fsck.audit_bridge_mappings(tracker)

    assert findings["unknown_event_types"] == ["FUTURE_COMPACT", "FUTURE_SPACED"]


@pytest.mark.unit
@pytest.mark.scripts
def test_store_integrity_reports_the_six_exact_contract_kinds(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    _init_tickets_repo(tracker)
    _commit_known_event(tracker)
    _write_bindings(
        tracker,
        bindings={
            "missing-key": {"state": "confirmed"},
            "missing-reverse": _confirmed("REB-2"),
            "mismatch-source": _confirmed("REB-3"),
            "mismatch-target": _confirmed("REB-3"),
            "pending": {"state": "pending", "jira_key": "REB-5"},
            "jira-mismatch": _confirmed("REB-X"),
        },
        reverse={
            "REB-3": "mismatch-target",
            "REB-4": "absent-local",
            "REB-5": "pending",
            "REB-6": "jira-mismatch",
            "REB-X": "jira-mismatch",
        },
    )

    findings = fsck.audit_bridge_mappings(tracker)["store_integrity"]

    assert sorted(findings, key=lambda item: item["kind"]) == sorted(
        [
            {"kind": "forward_missing_jira_key", "local_id": "missing-key"},
            {
                "kind": "forward_missing_reverse",
                "local_id": "missing-reverse",
                "jira_key": "REB-2",
            },
            {
                "kind": "forward_reverse_mismatch",
                "local_id": "mismatch-source",
                "jira_key": "REB-3",
                "actual_local_id": "mismatch-target",
            },
            {
                "kind": "reverse_missing_forward",
                "local_id": "absent-local",
                "jira_key": "REB-4",
            },
            {
                "kind": "reverse_nonconfirmed_forward",
                "local_id": "pending",
                "jira_key": "REB-5",
            },
            {
                "kind": "reverse_jira_key_mismatch",
                "local_id": "jira-mismatch",
                "jira_key": "REB-6",
                "forward_jira_key": "REB-X",
            },
        ],
        key=lambda item: item["kind"],
    )


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.parametrize(
    ("raw_local_id", "rendered_local_id"),
    [
        (42, "42"),
        (None, "null"),
        ({"bad": 1}, '{"bad":1}'),
    ],
)
def test_store_integrity_keeps_corrupt_reverse_values_schema_valid(
    fsck, tmp_path, raw_local_id, rendered_local_id
):
    from rebar import schemas

    tracker = tmp_path / ".tickets-tracker"
    _init_tickets_repo(tracker)
    _commit_known_event(tracker)
    _write_bindings(
        tracker,
        bindings={"known": _confirmed("REB-1")},
        reverse={"REB-1": raw_local_id},
    )

    result = fsck.audit_bridge_mappings(tracker)

    schemas.validator(schemas.BRIDGE_FSCK).validate(result)
    assert result["store_integrity"] == [
        {
            "kind": "forward_reverse_mismatch",
            "local_id": "known",
            "jira_key": "REB-1",
            "actual_local_id": rendered_local_id,
        },
        {
            "kind": "reverse_missing_forward",
            "local_id": rendered_local_id,
            "jira_key": "REB-1",
        },
    ]


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.parametrize(
    ("raw_jira_key", "rendered_jira_key"),
    [
        (None, None),
        (42, "42"),
        (["bad"], '["bad"]'),
        ({"bad": 1}, '{"bad":1}'),
    ],
)
def test_store_integrity_keeps_corrupt_forward_jira_keys_schema_valid(
    fsck, tmp_path, raw_jira_key, rendered_jira_key
):
    from rebar import schemas

    tracker = tmp_path / ".tickets-tracker"
    _init_tickets_repo(tracker)
    _commit_known_event(tracker)
    entry = _confirmed("REB-1")
    entry["jira_key"] = raw_jira_key
    _write_bindings(
        tracker,
        bindings={"known": entry},
        reverse={"REB-1": "known"},
    )

    result = fsck.audit_bridge_mappings(tracker)

    schemas.validator(schemas.BRIDGE_FSCK).validate(result)
    assert result["store_integrity"] == [
        {"kind": "forward_missing_jira_key", "local_id": "known"},
        {
            "kind": "reverse_jira_key_mismatch",
            "local_id": "known",
            "jira_key": "REB-1",
            "forward_jira_key": rendered_jira_key,
        },
    ]


@pytest.mark.unit
@pytest.mark.scripts
def test_pending_forward_without_reverse_is_valid(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    _init_tickets_repo(tracker)
    _commit_known_event(tracker)
    _write_bindings(
        tracker,
        bindings={"pending": {"state": "pending", "jira_key": "REB-P"}},
        reverse={},
    )

    assert fsck.audit_bridge_mappings(tracker)["store_integrity"] == []


@pytest.mark.unit
@pytest.mark.scripts
def test_corrupt_binding_store_is_an_operational_error(fsck, tmp_path, capsys):
    from rebar import RebarError

    tracker = tmp_path / ".tickets-tracker"
    _init_tickets_repo(tracker)
    _commit_known_event(tracker)
    state = tracker / ".bridge_state"
    state.mkdir(exist_ok=True)
    (state / "bindings.json").write_text(
        "<<<<<<< local\n{}\n=======\n{}\n>>>>>>> remote\n",
        encoding="utf-8",
    )

    with pytest.raises(RebarError) as raised:
        fsck.audit_bridge_mappings(tracker)

    assert raised.value.returncode == 2
    assert "bindings.json" in str(raised.value)

    rc = fsck.main(["--tickets-tracker", str(tracker), "--output", "json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "bindings.json" in captured.err


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bindings", []),
        ("reverse", "corrupt"),
    ],
)
def test_non_object_binding_indexes_are_operational_errors(fsck, tmp_path, field, value):
    from rebar import RebarError

    tracker = tmp_path / ".tickets-tracker"
    _init_tickets_repo(tracker)
    _commit_known_event(tracker)
    state = tracker / ".bridge_state"
    state.mkdir(exist_ok=True)
    store = {"version": 2, "bindings": {}, "reverse": {}}
    store[field] = value
    (state / "bindings.json").write_text(json.dumps(store), encoding="utf-8")

    with pytest.raises(RebarError) as raised:
        fsck.audit_bridge_mappings(tracker)

    assert raised.value.returncode == 2
    assert field in str(raised.value)


@pytest.mark.unit
@pytest.mark.scripts
def test_absent_reverse_index_keeps_legacy_empty_index_semantics(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    _init_tickets_repo(tracker)
    _commit_known_event(tracker)
    state = tracker / ".bridge_state"
    state.mkdir(exist_ok=True)
    (state / "bindings.json").write_text(
        json.dumps({"version": 1, "bindings": {"known": _confirmed("REB-1")}}),
        encoding="utf-8",
    )

    findings = fsck.audit_bridge_mappings(tracker)

    assert findings["store_integrity"] == [
        {"kind": "forward_missing_reverse", "local_id": "known", "jira_key": "REB-1"}
    ]


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.parametrize(
    ("mode", "diagnostic"),
    [
        ("missing_git", "git is unavailable"),
        ("malformed_grep", "malformed"),
        ("grep_failure", "git grep"),
        ("grep_timeout", "timed out"),
        ("candidate_failure", "candidate"),
        ("candidate_timeout", "timed out"),
    ],
)
def test_scan_failures_raise_public_rebar_error(
    fsck, tmp_path, monkeypatch: pytest.MonkeyPatch, mode: str, diagnostic: str
):
    import rebar

    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()

    def fake_run_git(cwd, *args, **kwargs):
        del cwd
        timeout = kwargs.get("timeout")
        if mode == "missing_git":
            raise FileNotFoundError("git")
        if mode == "grep_timeout" and args[0] == "grep":
            raise subprocess.TimeoutExpired(args, timeout)
        if mode == "candidate_timeout" and args[0] == "show":
            raise subprocess.TimeoutExpired(args, timeout)
        if args[0] == "grep":
            if mode == "malformed_grep":
                return subprocess.CompletedProcess(args, 0, "not-a-grep-record\n", "")
            if mode == "grep_failure":
                return subprocess.CompletedProcess(args, 2, "", "fatal: grep failed")
            return subprocess.CompletedProcess(
                args,
                0,
                'refs/heads/tickets:future/1.json:1:"event_type":"FUTURE"\n',
                "",
            )
        return subprocess.CompletedProcess(args, 128, "", "fatal: blob unreadable")

    monkeypatch.setattr(fsck, "run_git", fake_run_git, raising=False)
    with pytest.raises(rebar.RebarError) as raised:
        fsck.audit_bridge_mappings(tracker)

    assert raised.value.returncode == 2
    assert diagnostic in str(raised.value).lower()


@pytest.mark.unit
@pytest.mark.scripts
def test_missing_tickets_ref_is_operational_error_and_cli_exit_two(
    fsck, tmp_path, capsys: pytest.CaptureFixture[str]
):
    import rebar

    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    subprocess.run(["git", "-C", str(tracker), "init", "-q", "-b", "main"], check=True)

    with pytest.raises(rebar.RebarError) as raised:
        fsck.audit_bridge_mappings(tracker)
    assert raised.value.returncode == 2
    assert "tickets" in str(raised.value).lower()

    rc = fsck.main(["--tickets-tracker", str(tracker), "--output", "json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "tickets" in captured.err.lower()


@pytest.mark.unit
@pytest.mark.scripts
def test_store_integrity_gates_exit_but_unknown_types_do_not(
    fsck, tmp_path, capsys: pytest.CaptureFixture[str]
):
    tracker = tmp_path / ".tickets-tracker"
    _init_tickets_repo(tracker)
    ticket_dir = tracker / "future-ticket"
    ticket_dir.mkdir()
    (ticket_dir / "1-future.json").write_text(
        json.dumps({"event_type": "FUTURE_ONLY"}), encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(tracker), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tracker), "commit", "-q", "-m", "future fixture"], check=True)

    assert fsck.main(["--tickets-tracker", str(tracker), "--output", "json"]) == 0
    capsys.readouterr()

    _write_bindings(
        tracker,
        bindings={"broken": _confirmed("REB-BROKEN")},
        reverse={},
    )
    assert fsck.main(["--tickets-tracker", str(tracker), "--output", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["store_integrity"] == [
        {
            "kind": "forward_missing_reverse",
            "local_id": "broken",
            "jira_key": "REB-BROKEN",
        }
    ]


@pytest.mark.unit
@pytest.mark.scripts
def test_human_report_advertises_only_live_checks(fsck, tmp_path, capsys):
    tracker = tmp_path / ".tickets-tracker"
    _init_tickets_repo(tracker)
    _commit_known_event(tracker)

    assert fsck.main(["--tickets-tracker", str(tracker)]) == 0
    report = capsys.readouterr().out
    lowered = report.lower()
    for phantom in ("orphan", "duplicate", "stale sync", "unresolved alert"):
        assert phantom not in lowered
    assert "store integrity" in lowered
