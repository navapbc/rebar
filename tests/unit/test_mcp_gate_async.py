"""Phase-2 async gate surface (bug d80d): ``*_start`` handles + durable poll.

Phase 1 de-dups a concurrent retry; Phase 2 removes the client timeout from the
request path entirely with an async ``review_plan_start`` / ``verify_completion_start``
that return a durable ``job_id`` in milliseconds while the gate runs on a background
daemon thread. These tests pin fast handle return, terminal poll, a duplicate
``*_start`` that shares ONE run's job_id, and a fresh (post-restart) registry that still
resolves the durable verdict from the on-disk store.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import rebar
from rebar import _mcp_inflight as inflight
from rebar._mcp_llm import register_llm_tools
from rebar.llm import gate_runs


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    inflight.reset_registry()
    return repo


def _server_with_llm_tools(readonly: bool = False):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Settings

    Settings.model_rebuild()
    mcp = FastMCP("gate-async-test")
    ctx = SimpleNamespace(allow_llm=lambda: True, readonly=lambda: readonly)
    register_llm_tools(mcp, ctx)
    return mcp


class _BlockingReviewPlan:
    """A stand-in for ``rebar.llm.review_plan`` that blocks until released, counting calls.

    ``hold`` caps the wait for ``release``; ``finished`` is set when the body returns."""

    def __init__(self, hold: float = 5):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self._hold = hold
        self._lock = threading.Lock()

    def __call__(
        self,
        ticket_id,
        *,
        ref=None,
        source=None,
        sign=True,
        emit_sidecar=True,
        force=False,
        engine=None,
    ):
        with self._lock:
            self.calls += 1
        self.started.set()
        self.release.wait(timeout=self._hold)
        self.finished.set()
        return {"verdict": "PASS", "ticket_id": ticket_id}


def _start_review_plan_and_check_gate(mcp, fake: _BlockingReviewPlan) -> tuple[dict, bool]:
    """Call ``review_plan_start`` with the stub gate held; return ``(handle, gate_finished)``.

    ``gate_finished`` is read the moment the call returns. Nothing releases the gate before
    then, so a start that waits on the gate returns only after the stub's ``hold`` expires,
    with ``finished`` already set; a start that hands the gate to its daemon returns with the
    gate still held, however loaded the host. That ordering is the contract. A wall-clock
    bound measured the start's dedup-key git calls along with it (bug 2698-ff06-5f35-463c).
    """
    holder: dict = {}

    async def scenario():
        holder["started"] = await mcp._tool_manager.call_tool(
            "review_plan_start", {"ticket_id": "d80d"}
        )
        holder["gate_finished"] = fake.finished.is_set()

    anyio.run(scenario)
    return holder["started"], holder["gate_finished"]


def test_begin_gate_job_attaches_to_an_inflight_key_with_the_same_job_id():
    inflight.reset_registry()
    first = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested")
    assert first.is_new is True
    second = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested")
    assert second.is_new is False
    assert second.job_id == first.job_id, "a duplicate *_start must share the in-flight job_id"


def test_begin_gate_job_gives_a_distinct_job_for_a_different_basis():
    inflight.reset_registry()
    a = inflight.begin_gate_job("plan_review", "d80d", ref="deadbeef", variant="source=attested")
    b = inflight.begin_gate_job("plan_review", "d80d", ref="feedface", variant="source=attested")
    assert a.job_id != b.job_id


def test_begin_gate_job_force_never_attaches():
    inflight.reset_registry()
    a = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested")
    b = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested", force=True)
    assert b.is_new is True
    assert b.job_id != a.job_id


def test_gate_status_unknown_for_an_unrecorded_job(store):
    out = gate_runs.gate_run_status("no-such-job")
    assert out["status"] == "unknown"
    assert "findings" not in out


def test_gate_status_running_then_terminal_from_the_index(store):
    inflight.reset_registry()
    handle = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested")
    gate_runs.record_gate_run(
        {
            "job_id": handle.job_id,
            "ticket_id": "d80d",
            "gate_type": "plan_review",
            "status": "running",
            "started_at": time.time(),
        }
    )
    running = gate_runs.gate_run_status(handle.job_id)
    assert running["status"] == "running"
    assert running["findings"]["reason"] == "run-running"
    handle.complete(result={"verdict": "PASS"})
    gate_runs.record_gate_run(
        {
            "job_id": handle.job_id,
            "ticket_id": "d80d",
            "gate_type": "plan_review",
            "status": "passed",
            "verdict": "PASS",
            "finished_at": time.time(),
        }
    )
    out = gate_runs.gate_run_status(handle.job_id)
    assert out["status"] == "passed"
    assert out["verdict"] == "PASS"
    assert out["findings"]["readable"] is False
    assert out["findings"]["reason"] == "review-result-generation-unknown"


def test_gate_status_failed_diagnostic_when_daemon_gone_and_index_still_running(store):
    inflight.reset_registry()
    gate_runs.record_gate_run(
        {
            "job_id": "wedged-job",
            "ticket_id": "d80d",
            "gate_type": "plan_review",
            "status": "running",
            "started_at": time.time() - (gate_runs._STALE_GRACE_SECONDS + 1.0),
        }
    )
    out = gate_runs.gate_run_status("wedged-job")
    assert out["status"] == "failed"
    assert out["verdict"] == "daemon-stopped"
    assert out["error"] == "async gate daemon stopped before recording a terminal result"
    assert out["findings"]["reason"] == "run-failed"


def test_gate_status_materializes_stale_running_as_failed_diagnostic(store):
    inflight.reset_registry()
    gate_runs.record_gate_run(
        {
            "job_id": "wedged-diagnostic-job",
            "ticket_id": "d80d",
            "gate_type": "plan_review",
            "status": "running",
            "started_at": time.time() - (gate_runs._STALE_GRACE_SECONDS + 1.0),
        }
    )

    out = gate_runs.gate_run_status("wedged-diagnostic-job")

    assert out["status"] == "failed"
    assert out["error"] == "async gate daemon stopped before recording a terminal result"
    assert out["findings"]["reason"] == "run-failed"
    assert out["findings"]["readable"] is False
    persisted = gate_runs.read_gate_run("wedged-diagnostic-job")
    assert persisted is not None
    assert persisted["status"] == "failed"
    assert persisted["error"] == out["error"]


def test_gate_status_fresh_registry_resolves_the_durable_verdict(store):
    # Simulate a NEW CONTAINER: the gate ran to completion and recorded its terminal
    # verdict to the on-disk store, then the in-process registry is wiped. A poll must
    # still resolve the durable outcome from disk (never a re-run).
    gate_runs.record_gate_run(
        {
            "job_id": "durable-job",
            "ticket_id": "d80d",
            "gate_type": "plan_review",
            "status": "passed",
            "verdict": "PASS",
            "finished_at": time.time(),
        }
    )
    inflight.reset_registry()  # the fresh registry has no in-memory trace of the run
    out = gate_runs.gate_run_status("durable-job")
    assert out["status"] == "passed"
    assert out["verdict"] == "PASS"


def test_gate_status_surfaces_the_durable_completion_verdict(store, monkeypatch):
    # A COMPLETION job's poll must also carry the gate's own signed-attestation currency in
    # ``durable`` (the same answer ``verify_completion_status`` gives). Record a terminal
    # completion run and stand up a certified, current attestation; the poll folds it in.
    from rebar import _reads, signing

    faithful_opcert = {
        "verified": True,
        "opcert": True,
        "signed_manifest": [signing.verified_at_sha_step("d" * 40), "delivered-now:true"],
        "merged_log_commit": "d" * 40,
        "signed_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(signing, "verify_signature", lambda *a, **k: faithful_opcert)
    monkeypatch.setattr(
        _reads, "show_ticket", lambda *a, **k: {"ticket_id": "d80d", "status": "closed"}
    )
    gate_runs.record_gate_run(
        {
            "job_id": "verify-job",
            "ticket_id": "d80d",
            "gate_type": "verify_completion",
            "status": "passed",
            "verdict": "PASS",
            "finished_at": time.time(),
        }
    )
    inflight.reset_registry()
    out = gate_runs.gate_run_status("verify-job")
    assert out["status"] == "passed"
    assert "durable" in out, "a completion poll must surface the durable attestation currency"
    assert out["durable"]["verdict"] == "certified"
    assert out["durable"]["ok"] is True


def test_gate_status_within_grace_window_still_reads_running(store):
    inflight.reset_registry()
    # An index that reads 'running' with NO active daemon but WITHIN the grace window is a
    # just-spawned job whose daemon has not yet claimed the registry — it must still read
    # 'running' (not prematurely failed). This is the sibling of the past-grace case.
    gate_runs.record_gate_run(
        {
            "job_id": "fresh-job",
            "ticket_id": "d80d",
            "gate_type": "plan_review",
            "status": "running",
            "started_at": time.time(),  # well within _STALE_GRACE_SECONDS
        }
    )
    assert gate_runs.gate_run_status("fresh-job")["status"] == "running"


def test_verify_completion_status_unsigned_without_an_attestation(store):
    tid = rebar.create_ticket("bug", "no completion attestation yet")
    out = gate_runs.verify_completion_status(tid)
    assert out["ok"] is False
    assert out["verdict"] == "unsigned"


def test_verify_completion_status_populates_verified_at_sha(store, monkeypatch):
    # The signed op-cert manifest is a list. Read verified_at_sha from its pinned
    # verified-at-sha step. Treating it as a dict left the value unset. A faithful
    # verdict avoids signing and SSH setup here.
    from rebar import _reads, signing

    sha = "a" * 40
    faithful_opcert = {
        "verified": True,
        "opcert": True,
        "signed_manifest": [signing.verified_at_sha_step(sha), "delivered-now:true"],
        "merged_log_commit": sha,
        "signed_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(signing, "verify_signature", lambda *a, **k: faithful_opcert)
    # A CERTIFIED verdict now requires the attestation to be current on the ticket's state, so
    # the read routes through ``compute_validity`` — a closed, never-reopened ticket keeps it
    # certified while still proving the ``verified_at_sha`` enrichment resolves from the list.
    monkeypatch.setattr(
        _reads, "show_ticket", lambda *a, **k: {"ticket_id": "d80d", "status": "closed"}
    )

    out = gate_runs.verify_completion_status("d80d")
    assert out["ok"] is True
    assert out["verdict"] == "certified"
    assert out["verified_at_sha"] == sha, "the pinned verified-at-sha must be resolved, not None"
    assert out["signed_at"] == "2026-01-01T00:00:00Z"


def test_verify_completion_status_falls_back_to_signed_head_without_a_pin(store, monkeypatch):
    # A local/unscoped verify has no pinned ``verified-at-sha`` step; the read then falls back
    # to the AUTHENTICATED signed head (``merged_log_commit`` for an op-cert).
    from rebar import _reads, signing

    head = "b" * 40
    faithful_opcert = {
        "verified": True,
        "opcert": True,
        "signed_manifest": ["delivered-now:true"],
        "merged_log_commit": head,
        "signed_at": "2026-02-02T00:00:00Z",
    }
    monkeypatch.setattr(signing, "verify_signature", lambda *a, **k: faithful_opcert)
    monkeypatch.setattr(
        _reads, "show_ticket", lambda *a, **k: {"ticket_id": "d80d", "status": "closed"}
    )

    out = gate_runs.verify_completion_status("d80d")
    assert out["verified_at_sha"] == head


def test_verify_completion_status_reports_stale_for_a_reopened_attestation(store, monkeypatch):
    # A reopened completion attestation is stale even when its signature verifies.
    # compute_validity, as in plan_review_status, reports stale-reopened instead
    # of treating the bare verified flag as current.
    from rebar import _reads, signing

    sha = "c" * 40
    faithful_opcert = {
        "verified": True,
        "opcert": True,
        "signed_manifest": [signing.verified_at_sha_step(sha), "delivered-now:true"],
        "merged_log_commit": sha,
        "signed_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(signing, "verify_signature", lambda *a, **k: faithful_opcert)
    # Closed, but reopened AFTER the attestation was signed → the verdict no longer applies.
    monkeypatch.setattr(
        _reads,
        "show_ticket",
        lambda *a, **k: {
            "ticket_id": "d80d",
            "status": "closed",
            "last_reopened_at": "2026-06-01T00:00:00Z",
        },
    )

    out = gate_runs.verify_completion_status("d80d")
    assert out["ok"] is False, "a reopened completion attestation is NOT current"
    assert out["verdict"] != "certified", f"stale attestation read as certified: {out['verdict']}"
    assert out["verdict"] == "stale-reopened"
    # The signed anchor is still surfaced as enrichment even though the verdict is stale.
    assert out["verified_at_sha"] == sha


# ── _terminal_from_result: classifying a completed run for the index ─────────────


def test_terminal_from_result_maps_a_structured_failure_to_failed():
    from rebar._mcp_llm import _terminal_from_result

    status, verdict = _terminal_from_result({"error": "llm_unavailable", "retryable": True})
    assert status == "failed"
    assert verdict == "llm_unavailable"


def test_terminal_from_result_maps_a_block_verdict_to_passed():
    # A BLOCK is a run that COMPLETED with a verdict, NOT one that errored — so the run status
    # is 'passed' (it produced a verdict) carrying the gate's own BLOCK.
    from rebar._mcp_llm import _terminal_from_result

    status, verdict = _terminal_from_result({"verdict": "BLOCK"})
    assert status == "passed"
    assert verdict == "BLOCK"


def test_terminal_from_result_maps_a_pass_verdict_to_passed():
    from rebar._mcp_llm import _terminal_from_result

    status, verdict = _terminal_from_result({"verdict": "PASS"})
    assert status == "passed"
    assert verdict == "PASS"


def test_gate_daemon_records_failed_when_the_gate_raises(store):
    # A gate that raises BEFORE producing a verdict settles the run index at 'failed' (the
    # daemon's ``finally`` records it and releases followers) — the poller never wedges.
    from rebar._mcp_llm import _spawn_gate_daemon

    inflight.reset_registry()
    handle = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested")

    def _boom():
        raise RuntimeError("gate exploded")

    _spawn_gate_daemon(handle, "plan_review", "d80d", _boom)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if gate_runs.gate_run_status(handle.job_id)["status"] in ("failed", "passed"):
            break
        time.sleep(0.02)
    out = gate_runs.gate_run_status(handle.job_id)
    assert out["status"] == "failed"
    assert out["findings"]["reason"] == "run-failed"
    assert "gate exploded" in str(out.get("error") or out.get("verdict") or "")


# ── Tool-level: the async start surface end to end ───────────────────────────────


def test_review_plan_start_returns_a_handle_fast_then_polls_to_passed(store, monkeypatch):
    inflight.reset_registry()
    # The hold only bounds a regressed start; a correct one is released below, long before.
    fake = _BlockingReviewPlan(hold=60)
    import rebar.llm

    monkeypatch.setattr(rebar.llm, "review_plan", fake)
    mcp = _server_with_llm_tools()

    started, gate_finished = _start_review_plan_and_check_gate(mcp, fake)

    assert started["status"] == "running"
    assert not gate_finished, "async start must return a handle without blocking on the gate"
    job_id = started["job_id"]
    # Wait for the daemon to enter the gate, then release it and poll to a terminal verdict.
    assert fake.started.wait(timeout=5)
    assert gate_runs.gate_run_status(job_id)["status"] == "running"
    fake.release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if gate_runs.gate_run_status(job_id)["status"] != "running":
            break
        time.sleep(0.02)
    out = gate_runs.gate_run_status(job_id)
    assert out["status"] == "passed"
    assert fake.calls == 1


def test_review_plan_start_ordering_check_flags_a_synchronous_gate(store, monkeypatch):
    # The oracle above must catch the regression it exists for: a start that runs the gate
    # in the request. Running the daemon body inline is exactly that mutant; the short hold
    # keeps the stub from waiting on a release this test never sends.
    import rebar._mcp_llm as mcp_llm
    import rebar.llm

    inflight.reset_registry()
    fake = _BlockingReviewPlan(hold=0.2)
    monkeypatch.setattr(rebar.llm, "review_plan", fake)
    monkeypatch.setattr(mcp_llm, "spawn_context_daemon", lambda target, *, name=None: target())
    mcp = _server_with_llm_tools()

    _, gate_finished = _start_review_plan_and_check_gate(mcp, fake)

    assert gate_finished, "a start that runs the gate before returning must be flagged"
    assert fake.calls == 1


def test_duplicate_review_plan_start_shares_one_job_and_one_run(store, monkeypatch):
    inflight.reset_registry()
    fake = _BlockingReviewPlan()
    import rebar.llm

    monkeypatch.setattr(rebar.llm, "review_plan", fake)
    mcp = _server_with_llm_tools()

    holder: dict = {}

    async def scenario():
        a = await mcp._tool_manager.call_tool("review_plan_start", {"ticket_id": "d80d"})
        # Second start WHILE the first run is still blocked inside the gate.
        assert fake.started.wait(timeout=5)
        b = await mcp._tool_manager.call_tool("review_plan_start", {"ticket_id": "d80d"})
        holder["a"], holder["b"] = a, b

    anyio.run(scenario)
    assert holder["a"]["job_id"] == holder["b"]["job_id"], "a duplicate start must share the job_id"
    fake.release.set()
    # Give the single daemon a moment to settle; the gate ran exactly once.
    time.sleep(0.2)
    assert fake.calls == 1


def test_verify_completion_start_preserves_graph_tristate_and_variant_key(store, monkeypatch):
    # Keep graph=None. Ticket-type defaults make epics verify their whole subtree.
    # Converting it to False also changes the de-dup key, preventing async start
    # and sync verification from sharing a run.
    import rebar.llm

    seen_graph: list[bool | None] = []

    def fake_verify(ticket_id, *, graph=None, ref=None, source=None):
        seen_graph.append(graph)
        return {"verdict": "PASS", "ticket_id": ticket_id}

    monkeypatch.setattr(rebar.llm, "verify_completion", fake_verify)

    variants: dict[str, str] = {}
    orig_compute = inflight.compute_key

    def spy_compute(gate_type, ticket_id, basis, variant, readonly):
        key = orig_compute(gate_type, ticket_id, basis, variant, readonly)
        if gate_type == "verify_completion":
            variants[variant] = key
        return key

    monkeypatch.setattr(inflight, "compute_key", spy_compute)
    # readonly keeps both bodies store-independent; both share the readonly key dimension, so
    # any key difference is attributable to the graph variant alone.
    mcp = _server_with_llm_tools(readonly=True)

    # (1) async start with default graph -> the daemon calls the gate with graph=None.
    inflight.reset_registry()
    started = anyio.run(
        mcp._tool_manager.call_tool, "verify_completion_start", {"ticket_id": "d80d"}
    )
    job_id = started["job_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if gate_runs.gate_run_status(job_id)["status"] in ("passed", "failed"):
            break
        time.sleep(0.02)
    async_variants = dict(variants)

    # (2) sync verify_completion with default graph.
    variants.clear()
    inflight.reset_registry()
    anyio.run(mcp._tool_manager.call_tool, "verify_completion", {"ticket_id": "d80d"})
    sync_variants = dict(variants)

    # The tri-state is preserved end to end: the gate saw None (whole-subtree default), never
    # a collapsed False.
    assert seen_graph, "the gate must have been invoked on both paths"
    assert all(g is None for g in seen_graph), f"graph tri-state collapsed: {seen_graph}"
    # start and sync build the IDENTICAL variant + key, so they de-dup against each other.
    assert async_variants == sync_variants
    # The variant also carries the ticket's plan-material fingerprint (bug c912-f274); ``d80d``
    # does not exist in this store, so the light read yields the ``material=None`` key.
    assert list(async_variants) == ["graph=None;source=attested;material=None"]


def test_follower_start_does_not_reclobber_the_leaders_index_record(store, monkeypatch):
    # A follower attaches without writing the last-writer-wins index. A new
    # running record could overwrite the leader's terminal result. Count writes
    # while the leader is blocked to verify the follower records nothing.
    from rebar import _mcp_llm

    inflight.reset_registry()

    records: list[str] = []
    orig_record = gate_runs.record_gate_run

    def counting_record(record, **kwargs):
        records.append(str(record.get("status")))
        return orig_record(record, **kwargs)

    import rebar.llm

    monkeypatch.setattr(rebar.llm, "record_gate_run", counting_record)

    started = threading.Event()
    release = threading.Event()

    def blocking_work():
        started.set()
        release.wait(timeout=5)
        return {"verdict": "PASS"}

    call = dict(
        ref=None,
        source="attested",
        variant="graph=None;source=attested",
        readonly=True,
        force=False,
        work=blocking_work,
    )
    try:
        leader = _mcp_llm._start_gate_job("verify_completion", "d80d", **call)
        assert started.wait(timeout=5), "the leader daemon must enter its (blocked) work"
        # Exactly one write so far: the leader's 'running' handle. The daemon is blocked in
        # ``work`` and has NOT reached its terminal ``finally`` record yet.
        assert records == ["running"]
        follower = _mcp_llm._start_gate_job("verify_completion", "d80d", **call)
        # The follower ATTACHES (same job_id) and, thanks to the guard, writes nothing.
        assert follower["job_id"] == leader["job_id"]
        assert records == ["running"], f"follower re-recorded the index: {records}"
    finally:
        release.set()
    # After release the leader daemon settles and records its terminal status exactly once.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if gate_runs.gate_run_status(leader["job_id"])["status"] != "running":
            break
        time.sleep(0.02)
    assert gate_runs.gate_run_status(leader["job_id"])["status"] == "passed"
    assert records == ["running", "passed"]


# ── Convergence matrix (bug d80d): {leader, follower, forced, dedup-disabled} × properties ──


def test_forced_start_registers_an_observable_active_entry():
    # MATRIX forced×(a,c) — TEETH: a force=True start owns no content-keyed slot, but it MUST
    # still report active for its daemon lifetime, or a status poll of a forced 15-20 min run
    # reads 'settled' while the gate runs. RED before the fix (no registry entry => not active).
    inflight.reset_registry()
    handle = inflight.begin_gate_job("verify_completion", "d80d", force=True)
    assert handle.is_new is True
    assert inflight.is_job_active(handle.job_id) is True, "a forced in-flight job must be active"
    handle.complete(result={"verdict": "PASS"})
    assert inflight.is_job_active(handle.job_id) is False, "completion must purge the entry"


def test_dedup_disabled_start_registers_an_observable_active_entry(monkeypatch):
    # MATRIX dedup-disabled×(a,c): the REBAR_MCP_DEDUP=0 kill-switch takes the same bypass path
    # as force=True — it too must stay observable as active.
    monkeypatch.setenv("REBAR_MCP_DEDUP", "0")
    inflight.reset_registry()
    handle = inflight.begin_gate_job("plan_review", "d80d")
    assert handle.is_new is True, "the kill-switch disables attach, so every start is a leader"
    assert inflight.is_job_active(handle.job_id) is True
    handle.complete()
    assert inflight.is_job_active(handle.job_id) is False


def test_two_forced_starts_are_independent_and_both_active():
    # MATRIX forced×(d): two forced starts never share a job_id (never attach) and have
    # INDEPENDENT lifetimes — completing one leaves the other active, so neither can clobber
    # the other's terminal record.
    inflight.reset_registry()
    a = inflight.begin_gate_job("verify_completion", "d80d", force=True)
    b = inflight.begin_gate_job("verify_completion", "d80d", force=True)
    assert a.job_id != b.job_id
    assert inflight.is_job_active(a.job_id) and inflight.is_job_active(b.job_id)
    a.complete()
    assert inflight.is_job_active(a.job_id) is False
    assert inflight.is_job_active(b.job_id) is True
    b.complete()


def test_follower_poll_before_leader_index_write_reports_attaching(store):
    # A follower shares the leader's job_id without an index write. Before the
    # leader writes running, the shared registry must report attaching, not unknown.
    inflight.reset_registry()
    leader = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested")
    follower = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested")
    assert leader.is_new is True and follower.is_new is False
    assert follower.job_id == leader.job_id
    assert gate_runs.read_gate_run(leader.job_id) is None, "no index record has been written yet"
    out = gate_runs.gate_run_status(leader.job_id)
    assert out["status"] == "attaching"
    leader.complete()


def test_unknown_job_still_reports_unknown_not_attaching(store):
    # MATRIX guard: 'attaching' must be reserved for a genuinely in-flight job — a job_id that
    # was never registered still reads 'unknown'.
    inflight.reset_registry()
    assert gate_runs.gate_run_status("never-existed")["status"] == "unknown"


def test_gate_status_includes_durable_unknown_when_the_durable_read_raises(store, monkeypatch):
    # A recorded completion job with ticket_id carries durable. If its read
    # raises, report unknown with a reason and retain the field.
    def boom(*a, **k):
        raise RuntimeError("attestation store unreadable")

    monkeypatch.setattr(gate_runs, "verify_completion_status", boom)
    gate_runs.record_gate_run(
        {
            "job_id": "verify-boom",
            "ticket_id": "d80d",
            "gate_type": "verify_completion",
            "status": "passed",
            "verdict": "PASS",
            "finished_at": time.time(),
        }
    )
    inflight.reset_registry()
    out = gate_runs.gate_run_status("verify-boom")
    assert out["status"] == "passed"
    assert "durable" in out, "durable must never be silently omitted on a read error"
    assert out["durable"]["ok"] is False
    assert out["durable"]["verdict"] == "unknown"
    assert "unreadable" in out["durable"]["reason"]


# ── Bug cb1b: every gate-running MCP surface has an async counterpart ─────────────────


class _BlockingOp:
    """A stand-in for a gate-running library op that blocks until released, counting calls.

    ``result`` is what the op returns once released; ``raises`` makes it raise instead."""

    def __init__(self, result: dict, hold: float = 5, raises: BaseException | None = None):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self._hold = hold
        self._result = result
        self._raises = raises
        self._lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        with self._lock:
            self.calls += 1
        self.started.set()
        self.release.wait(timeout=self._hold)
        self.finished.set()
        if self._raises is not None:
            raise self._raises
        return dict(self._result)


# (starter tool, library attribute it drives, the module holding it, args, result, verdict)
_CODE_REVIEW_RESULT = {
    "findings": [],
    "summary": "PASS: 0 blocking, 0 advisory.",
    "verdict": {"verdict": "PASS", "runner": "code-review-gate"},
}
_STARTERS = [
    pytest.param(
        "review_code_start",
        ("rebar.llm", "review_code"),
        {"base": "HEAD~1", "head": "HEAD"},
        _CODE_REVIEW_RESULT,
        "PASS",
        "code_review",
        id="review_code_start",
    ),
    pytest.param(
        "transition_ticket_start",
        ("rebar", "transition"),
        {"ticket_id": "d80d", "current_status": "in_progress", "target_status": "closed"},
        {"ticket_id": "d80d", "from": "in_progress", "to": "closed", "newly_unblocked": []},
        "applied",
        "transition",
        id="transition_ticket_start",
    ),
]


def _server_with_gate_starters():
    """A server carrying every gate starter: the LLM registrar plus the ticket-op starter."""
    from rebar._mcp_ticket_async import register_ticket_async_tools

    mcp = _server_with_llm_tools()
    ctx = SimpleNamespace(allow_llm=lambda: True, readonly=lambda: False)
    register_ticket_async_tools(mcp, ctx)
    return mcp


def _patch_op(monkeypatch, target: tuple[str, str], op) -> None:
    import importlib

    monkeypatch.setattr(importlib.import_module(target[0]), target[1], op)


def _poll_terminal(job_id: str, budget: float = 5) -> dict:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        out = gate_runs.gate_run_status(job_id)
        if out["status"] in ("passed", "failed"):
            return out
        time.sleep(0.02)
    return gate_runs.gate_run_status(job_id)


@pytest.mark.parametrize(("tool", "target", "args", "result", "verdict", "gate_type"), _STARTERS)
def test_gate_starter_returns_its_handle_before_the_gate_completes(
    store, monkeypatch, tool, target, args, result, verdict, gate_type
):
    # AC1/AC2/AC4: the handle comes back while the op is still held inside the gate, and the job
    # then polls through gate_status to a terminal verdict. A terminal-only check would pass
    # against the blocking surfaces this bug is about.
    inflight.reset_registry()
    op = _BlockingOp(result, hold=60)
    _patch_op(monkeypatch, target, op)
    mcp = _server_with_gate_starters()
    holder: dict = {}

    async def scenario():
        holder["handle"] = await mcp._tool_manager.call_tool(tool, args)
        holder["op_finished"] = op.finished.is_set()

    anyio.run(scenario)
    handle = holder["handle"]
    assert not holder["op_finished"], f"{tool} must return without waiting on the gate"
    assert handle["status"] == "running"
    assert handle["gate_type"] == gate_type
    assert set(handle) == {"job_id", "ticket_id", "gate_type", "status"}
    assert op.started.wait(timeout=5)
    assert gate_runs.gate_run_status(handle["job_id"])["status"] == "running"
    op.release.set()
    out = _poll_terminal(handle["job_id"])
    assert out["status"] == "passed"
    assert out["verdict"] == verdict
    assert out["gate_type"] == gate_type
    assert op.calls == 1


@pytest.mark.parametrize(("tool", "target", "args", "result", "verdict", "gate_type"), _STARTERS)
def test_gate_starter_refire_attaches_to_the_inflight_run(
    store, monkeypatch, tool, target, args, result, verdict, gate_type
):
    # AC7: the re-fire reflex after a timeout attaches to the live job instead of a second run.
    inflight.reset_registry()
    op = _BlockingOp(result)
    _patch_op(monkeypatch, target, op)
    mcp = _server_with_gate_starters()
    holder: dict = {}

    async def scenario():
        holder["a"] = await mcp._tool_manager.call_tool(tool, args)
        assert op.started.wait(timeout=5)
        holder["b"] = await mcp._tool_manager.call_tool(tool, args)

    try:
        anyio.run(scenario)
    finally:
        op.release.set()
    assert holder["a"]["job_id"] == holder["b"]["job_id"]
    assert _poll_terminal(holder["a"]["job_id"])["status"] == "passed"
    assert op.calls == 1


_CLOSE_ARGS = {"ticket_id": "d80d", "current_status": "in_progress", "target_status": "closed"}


def test_transition_starter_reports_a_gate_refusal_as_failed(store, monkeypatch):
    # A gate refusal leaves the ticket where it was and settles the job as failed with the
    # refusal message, so the poller learns why the status change was not applied.
    inflight.reset_registry()
    op = _BlockingOp({}, hold=0, raises=rebar.RebarError("completion gate refused: FAIL"))
    monkeypatch.setattr(rebar, "transition", op)
    mcp = _server_with_gate_starters()
    handle = anyio.run(mcp._tool_manager.call_tool, "transition_ticket_start", _CLOSE_ARGS)
    out = _poll_terminal(handle["job_id"])
    assert out["status"] == "failed"
    assert "completion gate refused" in str(out.get("error"))


def test_close_starter_never_attaches_to_a_completion_check_job(store, monkeypatch):
    # A close RUNS completion verification. Keyed on gate_type verify_completion it would attach
    # to a live verify_completion_start for the same ticket, report that check's "passed", and
    # never transition.
    inflight.reset_registry()
    check = _BlockingOp({"verdict": "PASS"})
    close = _BlockingOp({"ticket_id": "d80d", "from": "in_progress", "to": "closed"})
    import rebar.llm

    monkeypatch.setattr(rebar.llm, "verify_completion", check)
    monkeypatch.setattr(rebar, "transition", close)
    mcp = _server_with_gate_starters()
    holder: dict = {}

    async def scenario():
        holder["check"] = await mcp._tool_manager.call_tool(
            "verify_completion_start", {"ticket_id": "d80d"}
        )
        assert check.started.wait(timeout=5)
        holder["close"] = await mcp._tool_manager.call_tool("transition_ticket_start", _CLOSE_ARGS)

    try:
        anyio.run(scenario)
        assert close.started.wait(timeout=5), "the close must run, not inherit the check"
    finally:
        check.release.set()
        close.release.set()
    assert holder["close"]["job_id"] != holder["check"]["job_id"]
    assert holder["close"]["gate_type"] == "transition"
    assert _poll_terminal(holder["close"]["job_id"])["status"] == "passed"
    assert close.calls == 1


def test_transition_job_reports_the_durable_completion_certificate(store):
    # AC3: a close poller sees whether the completion certificate persisted.
    gate_runs.record_gate_run(
        {
            "job_id": "close-1",
            "ticket_id": "d80d",
            "gate_type": "transition",
            "status": "passed",
            "verdict": "applied",
            "finished_at": time.time(),
        }
    )
    inflight.reset_registry()
    out = gate_runs.gate_run_status("close-1")
    assert out["gate_type"] == "transition"
    assert out["verdict"] == "applied"
    assert "durable" in out and "verdict" in out["durable"]


def test_terminal_from_result_reads_the_code_review_gate_verdict():
    # review_result nests the gate verdict as a dict; the run index records its PASS/BLOCK.
    from rebar._mcp_llm import _terminal_from_result

    assert _terminal_from_result({"verdict": {"verdict": "BLOCK"}}) == ("passed", "BLOCK")


# The surfaces that run the plan-review, completion-verifier, or code-review gate inline.
# claim_ticket is absent on purpose: its claim gate is the local, no-LLM claim_gate_check.
_GATE_RUNNING_TOOLS = frozenset(
    {"review_plan", "verify_completion", "review_code", "transition_ticket"}
)
# Billable LLM tools that are not one of those three gates, with the reason.
_NOT_GATE_SURFACES = {
    "scan_spec": "billable spec scan over epics; it runs none of the three gates",
}


def _live_tool_names() -> set[str]:
    from rebar.mcp_server import build_server

    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("REBAR_MCP_READONLY", raising=False)
        return {t.name for t in anyio.run(build_server().list_tools)}


def test_every_gate_running_mcp_tool_has_an_async_counterpart():
    # AC5: the invariant itself, against the real server. A gate surface without a
    # ``<name>_start`` forces callers into a request that outlives the client deadline.
    names = _live_tool_names()
    assert _GATE_RUNNING_TOOLS <= names
    missing = sorted(t for t in _GATE_RUNNING_TOOLS if f"{t}_start" not in names)
    assert not missing, f"gate-running MCP tools without an async *_start: {missing}"


def test_gate_running_roster_covers_every_billable_llm_tool():
    # Keeps the roster above honest: every LLM-registrar tool that makes a billable call is
    # either a gate surface (and so needs a starter) or listed with a reason it is not.
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("roster")
    register_llm_tools(mcp, SimpleNamespace(allow_llm=lambda: False, readonly=lambda: False))
    billable = {
        name
        for name, tool in mcp._tool_manager._tools.items()
        if "REBAR_MCP_ALLOW_LLM" in (tool.description or "") and not name.endswith("_start")
    }
    unclassified = billable - _GATE_RUNNING_TOOLS - set(_NOT_GATE_SURFACES)
    assert not unclassified, f"classify these billable tools for the cb1b invariant: {unclassified}"


# ── Cross-process owner lock (bug digestive-greasy-kingsnake) ─────────────────────
#
# After a blue-green cutover the new MCP container polls a job whose daemon still runs in
# the draining container. Its in-memory registry has no entry for that job, so the poll
# used to write ``failed / daemon-stopped`` into the shared index. The owner lock is held
# by the process that runs the job and released by the kernel when that process dies.

_HOLD_OWNER_LOCK = (
    "import os, sys\n"
    "from rebar._store import lock_kernel\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "lock_kernel.take_exclusive(fd)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


def _record_stale_running(job_id: str) -> None:
    gate_runs.record_gate_run(
        {
            "job_id": job_id,
            "ticket_id": "d80d",
            "gate_type": "plan_review",
            "status": "running",
            "started_at": time.time() - (gate_runs._STALE_GRACE_SECONDS + 1.0),
        }
    )


def test_gate_status_keeps_a_job_whose_owner_in_another_process_is_alive(store):
    import sys

    inflight.reset_registry()
    job_id = "owned-elsewhere"
    _record_stale_running(job_id)
    index_file = store / ".rebar" / "gate_runs" / job_id
    before = index_file.read_bytes()
    owner = subprocess.Popen(
        [sys.executable, "-c", _HOLD_OWNER_LOCK, str(gate_runs.owner_lock_path(job_id))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None and owner.stdout.readline().strip() == "held"

        out = gate_runs.gate_run_status(job_id)

        assert out["status"] == "running", out
        assert index_file.read_bytes() == before, "the poll rewrote a live owner's index"
    finally:
        assert owner.stdin is not None
        owner.stdin.close()
        try:
            owner.wait(timeout=30)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.wait(timeout=30)

    # The owner is gone and the kernel released its lock: the job is now stranded.
    out = gate_runs.gate_run_status(job_id)
    assert out["status"] == "failed"
    assert out["verdict"] == "daemon-stopped"
    persisted = gate_runs.read_gate_run(job_id)
    assert persisted is not None and persisted["status"] == "failed"


def test_review_plan_start_holds_the_owner_lock_until_the_terminal_record(store, monkeypatch):
    import os

    from rebar._store import lock_kernel

    def _lock_is_free(job_id: str) -> bool:
        fd = os.open(gate_runs.owner_lock_path(job_id), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            lock_kernel.take_exclusive(fd)
        except OSError as exc:
            if not lock_kernel.is_contention(exc):
                raise
            return False
        finally:
            os.close(fd)
        return True

    inflight.reset_registry()
    fake = _BlockingReviewPlan(hold=60)
    import rebar.llm

    monkeypatch.setattr(rebar.llm, "review_plan", fake)
    mcp = _server_with_llm_tools()

    started, _gate_finished = _start_review_plan_and_check_gate(mcp, fake)
    job_id = started["job_id"]
    assert fake.started.wait(timeout=5)
    assert not _lock_is_free(job_id), "the running job does not hold its owner lock"
    fake.release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if gate_runs.gate_run_status(job_id)["status"] != "running":
            break
        time.sleep(0.02)
    assert gate_runs.gate_run_status(job_id)["status"] == "passed"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and gate_runs.owner_lock_path(job_id).exists():
        time.sleep(0.02)
    assert not gate_runs.owner_lock_path(job_id).exists() or _lock_is_free(job_id)


def test_gate_daemon_writes_its_terminal_record_to_the_root_it_started_in(
    store, tmp_path, monkeypatch
):
    """The daemon outlives the request; its records stay in the starting store's root.

    Resolving the root at write time followed whatever REBAR_ROOT/cwd the process had by
    then. In the suite that was the checkout once the starting test's monkeypatch was
    undone, so ``.rebar/gate_runs`` leaked into REPO_ROOT (coexistent-unclear-icefish).
    """
    from rebar import _mcp_llm

    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, check=True, capture_output=True)
    inflight.reset_registry()
    release = threading.Event()

    def work():
        release.wait(timeout=10)
        return {"verdict": "PASS"}

    handle = _mcp_llm._start_gate_job(
        "plan_review",
        "d80d",
        ref=None,
        source=None,
        variant="",
        readonly=False,
        force=False,
        work=work,
    )
    monkeypatch.setenv("REBAR_ROOT", str(other))  # the process moved on before the gate ended
    release.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        record = gate_runs.read_gate_run(handle["job_id"], repo_root=str(store))
        if record and record.get("status") != "running":
            break
        time.sleep(0.02)

    assert record and record["status"] == "passed"
    assert not (other / ".rebar" / "gate_runs").exists()
