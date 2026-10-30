"""`summary` over N ids loads the dependency graph's store states once.

``build_dep_graph`` caches graphs per ticket id, so each id a summary names missed the
cache and reduced the whole store again (bug exhaustive-semiacidic-chupacabra: 3 ids,
3 passes, 29,238 reductions). ``summary_many`` shares one memoised loader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import rebar.graph._loader as graph_loader
from rebar._engine_support import gates

pytestmark = pytest.mark.unit

_ENV = "00000000-0000-4000-8000-000000000001"


class _Event:
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


BLOCKED, BLOCKER, READY, ARCHIVED, LOG = (f"bbbb-0000-0000-{i:04d}" for i in range(1, 6))
DONE_BLOCKER, UNBLOCKED = "bbbb-0000-0000-0006", "bbbb-0000-0000-0007"
IDS = [BLOCKED, READY, ARCHIVED, LOG, UNBLOCKED, "no-such-ticket"]


@pytest.fixture
def tracker(tmp_path: Path) -> str:
    root = tmp_path / "tracker"
    ev = _Event()

    def create(tid: str, ttype: str = "task", title: str = "t") -> Path:
        tdir = root / tid
        ev.write(tdir, "CREATE", {"ticket_type": ttype, "title": title})
        return tdir

    blocked = create(BLOCKED, "bug", "blocked bug")
    create(BLOCKER, title="open blocker")
    ev.write(blocked, "LINK", {"target_id": BLOCKER, "relation": "depends_on"})
    create(READY, title="ready task")
    archived = create(ARCHIVED, title="archived task")
    ev.write(archived, "ARCHIVED", {})
    create(LOG, "session_log", "a session log")
    done = create(DONE_BLOCKER, title="closed blocker")
    ev.write(done, "STATUS", {"status": "closed", "current_status": "open"})
    unblocked = create(UNBLOCKED, title="unblocked task")
    ev.write(unblocked, "LINK", {"target_id": DONE_BLOCKER, "relation": "depends_on"})
    return str(root)


def _count_store_passes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = graph_loader.reducer.reduce_all_tickets

    def counting(tracker_dir: Any, **kwargs: Any) -> list[dict]:
        calls.append(str(tracker_dir))
        return real(tracker_dir, **kwargs)

    monkeypatch.setattr(graph_loader.reducer, "reduce_all_tickets", counting)
    return calls


def _drop_graph_cache(tracker: str) -> None:
    cache = Path(tracker) / ".graph-cache.json"
    if cache.exists():
        cache.unlink()


def test_summary_of_three_ids_reduces_the_store_once(
    tracker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_store_passes(monkeypatch)
    gates.summary_many([BLOCKED, READY, UNBLOCKED], tracker)
    assert len(calls) == 1, f"expected one whole-store pass, got {len(calls)}"


def test_a_warm_graph_cache_needs_no_store_pass(
    tracker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    gates.summary_many([BLOCKED, READY, UNBLOCKED], tracker)
    calls = _count_store_passes(monkeypatch)
    gates.summary_many([BLOCKED, READY, UNBLOCKED], tracker)
    assert calls == []


def test_summary_many_matches_the_per_id_loop(tracker: str) -> None:
    _drop_graph_cache(tracker)
    old = [gates.summary_compute(tid, tracker) for tid in IDS]
    _drop_graph_cache(tracker)
    new = gates.summary_many(IDS, tracker)
    assert new == old
    by_id = {item["ticket_id"]: item for item in new}
    assert by_id[BLOCKED]["blocking_summary"] == f"blocked by: {BLOCKER}"
    assert by_id[READY]["blocking_summary"] == "ready"
    assert by_id[UNBLOCKED]["blocking_summary"] == "ready"
    assert by_id["no-such-ticket"]["status"] == "unknown"


def test_library_summary_uses_one_pass(tracker: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import rebar
    from rebar._engine_support import reads

    monkeypatch.setattr(reads, "tracker_dir", lambda repo_root=None: tracker)
    monkeypatch.setattr(reads, "ensure_fresh", lambda *a, **k: None)
    calls = _count_store_passes(monkeypatch)
    items = rebar.summary(BLOCKED, READY, UNBLOCKED)
    assert [i["ticket_id"] for i in items] == [BLOCKED, READY, UNBLOCKED]
    assert len(calls) == 1
