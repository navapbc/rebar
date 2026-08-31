"""In-flight singleflight de-duplication for long-running MCP gate ops (bug d80d).

WHY THIS TEST EXISTS. Calling ``review_plan`` / ``verify_completion`` over the MCP
server runs a 15-20 minute billable LLM gate. The MCP *client* SDK gives up at 60s
with an opaque ``-32001``; the documented agent reflex is to re-invoke, which — with
no de-duplication — starts a SECOND billable LLM pass while the first is still in
flight (bug d80d AC #2). ``rebar._mcp_inflight`` collapses concurrent duplicate
calls for the same ``(gate, ticket, basis)`` into ONE computation whose result every
caller shares (golang ``singleflight`` semantics).

WHAT THESE TESTS PIN, and why they are shaped this way. The gate is stubbed with a
fake that blocks on a ``threading.Event`` and counts its invocations, so the verdict
is a VALUE (the call count) not a wall-clock duration — deterministic, and zero
tokens. The registry is thread-based on purpose: the MCP server already runs every
sync tool body on its own anyio worker thread (``_mcp_health.offload_sync_tools``),
and the certified-tool in-flight gauge / SIGTERM drain REQUIRE those bodies to stay
synchronous, so the de-dup primitive collapses concurrent *threads*, not coroutines.
"""

from __future__ import annotations

import threading

from rebar import _mcp_inflight as inflight


class _BlockingGate:
    """A stand-in for ``rebar.llm.review_plan`` that blocks until released and counts calls."""

    def __init__(self, verdict=None):
        self.calls = 0
        self._started = threading.Event()
        self._release = threading.Event()
        self._verdict = verdict if verdict is not None else {"verdict": "PASS"}
        self._count_lock = threading.Lock()

    def __call__(self):
        with self._count_lock:
            self.calls += 1
        self._started.set()
        self._release.wait(timeout=5)
        return self._verdict

    def wait_started(self):
        assert self._started.wait(timeout=5), "gate never started"

    def release(self):
        self._release.set()


def _spawn(fn):
    box: dict = {}

    def _run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — capture for the test to assert on
            box["error"] = exc

    t = threading.Thread(target=_run)
    t.start()
    return t, box


def test_concurrent_same_key_runs_the_gate_exactly_once():
    """AC #2 proof: two concurrent same-key callers share ONE gate run, one verdict."""
    inflight.reset_registry()
    gate = _BlockingGate({"verdict": "PASS", "coverage": {"llm_ran": True}})
    key = inflight.compute_key("plan_review", "d80d", "a" * 40, "source=attested", False)

    t1, box1 = _spawn(lambda: inflight.run_singleflight(key, inflight.new_job_id, gate))
    gate.wait_started()
    # The second caller arrives while the first is still in flight — it must ATTACH.
    t2, box2 = _spawn(lambda: inflight.run_singleflight(key, inflight.new_job_id, gate))
    # Give the follower a moment to reach the wait, then release the shared computation.
    threading.Event().wait(0.05)
    gate.release()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert gate.calls == 1, "the gate must run exactly once for two concurrent same-key calls"
    job1, res1 = box1["value"]
    job2, res2 = box2["value"]
    assert res1 == res2 == {"verdict": "PASS", "coverage": {"llm_ran": True}}
    assert job1 == job2, "both callers share the leader's job_id"


def test_different_basis_runs_the_gate_twice():
    """A moved base ref => a different resolved SHA => a different key => a real re-review."""
    inflight.reset_registry()
    gate = _BlockingGate()
    key_a = inflight.compute_key("plan_review", "d80d", "a" * 40, "source=attested", False)
    key_b = inflight.compute_key("plan_review", "d80d", "b" * 40, "source=attested", False)
    assert key_a != key_b

    t1, _ = _spawn(lambda: inflight.run_singleflight(key_a, inflight.new_job_id, gate))
    gate.wait_started()
    t2, _ = _spawn(lambda: inflight.run_singleflight(key_b, inflight.new_job_id, gate))
    gate.release()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert gate.calls == 2, "different basis keys must not be de-duplicated"


def test_re_invocation_after_completion_runs_again():
    """Purge-on-completion: a same-key call re-invokes once the prior run finished."""
    inflight.reset_registry()
    gate = _BlockingGate()
    key = inflight.compute_key("plan_review", "d80d", "a" * 40, "source=attested", False)

    gate.release()  # do not block — let each run complete immediately
    job1, _ = inflight.run_singleflight(key, inflight.new_job_id, gate)
    job2, _ = inflight.run_singleflight(key, inflight.new_job_id, gate)

    assert gate.calls == 2, "a call after the prior run completed must re-invoke the gate"
    assert job1 != job2, "each fresh run gets its own job_id"


def test_force_bypass_skips_dedup():
    """force=True must never attach to an in-flight run (mirrors review_plan(force=True))."""
    inflight.reset_registry()
    gate = _BlockingGate()
    key = inflight.compute_key("plan_review", "d80d", "a" * 40, "source=attested", False)

    t1, _ = _spawn(lambda: inflight.run_singleflight(key, inflight.new_job_id, gate, bypass=True))
    gate.wait_started()
    t2, _ = _spawn(lambda: inflight.run_singleflight(key, inflight.new_job_id, gate, bypass=True))
    gate.release()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert gate.calls == 2, "bypass=True must not de-duplicate"


def test_kill_switch_disables_dedup(monkeypatch):
    """REBAR_MCP_DEDUP=0 turns the whole singleflight into a pass-through."""
    inflight.reset_registry()
    monkeypatch.setenv("REBAR_MCP_DEDUP", "0")
    assert inflight.dedup_enabled() is False
    gate = _BlockingGate()
    key = inflight.compute_key("plan_review", "d80d", "a" * 40, "source=attested", False)

    t1, _ = _spawn(lambda: inflight.run_singleflight(key, inflight.new_job_id, gate))
    gate.wait_started()
    t2, _ = _spawn(lambda: inflight.run_singleflight(key, inflight.new_job_id, gate))
    gate.release()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert gate.calls == 2, "with the kill-switch on, calls are not de-duplicated"


def test_dedup_on_by_default(monkeypatch):
    monkeypatch.delenv("REBAR_MCP_DEDUP", raising=False)
    assert inflight.dedup_enabled() is True


def test_leader_exception_propagates_to_followers_and_purges():
    """A gate that raises fails every attached caller with the SAME error, then purges."""
    inflight.reset_registry()
    boom = RuntimeError("gate blew up")

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def raising_gate():
        calls["n"] += 1
        started.set()
        release.wait(timeout=5)
        raise boom

    key = inflight.compute_key(
        "verify_completion", "d80d", "a" * 40, "graph=None;source=attested", False
    )
    t1, box1 = _spawn(lambda: inflight.run_singleflight(key, inflight.new_job_id, raising_gate))
    assert started.wait(timeout=5)
    t2, box2 = _spawn(lambda: inflight.run_singleflight(key, inflight.new_job_id, raising_gate))
    threading.Event().wait(0.05)
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert calls["n"] == 1, "the raising gate ran once; the follower shared the failure"
    assert box1.get("error") is boom
    assert box2.get("error") is boom
    # Purge-on-completion also applies on the error path: a retry re-invokes.
    release2 = threading.Event()
    release2.set()

    def ok_gate():
        calls["n"] += 1
        return {"verdict": "PASS"}

    inflight.run_singleflight(key, inflight.new_job_id, ok_gate)
    assert calls["n"] == 2, "after a failed run purged its key, a retry runs again"


def test_max_age_sweep_reclaims_a_wedged_entry(monkeypatch):
    """A crashed/wedged leader must not leak its key forever — the defensive sweep reclaims it."""
    inflight.reset_registry()
    monkeypatch.setattr(inflight, "_MAX_AGE_SECONDS", 0.0)
    gate = _BlockingGate()
    key = inflight.compute_key("plan_review", "d80d", "a" * 40, "source=attested", False)

    # Seed a stale in-flight entry that will never complete (simulated wedged run).
    inflight.seed_stale_entry(key)
    gate.release()
    inflight.run_singleflight(key, inflight.new_job_id, gate)
    assert gate.calls == 1, "the sweep evicted the wedged entry so a fresh run proceeded"


def test_compute_key_is_deterministic_and_variant_sensitive():
    base = ("plan_review", "d80d", "a" * 40, "source=attested", False)
    assert inflight.compute_key(*base) == inflight.compute_key(*base)
    assert inflight.compute_key(*base) != inflight.compute_key("verify_completion", *base[1:])
    assert inflight.compute_key(*base) != inflight.compute_key(base[0], "other", *base[2:])
    assert inflight.compute_key(*base) != inflight.compute_key(*base[:2], "b" * 40, *base[3:])
    assert inflight.compute_key(*base) != inflight.compute_key(*base[:3], "source=local", base[4])
    assert inflight.compute_key(*base) != inflight.compute_key(*base[:4], True)


def test_canonical_ticket_id_is_best_effort(tmp_path, monkeypatch):
    """Canonicalisation must never raise — a store it cannot read returns the input unchanged."""
    monkeypatch.chdir(tmp_path)
    assert inflight.canonical_ticket_id("d80d-7be7-1c0a-4231") == "d80d-7be7-1c0a-4231"


def test_resolve_basis_sha_falls_back_when_ref_unresolvable(tmp_path, monkeypatch):
    """An unresolvable ref must yield a stable non-raising sentinel, not crash the tool."""
    monkeypatch.chdir(tmp_path)
    basis = inflight.resolve_basis_sha("does-not-exist-ref", None, repo_root=str(tmp_path))
    assert basis  # non-empty, deterministic
    assert inflight.resolve_basis_sha("does-not-exist-ref", None, repo_root=str(tmp_path)) == basis
