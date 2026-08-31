"""Phase-2 async gate surface (bug d80d): ``*_start`` handles + durable poll.

Phase 1 de-dups a concurrent retry; Phase 2 removes the client timeout from the
request path entirely with an async ``review_plan_start`` / ``verify_completion_start``
that return a durable ``job_id`` in milliseconds while the gate runs on a background
daemon thread. These tests pin the four behaviours the ticket calls out — a sub-100ms
handle while the gate blocks, a poll that transitions running -> passed, a duplicate
``*_start`` that shares ONE run's job_id, and a fresh (post-restart) registry that still
resolves the durable verdict from the on-disk store — deterministically and with zero
tokens (the gate is a fake that blocks on a ``threading.Event``).
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
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-d80d")
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
    """A stand-in for ``rebar.llm.review_plan`` that blocks until released, counting calls."""

    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def __call__(
        self, ticket_id, *, ref=None, source=None, sign=True, emit_sidecar=True, force=False
    ):
        with self._lock:
            self.calls += 1
        self.started.set()
        self.release.wait(timeout=5)
        return {"verdict": "PASS", "ticket_id": ticket_id}


# ── begin_gate_job: the non-blocking singleflight reservation ────────────────────


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


# ── gate_run_status: resolving a handle to a poll record ─────────────────────────


def test_gate_status_unknown_for_an_unrecorded_job(store):
    assert gate_runs.gate_run_status("no-such-job")["status"] == "unknown"


def test_gate_status_running_then_terminal_from_the_index(store):
    inflight.reset_registry()
    # A recorded run whose daemon is still active reads 'running'…
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
    assert gate_runs.gate_run_status(handle.job_id)["status"] == "running"
    # …and the recorded terminal status wins once the daemon settles.
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


def test_gate_status_stale_running_when_daemon_gone_and_index_still_running(store):
    inflight.reset_registry()
    # An index that reads 'running' with NO active daemon and past the grace window is a
    # crashed leader — surfaced as stale-running (the run_finished marker never fired).
    gate_runs.record_gate_run(
        {
            "job_id": "wedged-job",
            "ticket_id": "d80d",
            "gate_type": "plan_review",
            "status": "running",
            "started_at": time.time() - (gate_runs._STALE_GRACE_SECONDS + 1.0),
        }
    )
    assert gate_runs.gate_run_status("wedged-job")["status"] == "stale-running"


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
    # 'running' (not prematurely 'stale-running'). This is the sibling of the past-grace case.
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
    # ``verify_signature`` returns the manifest as a LIST (the signed op-cert steps), so the
    # status read MUST resolve ``verified_at_sha`` from that list — the pinned
    # ``verified-at-sha:`` step. The prior guard tested ``isinstance(manifest, dict)`` (always
    # False for the list-shaped manifest) so ``verified_at_sha`` stayed None forever; this pins
    # the fixed branch (a faithful op-cert verdict avoids the heavy signing/ssh setup).
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
    # TEETH (bug d80d LLM-Review): a completion attestation whose signature still verifies but
    # that was REOPENED after signing is NO LONGER current. The prior code read ``certified``
    # straight off ``sig.get('verified')`` (a bare HMAC check) and so reported a superseded
    # verdict as current. Routing through ``compute_validity`` — exactly as the sibling
    # ``plan_review_status`` does — makes the reopen surface as ``stale-reopened``/not-current.
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
    assert "gate exploded" in str(out.get("error") or out.get("verdict") or "")


# ── Tool-level: the async start surface end to end ───────────────────────────────


def test_review_plan_start_returns_a_handle_fast_then_polls_to_passed(store, monkeypatch):
    inflight.reset_registry()
    fake = _BlockingReviewPlan()
    import rebar.llm

    monkeypatch.setattr(rebar.llm, "review_plan", fake)
    mcp = _server_with_llm_tools()

    holder: dict = {}

    async def scenario():
        # The gate stays blocked on ``fake.release`` for the whole scenario, so a start body
        # that (incorrectly) blocked on the gate could never return here. ``fail_after`` turns
        # that into a deterministic failure instead of a hang; the wall-clock bound below then
        # only has to clear one-time cold pydantic/thread-pool warm-up, not race the gate.
        t0 = time.monotonic()
        with anyio.fail_after(3):
            started = await mcp._tool_manager.call_tool("review_plan_start", {"ticket_id": "d80d"})
        holder["elapsed"] = time.monotonic() - t0
        holder["started"] = started

    anyio.run(scenario)

    started = holder["started"]
    assert started["status"] == "running"
    assert holder["elapsed"] < 2, "async start must return a handle without blocking on the gate"
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
    # BLOCKING #1 (bug d80d Phase 2): the async ``*_start`` must NOT collapse the ``graph``
    # tri-state. Unspecified is ``None`` ("use the ticket-type default"; an epic verifies its
    # WHOLE SUBTREE) — a plain-bool ``False`` default silently drops that. And because the
    # de-dup variant key embeds ``graph``, a collapsed default hashes to a DIFFERENT variant
    # than the sync tool, so a start and its sync twin would fail to attach to one run. This
    # pins BOTH: the gate sees ``None`` (never ``False``), and start/sync share the variant key.
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
    assert list(async_variants) == ["graph=None;source=attested"]


def test_follower_start_does_not_reclobber_the_leaders_index_record(store, monkeypatch):
    # GUARD (bug d80d Phase 2 advisory): a follower ``*_start`` (is_new=False) ATTACHES to the
    # in-flight run and must NOT re-record a 'running' handle — the index is last-writer-wins,
    # so a follower write would clobber the leader daemon's (eventual) terminal record. Prove
    # the guard by counting index writes: the leader records once ('running') while its work
    # blocks; a follower issued during that window records NOTHING.
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
