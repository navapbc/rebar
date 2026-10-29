"""Store-wide reads discard unwanted tickets inside the reduce loop.

``reduce_all_tickets`` used to apply its exclusions after the loop, and its callers
filtered only after it returned, so every ticket about to be discarded was held in
memory until then (bug humdrum-priestly-lcont). These tests pin two things:

* retention: a ticket the exclusions or a caller's ``keep`` predicate discard is no
  longer referenced once the loop moves on;
* output equality: each moved caller returns exactly what the old path returned, on
  a store holding archived, deleted, session_log, code_review and corrupt tickets.
  The old path is reproduced by running the same caller with the new
  ``keep``/``keep_fields`` arguments stripped, which is what each caller did before
  (its post-loop filters are kept), plus explicit expected ids.
"""

from __future__ import annotations

import gc
import json
import weakref
from pathlib import Path
from typing import Any

import pytest

import rebar.reducer as reducer_pkg
from rebar._engine_support import bridge_fsck_drift, reads, validate
from rebar._engine_support.ticket_query import TicketQuery
from rebar.graph import _graph
from rebar.graph import _loader as graph_loader
from rebar.reducer import _api

pytestmark = pytest.mark.unit

_ENV = "00000000-0000-4000-8000-000000000001"


class _Event:
    """Monotonic event writer so every ticket's events replay in order."""

    def __init__(self) -> None:
        self.ts = 1_790_000_000_000_000_000
        self.n = 0

    def write(self, tdir: Path, event_type: str, data: dict[str, Any]) -> None:
        tdir.mkdir(parents=True, exist_ok=True)
        self.ts += 1_000
        self.n += 1
        uuid = f"00000000-0000-4000-8000-{self.n:012d}"
        payload = {
            "timestamp": self.ts,
            "uuid": uuid,
            "event_type": event_type,
            "env_id": _ENV,
            "author": "Test User",
            "data": data,
        }
        (tdir / f"{self.ts}-{uuid}-{event_type}.json").write_text(json.dumps(payload))


def _tid(n: int) -> str:
    return f"aaaa-0000-0000-{n:04d}"


EPIC, TASK_OPEN, TASK_CLOSED, BUG_BLOCKED, BLOCKER = (_tid(i) for i in range(1, 6))
ARCHIVED, DELETED, STATUS_DELETED = _tid(6), _tid(7), _tid(8)
LOG_OLD, LOG_NEW, LOG_ARCHIVED, REVIEW, CORRUPT = (_tid(i) for i in range(9, 14))
IN_PROGRESS = _tid(14)


@pytest.fixture
def tracker(tmp_path: Path) -> str:
    """A store with every ticket shape the moved reads must treat as before."""
    root = tmp_path / "tracker"
    ev = _Event()

    def create(tid: str, ttype: str, title: str, **extra: Any) -> Path:
        tdir = root / tid
        ev.write(tdir, "CREATE", {"ticket_type": ttype, "title": title, **extra})
        return tdir

    def status(tdir: Path, new: str, cur: str = "open") -> None:
        ev.write(tdir, "STATUS", {"status": new, "current_status": cur})

    create(EPIC, "epic", "Memory epic", description="parent of the memory tasks")
    t = create(TASK_OPEN, "task", "Trim memory in search", parent_id=EPIC)
    ev.write(t, "COMMENT", {"body": "memory note"})
    status(create(TASK_CLOSED, "task", "Closed memory task", parent_id=EPIC), "closed")
    status(create(IN_PROGRESS, "task", "Interface contract work"), "in_progress")
    b = create(BUG_BLOCKED, "bug", "Blocked bug", description="see src/rebar/x.py")
    create(BLOCKER, "task", "The blocker")
    ev.write(b, "LINK", {"target_id": BLOCKER, "relation": "depends_on"})
    a = create(ARCHIVED, "task", "Archived memory task")
    ev.write(a, "ARCHIVED", {})
    (a / ".archived").write_text("")
    d = create(DELETED, "task", "Deleted memory task")
    status(d, "deleted")
    ev.write(d, "ARCHIVED", {})
    status(create(STATUS_DELETED, "task", "Deleted-only memory task"), "deleted")
    create(LOG_OLD, "session_log", "memory session one")
    create(LOG_NEW, "session_log", "memory session two")
    la = create(LOG_ARCHIVED, "session_log", "archived memory session")
    ev.write(la, "ARCHIVED", {})
    create(REVIEW, "code_review", "memory code review", description="memory " * 200)
    corrupt = root / CORRUPT
    corrupt.mkdir()
    (corrupt / "1-00000000-0000-4000-8000-999999999999-CREATE.json").write_text("{not json")

    bridge = root / ".bridge_state"
    bridge.mkdir()
    bindings = {
        TASK_OPEN: {"state": "confirmed", "jira_key": "REB-1"},
        DELETED: {"state": "confirmed", "jira_key": "REB-2"},
        "aaaa-0000-0000-9999": {"state": "confirmed", "jira_key": "REB-3"},
    }
    reverse = {v["jira_key"]: k for k, v in bindings.items()}
    (bridge / "bindings.json").write_text(json.dumps({"bindings": bindings, "reverse": reverse}))
    return str(root)


def _strip_new_kwargs(monkeypatch: pytest.MonkeyPatch, *modules: Any) -> None:
    """Make ``reduce_all_tickets`` ignore ``keep``/``keep_fields``: the old call."""
    real = _api.reduce_all_tickets

    def old_path(tracker_dir: Any, **kwargs: Any) -> list[dict]:
        kwargs.pop("keep", None)
        kwargs.pop("keep_fields", None)
        return real(tracker_dir, **kwargs)

    for mod in modules:
        monkeypatch.setattr(mod, "reduce_all_tickets", old_path)


def _ids(rows: list[dict]) -> list[str]:
    return [r.get("ticket_id") or r.get("id") for r in rows]


# ── retention ─────────────────────────────────────────────────────────────────


class _Tracked(dict):
    __slots__ = ("__weakref__",)


def _retained_after_loop(
    monkeypatch: pytest.MonkeyPatch, tracker: str, discarded: set[str], **kwargs: Any
) -> tuple[list[str], list[dict]]:
    """Reduce the store and report discarded tickets still alive at the last reduce.

    Each state is handed out as a weak-referenceable dict. When the loop reduces its
    final ticket, every earlier discarded state must already be unreachable.
    """
    real = _api.reduce_ticket
    refs: list[tuple[str, weakref.ref]] = []
    alive_at_end: list[str] = []
    entries = sorted(p.name for p in Path(tracker).iterdir() if not p.name.startswith("."))

    def tracked(path: Any, **kw: Any) -> dict | None:
        name = Path(path).name
        if name == entries[-1]:
            gc.collect()
            alive_at_end.extend(tid for tid, ref in refs if ref() is not None)
        state = real(path, **kw)
        if state is None:
            return None
        out = _Tracked(state)
        if name in discarded:
            refs.append((name, weakref.ref(out)))
        return out

    monkeypatch.setattr(_api, "reduce_ticket", tracked)
    result = _api.reduce_all_tickets(tracker, **kwargs)
    return alive_at_end, result


def test_exclusions_drop_tickets_inside_the_loop(
    tracker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    discarded = {DELETED, STATUS_DELETED, LOG_OLD, LOG_NEW, REVIEW}
    alive, result = _retained_after_loop(
        monkeypatch, tracker, discarded, exclude_deleted=True, exclude_session_logs=True
    )
    assert alive == [], f"excluded tickets still held while the loop ran: {alive}"
    assert not discarded & set(_ids(result))


def test_keep_predicate_drops_tickets_inside_the_loop(
    tracker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    discarded = {EPIC, TASK_CLOSED, BLOCKER, BUG_BLOCKED}
    alive, result = _retained_after_loop(
        monkeypatch, tracker, discarded, keep=lambda s: s.get("ticket_id") not in discarded
    )
    assert alive == []
    assert TASK_OPEN in _ids(result)
    assert not discarded & set(_ids(result))


def test_keep_fields_projects_and_exclusions_match_post_loop_filtering(tracker: str) -> None:
    full = _api.reduce_all_tickets(tracker)
    expected = [
        r
        for r in full
        if not r.get("archived")
        and r.get("status") != "deleted"
        and r.get("ticket_type") not in _api._NON_GRAPH_ARTIFACT_TYPES
    ]
    got = _api.reduce_all_tickets(
        tracker, exclude_archived=True, exclude_deleted=True, exclude_session_logs=True
    )
    assert got == expected
    lean = _api.reduce_all_tickets(tracker, keep_fields=("ticket_id", "status"))
    by_id = {r.get("ticket_id"): r for r in full}
    for row in lean:
        if by_id[row["ticket_id"]].get("error"):
            continue  # error dicts are never projected (matches omit_fields)
        assert set(row) == {"ticket_id", "status"}
    assert [(r.get("ticket_id"), r.get("status")) for r in lean] == [
        (r.get("ticket_id"), r.get("status")) for r in full
    ]


# ── output equality per caller ────────────────────────────────────────────────


@pytest.mark.parametrize("full", [True, False])
def test_search_matches_the_old_path(
    tracker: str, monkeypatch: pytest.MonkeyPatch, full: bool
) -> None:
    new = reads.search_state(tracker, "memory", full=full)
    assert sorted(_ids(new)) == sorted([EPIC, TASK_OPEN, TASK_CLOSED, LOG_OLD, LOG_NEW, REVIEW])
    if full:
        assert any(r.get("jira_key") == "REB-1" for r in new)
    by_jira = reads.search_state(tracker, "REB-1", full=full)
    assert _ids(by_jira) == [TASK_OPEN]
    _strip_new_kwargs(monkeypatch, reads)
    assert reads.search_state(tracker, "memory", full=full) == new
    assert reads.search_state(tracker, "REB-1", full=full) == by_jira


def test_search_passes_an_in_loop_predicate(tracker: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    real = _api.reduce_all_tickets

    def spy(tracker_dir: Any, **kwargs: Any) -> list[dict]:
        seen.update(kwargs)
        return real(tracker_dir, **kwargs)

    monkeypatch.setattr(reads, "reduce_all_tickets", spy)
    reads.search_state(tracker, "memory")
    assert callable(seen.get("keep"))


def test_recent_session_logs_match_the_old_path(
    tracker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    new = reads.recent_session_logs_state(tracker, limit=5)
    assert _ids(new) == [LOG_NEW, LOG_OLD]
    assert "description" in new[0], "rows must be full public states"
    one = reads.recent_session_logs_state(tracker, limit=1)
    assert _ids(one) == [LOG_NEW]
    seen: dict[str, Any] = {}
    real = _api.reduce_all_tickets

    def spy(tracker_dir: Any, **kwargs: Any) -> list[dict]:
        seen.update(kwargs)
        return real(tracker_dir, **kwargs)

    monkeypatch.setattr(reads, "reduce_all_tickets", spy)
    reads.recent_session_logs_state(tracker, limit=5)
    assert seen.get("keep_fields"), "the store-wide pass must be projected"
    _strip_new_kwargs(monkeypatch, reads)
    assert reads.recent_session_logs_state(tracker, limit=5) == new


def test_binding_drift_matches_the_old_path(tracker: str, monkeypatch: pytest.MonkeyPatch) -> None:
    new = bridge_fsck_drift.audit_binding_drift(Path(tracker))
    assert {e["local_id"] for e in new["unbound_local"]} >= {LOG_OLD, REVIEW, EPIC}
    seen: dict[str, Any] = {}
    real = reducer_pkg.reduce_all_tickets

    def spy(tracker_dir: Any, **kwargs: Any) -> list[dict]:
        seen.update(kwargs)
        return real(tracker_dir, **kwargs)

    monkeypatch.setattr(reducer_pkg, "reduce_all_tickets", spy)
    assert bridge_fsck_drift.audit_binding_drift(Path(tracker)) == new
    assert seen.get("keep_fields"), "drift must reduce only the fields it reads"
    _strip_new_kwargs(monkeypatch, reducer_pkg)
    assert bridge_fsck_drift.audit_binding_drift(Path(tracker)) == new


def test_validate_matches_the_old_path(tracker: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reads, "ensure_fresh", lambda *a, **k: None)
    new_issues = validate.normalize_issues(validate._raw_tickets(tracker))
    assert sorted(i["id"] for i in new_issues) == sorted(
        [EPIC, TASK_OPEN, IN_PROGRESS, BUG_BLOCKED, BLOCKER, STATUS_DELETED]
    )
    new_report = validate.validate_state(tracker)
    _strip_new_kwargs(monkeypatch, reads)
    assert validate.normalize_issues(validate._raw_tickets(tracker)) == new_issues
    assert validate.validate_state(tracker) == new_report


def test_graph_load_matches_the_old_path(tracker: str, monkeypatch: pytest.MonkeyPatch) -> None:
    new = graph_loader.load_indexed_states(tracker)
    assert BLOCKER in new and REVIEW not in new and LOG_OLD not in new and CORRUPT not in new
    assert all("description" not in s for s in new.values())
    deps = {tid: _graph._compute_dep_graph(tid, tracker) for tid in (BUG_BLOCKED, EPIC, LOG_OLD)}
    assert deps[BUG_BLOCKED]["blockers"] == [BLOCKER]
    assert sorted(deps[EPIC]["children"]) == sorted([TASK_OPEN, TASK_CLOSED])
    _strip_new_kwargs(monkeypatch, graph_loader.reducer)
    monkeypatch.setattr(graph_loader.reducer, "reduce_all_tickets", _without_omit(monkeypatch))
    old = graph_loader.load_indexed_states(tracker)
    assert sorted(old) == sorted(new)
    assert {
        tid: _graph._compute_dep_graph(tid, tracker) for tid in (BUG_BLOCKED, EPIC, LOG_OLD)
    } == deps


def _without_omit(monkeypatch: pytest.MonkeyPatch) -> Any:
    real = _api.reduce_all_tickets

    def old_path(tracker_dir: Any, **kwargs: Any) -> list[dict]:
        for key in ("keep", "keep_fields", "omit_fields"):
            kwargs.pop(key, None)
        return real(tracker_dir, **kwargs)

    return old_path


@pytest.mark.parametrize(
    "query",
    [
        TicketQuery(status="open"),
        TicketQuery(status="open", include_body=False),
        TicketQuery(ticket_type="task", parent=EPIC),
        TicketQuery(status="open", blocking_state="unblocked"),
        TicketQuery(status="open", blocking_state="blocked"),
        TicketQuery(min_children=1),
        TicketQuery(status="open", with_children_count=True),
        TicketQuery(include_archived=True, exclude_deleted=True),
        TicketQuery(ticket_type="session_log,code_review", sort="-created"),
        TicketQuery(status="error"),
    ],
)
def test_list_states_matches_the_old_path(
    tracker: str, monkeypatch: pytest.MonkeyPatch, query: TicketQuery
) -> None:
    new = reads.list_states(tracker, query)
    _strip_new_kwargs(monkeypatch, reads)
    assert reads.list_states(tracker, query) == new


def test_list_states_prefilters_only_without_child_counts(
    tracker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    real = _api.reduce_all_tickets

    def spy(tracker_dir: Any, **kwargs: Any) -> list[dict]:
        seen.append(kwargs)
        return real(tracker_dir, **kwargs)

    monkeypatch.setattr(reads, "reduce_all_tickets", spy)
    rows = reads.list_states(tracker, TicketQuery(status="open"))
    assert callable(seen[-1].get("keep"))
    assert {r["status"] for r in rows} == {"open"}
    reads.list_states(tracker, TicketQuery(status="open", with_children_count=True))
    assert seen[-1].get("keep") is None
    counted = reads.list_states(tracker, TicketQuery(ticket_type="epic", with_children_count=True))
    assert counted[0]["children_count"] == 2, "closed children still count"
