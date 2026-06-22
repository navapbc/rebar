"""Bounded-concurrent map fan-out (8d8e): parallel agent calls with serialized
commits, identical results to the serial path, real concurrency (proven by a
barrier that would DEADLOCK under serial execution), and deterministic replay under
concurrency (order-independent via the iteration-keyed markers).
"""

from __future__ import annotations

import threading
import time

import pytest

from rebar.llm.workflow import executor as _ex


class _Crash(Exception):
    pass


class _RecorderWithLockProbe(_ex.RunRecorder):
    """Persists final markers by frame_key (LWW) and ASSERTS commits are serialized:
    if two threads were ever inside ``step_recorded`` at once, ``_inside`` would
    exceed 1. Can crash after the Nth commit (for the replay test)."""

    def __init__(self, store=None, crash_after=None):
        self.store = store if store is not None else {}
        self.crash_after = crash_after
        self.commits = 0
        self._inside = 0
        self.max_concurrent_commit = 0

    def run_started(self, record): ...
    def run_finished(self, record): ...

    def step_recorded(self, record):
        self._inside += 1
        self.max_concurrent_commit = max(self.max_concurrent_commit, self._inside)
        try:
            if record.get("status") == "running":
                return
            fk = record.get("frame_key") or record.get("step_id")
            self.store[fk] = dict(record)
            self.commits += 1
            if self.crash_after is not None and self.commits >= self.crash_after:
                self.crash_after = None
                raise _Crash(f"crash after {self.commits}")
        finally:
            self._inside -= 1

    def completed_step(self, run_id, frame_key):
        rec = self.store.get(frame_key)
        return rec if rec and rec.get("status") == "succeeded" else None


def _map_wf(n_items, *, bound):
    return {
        "schema_version": "2",
        "name": "fan",
        "inputs": {"items": {"type": "array"}},
        "steps": [
            {
                "id": "M",
                "map": {
                    "over": "${{ inputs.items }}",
                    "as": "item",
                    "max_concurrency": bound,
                    "body": [{"id": "work", "uses": "work"}],
                },
            }
        ],
    }


def _run(doc, registry, items):
    rec = _RecorderWithLockProbe()
    res = _ex.run_workflow(
        doc,
        {"items": items},
        recorder=rec,
        scripted_registry=registry,
        agent_runner=_ex.FakeAgentRunner(),
    )
    return res, rec


def test_concurrent_map_matches_serial_result():
    reg = {"work": lambda ctx: _ex.StepResult(outputs={"out": f"done:{ctx.inputs.get('item')}"})}
    wf = {
        "schema_version": "2",
        "name": "fan",
        "inputs": {"items": {"type": "array"}},
        "steps": [
            {
                "id": "M",
                "map": {
                    "over": "${{ inputs.items }}",
                    "as": "item",
                    "max_concurrency": 4,
                    "body": [{"id": "work", "uses": "work", "with": {"item": "${{ map.item }}"}}],
                },
            }
        ],
    }
    items = ["a", "b", "c", "d", "e"]
    res, rec = _run(wf, reg, items)
    assert res.status == "succeeded"
    assert res.outputs["M"] == {"count": 5}
    # Every iteration committed exactly one marker, keyed by its iteration path.
    assert {f"M#{j}/work" for j in range(5)} <= set(rec.store)
    for j, it in enumerate(items):
        assert rec.store[f"M#{j}/work"]["outputs"]["out"] == f"done:{it}"


def test_commits_are_serialized_even_under_concurrency():
    reg = {"work": lambda ctx: _ex.StepResult(outputs={"ok": True})}
    _, rec = _run(_map_wf(8, bound=8), reg, list(range(8)))
    # The lock guarantees the recorder is never re-entered concurrently.
    assert rec.max_concurrent_commit == 1


def test_map_actually_runs_concurrently():
    # A barrier requiring `bound` parties to proceed: under TRUE concurrency the first
    # `bound` iterations rendezvous and pass; under serial execution the first
    # iteration would block forever (the others never start) -> barrier times out.
    bound = 4
    barrier = threading.Barrier(bound, timeout=5)

    def work(ctx):
        barrier.wait()  # raises BrokenBarrierError on timeout (serial would deadlock)
        return _ex.StepResult(outputs={"ok": True})

    res, rec = _run(_map_wf(bound, bound=bound), {"work": work}, list(range(bound)))
    # Reaching "succeeded" IS the proof: a serial executor would block the first
    # iteration on the barrier forever and the others would never start (timeout ->
    # BrokenBarrierError -> failed run).
    assert res.status == "succeeded"
    assert {f"M#{j}/work" for j in range(bound)} <= set(rec.store)


def test_default_max_concurrency_is_serial():
    # max_concurrency defaults to 1 -> the serial path (no thread pool). A barrier of
    # 2 parties would deadlock; instead we just confirm it completes single-threaded by
    # recording the executing thread.
    threads = set()

    def work(ctx):
        threads.add(threading.current_thread().name)
        return _ex.StepResult(outputs={})

    wf = {
        "schema_version": "2",
        "name": "ser",
        "inputs": {"items": {"type": "array"}},
        "steps": [
            {
                "id": "M",
                "map": {
                    "over": "${{ inputs.items }}",
                    "as": "i",
                    "body": [{"id": "work", "uses": "work"}],
                },
            }
        ],
    }
    res, _ = _run(wf, {"work": work}, [1, 2, 3])
    assert res.status == "succeeded"
    assert threads == {threading.current_thread().name}  # all ran on the main thread


def test_replay_under_concurrency_is_exactly_once():
    # Crash mid-fan-out, then replay sharing the store: completed iterations are
    # idempotent-skipped, the map completes, and each iteration ran its effect once.
    calls: list[str] = []
    lock = threading.Lock()

    def work(ctx):
        with lock:
            calls.append(ctx.frame_key)
        return _ex.StepResult(outputs={"n": ctx.iteration})

    wf = _map_wf(6, bound=3)
    store: dict = {}
    rec1 = _RecorderWithLockProbe(store=store, crash_after=3)
    try:
        _ex.run_workflow(
            wf,
            {"items": list(range(6))},
            recorder=rec1,
            scripted_registry={"work": work},
            agent_runner=_ex.FakeAgentRunner(),
        )
    except _Crash:
        pass
    committed = {k for k, v in store.items() if v.get("status") == "succeeded"}
    calls.clear()
    rec2 = _RecorderWithLockProbe(store=store)
    res = _ex.run_workflow(
        wf,
        {"items": list(range(6))},
        recorder=rec2,
        scripted_registry={"work": work},
        agent_runner=_ex.FakeAgentRunner(),
    )
    assert res.status == "succeeded"
    # No already-committed iteration re-ran on replay (exactly-once across the crash).
    for fk in committed:
        assert fk not in calls, f"{fk} re-ran on replay"
    assert {f"M#{j}/work" for j in range(6)} <= set(store)
    assert res.outputs["M"] == {"count": 6}


def test_concurrent_map_body_with_control_construct_is_guarded():
    # A branch INSIDE a concurrent map body: its summary write (rc.outputs/statuses)
    # must also be serialized (M1). Run many iterations at high concurrency and assert
    # the recorder is never re-entered concurrently and every iteration's branch +
    # chosen arm committed correctly.
    reg = {
        "y": lambda ctx: _ex.StepResult(outputs={"v": "yes"}),
        "n": lambda ctx: _ex.StepResult(outputs={"v": "no"}),
    }
    wf = {
        "schema_version": "2",
        "name": "fanbranch",
        "inputs": {"items": {"type": "array"}},
        "steps": [
            {
                "id": "M",
                "map": {
                    "over": "${{ inputs.items }}",
                    "as": "item",
                    "index_var": "ix",
                    "max_concurrency": 8,
                    "body": [
                        {
                            "id": "g",
                            "branch": {
                                "when": "${{ map.item }}",
                                "then": [{"id": "yes", "uses": "y"}],
                                "else": [{"id": "no", "uses": "n"}],
                            },
                        }
                    ],
                },
            }
        ],
    }
    items = [bool(j % 2) for j in range(16)]
    res, rec = _run(wf, reg, items)
    assert res.status == "succeeded"
    assert rec.max_concurrent_commit == 1  # the branch summary writes are serialized too
    for j, flag in enumerate(items):
        assert rec.store[f"M#{j}/g"]["outputs"] == {"taken": "then" if flag else "else"}
        arm = "yes" if flag else "no"
        assert f"M#{j}/g@{'then' if flag else 'else'}/{arm}" in rec.store


def test_map_iteration_failure_fails_the_run():
    def work(ctx):
        if ctx.iteration == 2:
            raise RuntimeError("boom")
        return _ex.StepResult(outputs={})

    res, _ = _run(_map_wf(5, bound=5), {"work": work}, list(range(5)))
    assert res.status == "failed"
    # The failing iteration's frame key AND the underlying exception text surface (not a
    # vacuous "failed" substring).
    assert "M#2/work" in (res.error or "") and "boom" in (res.error or "")


def test_max_concurrency_upper_bound_is_respected():
    # The actual "bounded" guarantee: with max_concurrency=2 over 6 items, never more
    # than 2 iterations may be in-flight at once. An atomic counter records the peak.
    bound = 2
    lock = threading.Lock()
    state = {"inflight": 0, "peak": 0}

    def work(ctx):
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        time.sleep(0.02)  # widen the window so a bound violation would be observed
        with lock:
            state["inflight"] -= 1
        return _ex.StepResult(outputs={})

    res, _ = _run(_map_wf(6, bound=bound), {"work": work}, list(range(6)))
    assert res.status == "succeeded"
    assert state["peak"] <= bound, f"observed {state['peak']} concurrent > bound {bound}"
    assert state["peak"] == bound  # and it DID saturate the bound (real concurrency)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
