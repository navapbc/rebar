"""Unit tests for the Tier-1 enrichment drain + `rebar enrich` CLI (only-crave-art, c1de)."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._store import event_append
from rebar.llm import enrich_drain as D
from rebar.llm.overlap import digest_sidecar as ds
from rebar.llm.overlap import queue as Q
from rebar.llm.runner import Runner, RunRequest

_DIGEST = {
    "problem_keywords": ["overlap"],
    "component_or_area": "gate",
    "key_entities": ["review_plan"],
    "propositions": ["detect overlap", "advisory suggestions"],
}


class _DigestRunner(Runner):
    name = "digest"

    def run(self, req: RunRequest) -> dict:
        return {**_DIGEST, "runner": self.name, "model": None, "trace_id": None}

    def preflight(self) -> None:
        pass


class _BoomRunner(Runner):
    name = "boom"

    def run(self, req: RunRequest) -> dict:
        raise RuntimeError("llm down")

    def preflight(self) -> None:
        pass


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    rebar.init_repo(repo_root=str(r))
    monkeypatch.setattr(ds, "_active_model", lambda repo_root: "claude-opus-4-8")
    # Pin the opportunistic write-path drain OFF for this fixture's baseline. The write
    # path calls maybe_drain() with repo_root=None, so its overlap gate reads the AMBIENT
    # checkout config (cwd) — and this repo enables verify.overlap_enabled. Without this,
    # every create_ticket/enqueue during test setup would spawn a drain child, polluting
    # the _spawn_detached_drain spies and racing the queue in test_batch_cap. Tests that
    # exercise a drain mode opt in explicitly via _mock_flags(drain=...) / direct D.drain.
    monkeypatch.setenv("REBAR_LLM_OVERLAP_DRAIN", "off")
    return str(r)


def _tracker(repo: str) -> str:
    from rebar._commands._seam import tracker_dir

    return str(tracker_dir(repo))


def test_done_enrich_registered() -> None:
    for et in ("ENQUEUE_ENRICH", "CLAIM_ENRICH", "DONE_ENRICH"):
        assert et in event_append.EVENT_TYPES


def test_drain_once(repo: str) -> None:
    tid = rebar.create_ticket("task", "Drain me", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)  # eligible immediately
    result = D.drain(_tracker(repo), once=True, repo_root=repo, runner=_DigestRunner())
    assert result["processed"] == 1
    # The digest was written and the ticket is no longer pending (DONE tombstone + prune).
    assert ds.latest_ticket_digest(tid, repo_root=repo) is not None
    assert Q.reduce_ticket(tid, _tracker(repo))["pending"] is False
    assert Q.pending_enrichment(Q._now_ns(), _tracker(repo)) == []


def test_batch_cap(repo: str) -> None:
    tids = [rebar.create_ticket("task", f"T{i}", repo_root=repo) for i in range(8)]
    for t in tids:
        Q.enqueue(t, soak_min=0, repo_root=repo, now_ns=1000)
    first = D.drain(_tracker(repo), repo_root=repo, runner=_DigestRunner())
    assert first["processed"] == 5  # DEFAULT_OVERLAP_DRAIN_BATCH
    second = D.drain(_tracker(repo), repo_root=repo, runner=_DigestRunner())
    assert second["processed"] == 3  # backlog drains over successive runs
    assert Q.pending_enrichment(Q._now_ns(), _tracker(repo)) == []


def test_lock_held_skip(repo: str) -> None:
    tid = rebar.create_ticket("task", "Locked", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    fd = D._acquire_advisory_lock(_tracker(repo))
    assert fd is not None
    try:
        result = D.drain(_tracker(repo), repo_root=repo, runner=_DigestRunner())
        assert result.get("skipped") == "lock-held"
        assert result["processed"] == 0
    finally:
        D._release_advisory_lock(_tracker(repo), fd)


def test_enrich_error_continues(repo: str) -> None:
    tid = rebar.create_ticket("task", "Fails", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    result = D.drain(_tracker(repo), repo_root=repo, runner=_BoomRunner())  # enrich raises
    assert result["processed"] == 0  # no raise; item not marked done
    assert Q.reduce_ticket(tid, _tracker(repo))["done"] is False  # re-pickable later


def test_self_heal_stale_digest(repo: str) -> None:
    tid = rebar.create_ticket("task", "Stale", repo_root=repo)
    ds.emit(dict(_DIGEST), tid, model="claude-opus-4-8", repo_root=repo)
    rebar.edit_ticket(tid, description="content drifted", repo_root=repo)  # → stale digest
    assert ds.freshness(tid, repo_root=repo) == "present-stale"
    # No queue entry, but the fallback scan picks up the stale digest and re-enriches.
    result = D.drain(_tracker(repo), repo_root=repo, runner=_DigestRunner())
    assert result["processed"] == 1
    assert ds.freshness(tid, repo_root=repo) == "present-fresh"


def test_status_buckets(repo: str) -> None:
    now = 10_000_000_000_000
    soaking = rebar.create_ticket("task", "Soaking", repo_root=repo)
    pending = rebar.create_ticket("task", "Pending", repo_root=repo)
    claimed = rebar.create_ticket("task", "Claimed", repo_root=repo)
    Q.enqueue(soaking, soak_min=60, repo_root=repo, now_ns=now)  # not_before = now+60m
    Q.enqueue(pending, soak_min=0, repo_root=repo, now_ns=now)
    Q.enqueue(claimed, soak_min=0, repo_root=repo, now_ns=now)
    Q.claim(claimed, "d", lease_ttl_min=15, now_ns=now + 1, repo_root=repo)
    st = D.status(_tracker(repo), now_ns=now + 1, repo_root=repo)
    assert st == {"pending": 1, "claimed": 1, "soaking": 1}


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar.llm.config import LLMConfig

    monkeypatch.setenv("REBAR_LLM_OVERLAP_DRAIN", "always")
    monkeypatch.setenv("REBAR_LLM_OVERLAP_DRAIN_BATCH", "9")
    monkeypatch.setenv("REBAR_LLM_OVERLAP_DRAIN_GATE_BUDGET_MS", "42")
    cfg = LLMConfig.from_env()
    assert cfg.overlap_drain == "always"
    assert cfg.overlap_drain_batch == 9
    assert cfg.overlap_drain_gate_budget_ms == 42
    # An invalid enum value falls back to the default.
    monkeypatch.setenv("REBAR_LLM_OVERLAP_DRAIN", "nonsense")
    assert LLMConfig.from_env().overlap_drain == "async"


def test_windows_detach_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(D.os, "name", "nt")
    monkeypatch.setattr(D.subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(D.subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    kw = D._detach_kwargs()
    assert "creationflags" in kw
    assert "start_new_session" not in kw


def _mock_flags(monkeypatch, *, enabled=True, drain="always", agents=True):
    from rebar import config as rc
    from rebar.llm import config as lc

    real_load = rc.load_config

    def _patched(repo_root=None):
        c = real_load(repo_root)
        c.verify.overlap_enabled = enabled  # VerifyConfig is a mutable dataclass
        return c

    monkeypatch.setattr(rc, "load_config", _patched)
    monkeypatch.setattr(lc, "agents_extra_installed", lambda: agents)
    monkeypatch.setenv("REBAR_LLM_OVERLAP_DRAIN", drain)


def test_maybe_drain_off_and_no_key_and_windows(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(D, "drain", lambda *a, **k: calls.append("drain") or {})
    monkeypatch.setattr(D, "_spawn_detached_drain", lambda *a, **k: calls.append("spawn"))
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    tracker = _tracker(repo)

    # overlap disabled → no-op
    _mock_flags(monkeypatch, enabled=False)
    D.maybe_drain(tracker, repo_root=repo)
    # drain=off → no-op
    _mock_flags(monkeypatch, enabled=True, drain="off")
    D.maybe_drain(tracker, repo_root=repo)
    # no agents extra → no-op
    _mock_flags(monkeypatch, enabled=True, drain="always", agents=False)
    D.maybe_drain(tracker, repo_root=repo)
    # windows → no-op
    _mock_flags(monkeypatch, enabled=True, drain="always")
    monkeypatch.setattr(D.os, "name", "nt")
    D.maybe_drain(tracker, repo_root=repo)
    assert calls == []


def test_maybe_drain_always_runs_inline(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(D, "drain", lambda *a, **k: calls.append("drain") or {})
    monkeypatch.setattr(D, "_spawn_detached_drain", lambda *a, **k: calls.append("spawn"))
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    _mock_flags(monkeypatch, enabled=True, drain="always")
    monkeypatch.setattr(D.os, "name", "posix")
    D.maybe_drain(_tracker(repo), repo_root=repo)
    assert calls == ["drain"]  # inline, no detached spawn


def test_maybe_drain_async_detaches(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(D, "drain", lambda *a, **k: calls.append("drain") or {})
    monkeypatch.setattr(D, "_spawn_detached_drain", lambda *a, **k: calls.append("spawn"))
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    _mock_flags(monkeypatch, enabled=True, drain="async")
    monkeypatch.setattr(D.os, "name", "posix")
    D.maybe_drain(_tracker(repo), repo_root=repo)
    assert calls == ["spawn"]


def test_cli_dispatch_status(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    buf = io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(buf):
        rc = D.cmd_enrich(["status"], _tracker(repo))
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert set(out) == {"pending", "claimed", "soaking"}


# ── AC-named proving tests (epic only-crave-art / c1de acceptance criteria) ──────
def test_gate_latency(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """maybe_drain no-ops with NO spawn (and returns within the gate budget) when nothing is
    soaked."""
    calls = []
    monkeypatch.setattr(D, "drain", lambda *a, **k: calls.append("drain") or {})
    monkeypatch.setattr(D, "_spawn_detached_drain", lambda *a, **k: calls.append("spawn"))
    _mock_flags(monkeypatch, enabled=True, drain="async")
    monkeypatch.setattr(D.os, "name", "posix")
    # NOTHING enqueued → nothing soaked → cheap gate no-ops, no child spawned.
    import time as _t

    start = _t.perf_counter()
    D.maybe_drain(_tracker(repo), repo_root=repo)
    elapsed_ms = (_t.perf_counter() - start) * 1000
    assert calls == []
    assert elapsed_ms < 500  # the cheap gate is well under any interactive budget


def test_no_key_noop(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """[agents]/key absent → clean no-op (no spawn)."""
    calls = []
    monkeypatch.setattr(D, "drain", lambda *a, **k: calls.append("drain") or {})
    monkeypatch.setattr(D, "_spawn_detached_drain", lambda *a, **k: calls.append("spawn"))
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    _mock_flags(monkeypatch, enabled=True, drain="always", agents=False)
    monkeypatch.setattr(D.os, "name", "posix")
    D.maybe_drain(_tracker(repo), repo_root=repo)
    assert calls == []


def test_opt_out(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """REBAR_LLM_OVERLAP_DRAIN=off disables the opportunistic drain (single canonical name)."""
    calls = []
    monkeypatch.setattr(D, "drain", lambda *a, **k: calls.append("drain") or {})
    monkeypatch.setattr(D, "_spawn_detached_drain", lambda *a, **k: calls.append("spawn"))
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    _mock_flags(monkeypatch, enabled=True, drain="off")
    monkeypatch.setattr(D.os, "name", "posix")
    D.maybe_drain(_tracker(repo), repo_root=repo)
    assert calls == []


def test_windows_noop(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """maybe_drain no-ops on Windows (v1) — no child spawned (lock.py fcntl would crash it)."""
    calls = []
    monkeypatch.setattr(D, "drain", lambda *a, **k: calls.append("drain") or {})
    monkeypatch.setattr(D, "_spawn_detached_drain", lambda *a, **k: calls.append("spawn"))
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    tracker = _tracker(repo)  # resolve BEFORE mocking os.name (pathlib WindowsPath guard)
    _mock_flags(monkeypatch, enabled=True, drain="always")
    monkeypatch.setattr(D.os, "name", "nt")
    D.maybe_drain(tracker, repo_root=repo)
    assert calls == []


def test_always_mode(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """overlap_drain='always' runs the drain SYNCHRONOUSLY inline (no detached child)."""
    calls = []
    monkeypatch.setattr(D, "drain", lambda *a, **k: calls.append("drain") or {})
    monkeypatch.setattr(D, "_spawn_detached_drain", lambda *a, **k: calls.append("spawn"))
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    _mock_flags(monkeypatch, enabled=True, drain="always")
    monkeypatch.setattr(D.os, "name", "posix")
    D.maybe_drain(_tracker(repo), repo_root=repo)
    assert calls == ["drain"]


def test_cli_dispatch(repo: str) -> None:
    """rebar enrich status routes through cmd_enrich (the _cli/__init__.py intercept) and
    returns 0 with the three status buckets."""
    import contextlib
    import io as _io
    import json as _json

    tid = rebar.create_ticket("task", "T", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = D.cmd_enrich(["status"], _tracker(repo))
    assert rc == 0
    assert set(_json.loads(buf.getvalue())) == {"pending", "claimed", "soaking"}


def test_prune_keep1_count(repo: str) -> None:
    """After a drain, each processed ticket retains EXACTLY ONE queue event (keep=1 prune of
    the DONE_ENRICH tombstone; superseded ENQUEUE/CLAIM dropped)."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    D.drain(_tracker(repo), repo_root=repo, runner=_DigestRunner())
    ticket_dir = Path(_tracker(repo)) / tid
    queue_events = [
        f
        for f in ticket_dir.glob("*.json")
        if any(f.name.endswith(f"-{et}.json") for et in Q.QUEUE_EVENT_TYPES)
    ]
    assert len(queue_events) == 1
