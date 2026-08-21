"""Unit tests for the Tier-1 enrichment drain + `rebar enrich` CLI (only-crave-art, c1de)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import types
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
    # checkout config (cwd) — and this repo enables verify.suggest_duplicate_tickets.
    # Without this, every create_ticket/enqueue during test setup would spawn a drain child,
    # polluting the _spawn_detached_drain spies and racing the queue in test_batch_cap. Tests
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


def test_backlog_drains_despite_a_low_sorted_churn_set(repo: str) -> None:
    """Every queued entry is eventually served, even when `DRAIN_BATCH` entries that sort
    EARLIER keep re-entering the queue (bug f400-987f-45f6-419a).

    Contract: story c1de-d6a0-6cef-4135's AC — "`rebar enrich --drain` processes up to
    `DRAIN_BATCH` (default 5) soaked+unclaimed entries then exits; backlog drains over
    successive runs."

    The mechanism this pins is the interaction between the queue's candidate ORDER and the
    drain's success-counted batch cap: an order keyed on the ticket-id directory name returns
    a re-enqueued entry to the FRONT, so a churn set of `DRAIN_BATCH` low-sorted ids consumes
    the whole per-run budget forever and later-sorted entries are never claimed at all.

    The expected sets are built from ids this test created — never from `pending_enrichment`,
    which would make the oracle tautological. The assertion is on SERVICE (which entries reach
    a DONE tombstone), not on the order the queue happens to return, so any fair policy
    satisfies it and no wall-clock timing is involved.
    """
    tids = [rebar.create_ticket("task", f"T{i}", repo_root=repo) for i in range(14)]
    by_id = sorted(tids)
    churn, late = set(by_id[:5]), set(by_id[5:])  # 5 == DEFAULT_OVERLAP_DRAIN_BATCH

    stamp = 1000
    for tid in tids:
        Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=stamp)

    for _ in range(8):
        stamp += 60 * 1_000_000_000  # each re-enqueue carries a fresh, later soak deadline
        for tid in sorted(churn):
            Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=stamp)
        D.drain(_tracker(repo), repo_root=repo, runner=_DigestRunner())

    served = {t for t in tids if Q.reduce_ticket(t, _tracker(repo), now_ns=Q._now_ns())["done"]}
    assert late - served == set(), (
        f"{len(late - served)} of {len(late)} later-sorted entries were never served across "
        f"8 drain runs while the churn set was served repeatedly"
    )


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


def _mock_flags(monkeypatch, *, enabled=True, drain="always", agents=True):
    from rebar import config as rc
    from rebar.llm import config as lc

    real_load = rc.load_config

    def _patched(repo_root=None):
        c = real_load(repo_root)
        c.verify.suggest_duplicate_tickets = enabled  # VerifyConfig is a mutable dataclass
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
    """maybe_drain no-ops with NO spawn when nothing is soaked, and the cheap gate
    is exactly ONE pending_enrichment probe — no drain, no spawn, no second store
    read. Counting proxy, not a wall-clock budget: the previous
    `assert elapsed_ms < 500` was the CI flake class under runner contention."""
    calls = []
    monkeypatch.setattr(D, "drain", lambda *a, **k: calls.append("drain") or {})
    monkeypatch.setattr(D, "_spawn_detached_drain", lambda *a, **k: calls.append("spawn"))
    _mock_flags(monkeypatch, enabled=True, drain="async")
    monkeypatch.setattr(D.os, "name", "posix")
    probes = []
    real_pending = Q.pending_enrichment

    def counting_pending(*a, **k):
        probes.append(a)
        return real_pending(*a, **k)

    monkeypatch.setattr(Q, "pending_enrichment", counting_pending)
    # NOTHING enqueued → nothing soaked → cheap gate no-ops, no child spawned.
    D.maybe_drain(_tracker(repo), repo_root=repo)
    assert calls == []
    assert len(probes) == 1  # the gate is a single cheap soaked-work probe


def test_gate_proxy_detects_drain_path(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression simulation proving the proxy's teeth: force the probe to report
    soaked work and the same instrumentation goes loud (spawn fires) — so the
    quiet-gate assert above genuinely discriminates."""
    calls = []
    monkeypatch.setattr(D, "drain", lambda *a, **k: calls.append("drain") or {})
    monkeypatch.setattr(D, "_spawn_detached_drain", lambda *a, **k: calls.append("spawn"))
    _mock_flags(monkeypatch, enabled=True, drain="async")
    monkeypatch.setattr(D.os, "name", "posix")
    monkeypatch.setattr(Q, "pending_enrichment", lambda *a, **k: ["soaked-ticket"])
    D.maybe_drain(_tracker(repo), repo_root=repo)
    assert calls == ["spawn"]


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


def test_drain_preserves_committed_queue_history(repo: str) -> None:
    """A drain retains the committed queue history so stale clones can merge by UUID union."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    D.drain(_tracker(repo), repo_root=repo, runner=_DigestRunner())
    ticket_dir = Path(_tracker(repo)) / tid
    queue_events = [
        f
        for f in ticket_dir.glob("*.json")
        if any(f.name.endswith(f"-{et}.json") for et in Q.QUEUE_EVENT_TYPES)
    ]
    assert {f.name.rsplit("-", 1)[-1] for f in queue_events} == {
        "ENQUEUE_ENRICH.json",
        "CLAIM_ENRICH.json",
        "DONE_ENRICH.json",
    }


# --- drain-lock ownership + staleness (bug knavish-stimulated-bluebottle) -------------
#
# The drain lock used to be a bare O_EXCL file with no owner stamp and no staleness path:
# a drainer that died between acquire and release leaked it PERMANENTLY and every later
# drain skipped silently. These tests pin the stamp, the reclaim, and the refusals — all
# adjudicated by the SHARED lock_owner decision table, never a second heuristic.


def _drain_lock(repo: str) -> Path:
    return Path(D._drain_lock_path(_tracker(repo)))


def _plant_lock(repo: str, stamp: str, *, age_s: float = 0.0) -> Path:
    """Write a drain lock file carrying *stamp*, optionally back-dated by *age_s*."""
    path = _drain_lock(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stamp, encoding="utf-8")
    if age_s:
        when = time.time() - age_s
        os.utime(path, (when, when))
    return path


def _dead_pid() -> int:
    """A pid that is provably not running: a child we started, waited for, and reaped."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _stamp_with(**overrides: str) -> str:
    """Our own v2 stamp with individual fields substituted."""
    from rebar._store import lock_owner as owner

    fields = dict(token.split("=", 1) for token in owner._owner_stamp().split()[2:] if "=" in token)
    fields.update(overrides)
    return "rebar-lock v2 " + " ".join(f"{k}={v}" for k, v in fields.items())


def test_acquired_lock_carries_v2_stamp(repo: str) -> None:
    from rebar._store import lock_owner as owner

    fd = D._acquire_advisory_lock(_tracker(repo))
    assert fd is not None
    try:
        fields = owner._parse_v2_stamp(_drain_lock(repo).read_text(encoding="utf-8").strip())
        assert fields, "the acquired lock must carry a parseable v2 stamp"
        assert fields["host"] == owner._host_identity()
        assert fields["pid"] == str(os.getpid())
    finally:
        D._release_advisory_lock(_tracker(repo), fd)
    assert not _drain_lock(repo).exists()  # release still removes the file


def test_dead_holder_is_reclaimed(repo: str, caplog: pytest.LogCaptureFixture) -> None:
    """THE bug: a drainer that died without releasing must not wedge enrichment."""
    tid = rebar.create_ticket("task", "Wedged", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    _plant_lock(repo, _stamp_with(pid=str(_dead_pid())))

    with caplog.at_level("WARNING", logger="rebar.llm.enrich_drain"):
        result = D.drain(_tracker(repo), repo_root=repo, runner=_DigestRunner())

    assert "skipped" not in result
    assert result["processed"] == 1
    assert any("reclaiming stale drain lock" in r.message for r in caplog.records)
    assert any("held " in r.getMessage() for r in caplog.records)  # the reclaim names the age


def test_unstamped_orphan_is_reclaimed_past_the_ceiling(
    repo: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The already-leaked shape: a 0-byte lock from a pre-stamp rebar."""
    from rebar._store import lock_owner as owner

    _plant_lock(repo, "", age_s=owner._MKDIR_LOCK_STALE_CEILING_S + 60)
    with caplog.at_level("WARNING", logger="rebar.llm.enrich_drain"):
        fd = D._acquire_advisory_lock(_tracker(repo))
    assert fd is not None, "an aged-out unstamped orphan must be reclaimable"
    D._release_advisory_lock(_tracker(repo), fd)
    # With no holder to name, the age is the only signal an operator has — say it anyway.
    assert any(D._NO_STAMP in r.getMessage() for r in caplog.records)
    assert any(", held " in r.getMessage() for r in caplog.records)


def test_fresh_unstamped_lock_is_respected(repo: str) -> None:
    """The create/stamp window: a drainer between os.open and the stamp write."""
    _plant_lock(repo, "")
    assert D._acquire_advisory_lock(_tracker(repo)) is None


def test_live_holder_is_honoured(repo: str, caplog: pytest.LogCaptureFixture) -> None:
    path = _plant_lock(repo, _stamp_with())  # our own live pid
    before = path.read_bytes()

    with caplog.at_level("WARNING", logger="rebar.llm.enrich_drain"):
        result = D.drain(_tracker(repo), repo_root=repo, runner=_DigestRunner())

    assert result == {"skipped": "lock-held", "processed": 0}
    assert path.read_bytes() == before  # never broken, never rewritten
    assert any("advisory lock held by" in r.message for r in caplog.records)
    assert any(f"pid={os.getpid()}" in r.getMessage() for r in caplog.records)


def test_foreign_host_lock_is_respected_then_aged_out(repo: str) -> None:
    from rebar._store import lock_owner as owner

    _plant_lock(repo, _stamp_with(host="boot-somewhere-else"))
    assert D._acquire_advisory_lock(_tracker(repo)) is None  # no proof, no reclaim

    _plant_lock(
        repo,
        _stamp_with(host="boot-somewhere-else"),
        age_s=owner._MKDIR_LOCK_STALE_CEILING_S + 60,
    )
    fd = D._acquire_advisory_lock(_tracker(repo))
    assert fd is not None  # the inherited ceiling still bounds the wedge
    D._release_advisory_lock(_tracker(repo), fd)


def test_reclaim_retries_exactly_once(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bounded: a lock that keeps reappearing must not spin."""
    from rebar._store import lock_owner as owner

    _plant_lock(repo, "", age_s=owner._MKDIR_LOCK_STALE_CEILING_S + 60)
    real_open = os.open
    attempts = []

    def _always_taken(path, *args, **kwargs):
        if str(path).endswith(D._DRAIN_LOCK_NAME):
            attempts.append(path)
            raise FileExistsError(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", _always_taken)
    assert D._acquire_advisory_lock(_tracker(repo)) is None
    assert len(attempts) == 2  # the original + exactly one post-reclaim retry


def test_acquire_swallows_other_os_errors(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A drain concern must never fail its caller: an open failure that is not a
    collision (a read-only .rebar, a full disk) still degrades to "no lock"."""
    real_open = os.open

    def _boom(path, *args, **kwargs):
        if str(path).endswith(D._DRAIN_LOCK_NAME):
            raise PermissionError(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", _boom)
    assert D._acquire_advisory_lock(_tracker(repo)) is None


# ---------------------------------------------------------------------------
# holder vocabulary: the drain log and `rebar doctor` describe ONE artifact
# ---------------------------------------------------------------------------


def test_torn_stamp_is_described_as_incomplete_not_unstamped(repo: str) -> None:
    """A v2 stamp read mid-write (the prefix landed, the fields did not) is a DIFFERENT
    condition from a lock that was never stamped, and the log line must say which: the
    first is a live drainer caught in its create/stamp window, the second an orphan from
    a pre-stamp rebar. Collapsing both into "unstamped" hides that."""
    path = _plant_lock(repo, "rebar-lock v2 host=boot-x ns=1")

    description = D._describe_drain_lock_holder(str(path))

    assert D._INCOMPLETE_STAMP in description
    assert D._NO_STAMP not in description
    assert ", held " in description  # with no holder to name, the age is the only signal


@pytest.mark.parametrize(
    ("stamp", "phrase"),
    [
        ("", "_NO_STAMP"),
        ("rebar-lock v9 something=else", "_UNRECOGNISED_STAMP"),
        ("rebar-lock v2 host=boot-x ns=1", "_INCOMPLETE_STAMP"),
    ],
)
def test_drain_and_doctor_describe_a_holderless_lock_identically(
    repo: str, stamp: str, phrase: str
) -> None:
    """Two surfaces, one artifact: the drain's WARNING and doctor's lock row must not
    drift into private dialects for the same lock file. Pins the shared phrasing so a
    change to either surface fails here rather than in an operator's head."""
    from rebar._commands import doctor_locks

    expected = getattr(D, phrase)
    path = _plant_lock(repo, stamp)

    assert expected in D._describe_drain_lock_holder(str(path))
    row = doctor_locks._existence_report("enrich-drain", str(path), note="free")
    assert row["holder_description"] == f"unknown ({expected})"


def test_stamp_write_failure_keeps_a_ceiling_bounded_lock(
    repo: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed stamp write (full disk, read-only mount) must not fail the acquire — a
    drain concern never fails its caller. The contract of that branch is that the fd is
    still returned, the loss of attribution is announced, and the resulting unstamped
    lock stays bounded by the shared wall-clock ceiling rather than wedging."""
    from rebar._store import lock_owner as owner

    real_open, real_write = os.open, os.write
    drain_fds: set[int] = set()

    def _tracking_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if str(path).endswith(D._DRAIN_LOCK_NAME):
            drain_fds.add(fd)
        return fd

    def _refusing_write(fd, data):
        if fd in drain_fds:
            raise OSError("no space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "open", _tracking_open)
    monkeypatch.setattr(os, "write", _refusing_write)
    with caplog.at_level("WARNING", logger="rebar.llm.enrich_drain"):
        fd = D._acquire_advisory_lock(_tracker(repo))
    monkeypatch.setattr(os, "open", real_open)
    monkeypatch.setattr(os, "write", real_write)

    assert fd is not None, "a stamp write failure must not turn into a failed acquire"
    assert any("lock is unattributable" in r.getMessage() for r in caplog.records)
    path = _drain_lock(repo)
    assert path.read_text(encoding="utf-8") == ""  # nothing was stamped
    os.close(fd)

    # Still ceiling-bounded: honoured while young, reclaimable once aged out.
    assert D._acquire_advisory_lock(_tracker(repo)) is None
    when = time.time() - (owner._MKDIR_LOCK_STALE_CEILING_S + 60)
    os.utime(path, (when, when))
    reclaimed = D._acquire_advisory_lock(_tracker(repo))
    assert reclaimed is not None, "an unattributable lock must never wedge past the ceiling"
    D._release_advisory_lock(_tracker(repo), reclaimed)


# --- Permanent per-item failures must not cycle (bug spongy-illjudged-terrier) --------------


class _PermanentInputRejectionRunner(Runner):
    """Raises the provider's deterministic over-context rejection, verbatim.

    The raised error carries the SAME classified disposition ``run_failure`` attaches in
    production (``ResolutionClass.CHANGE_INPUT`` / ``retryable=False``), so the drain sees
    exactly the object it sees on the real path.
    """

    name = "permanent"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, req: RunRequest) -> dict:
        from rebar.llm.errors import LLMInputRejectedError
        from rebar.llm.failure import LLMOutcome, ResolutionClass

        self.calls += 1
        err = LLMInputRejectedError(
            "the LLM provider rejected the request input: status_code: 400, "
            "model_name: us.anthropic.claude-haiku-4-5-20251001-v1:0, "
            "'Message': 'prompt is too long: 206826 tokens > 200000 maximum'"
        )
        err.outcome = LLMOutcome(  # type: ignore[attr-defined]
            ResolutionClass.CHANGE_INPUT,
            {"status_code": 400},
            retryable=False,
        )
        raise err

    def preflight(self) -> None:
        pass


def _cycle_drains(repo: str, tid: str, runner: Runner, cycles: int) -> int:
    """Drive `cycles` drains, advancing a virtual clock past each claim lease between them.

    Returns the final virtual instant, so assertions read the queue at the same clock the
    drains ran against. Time is a MONKEYPATCHED counter, never a sleep: the property under
    test is algorithmic (does the entry converge to a terminal state?), not temporal.
    """
    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    try:
        clock = {"n": Q._now_ns()}
        mp.setattr(Q, "_now_ns", lambda: clock["n"])
        for _ in range(cycles):
            D.drain(_tracker(repo), repo_root=repo, runner=runner)
            clock["n"] += 40 * Q._NS_PER_MIN  # past any plausible lease
        return clock["n"]
    finally:
        mp.undo()


def test_permanent_input_rejection_reaches_a_terminal_state(repo: str) -> None:
    """A deterministically-failing entry converges instead of cycling forever.

    The algorithmic property, not a wall-clock bound: across repeated drains that each see a
    fresh (expired-lease) entry, a permanently-rejected item is attempted a BOUNDED number of
    times and ends in a terminal state, so it leaves the pending set for good.
    """
    tid = rebar.create_ticket("task", "Too large to enrich", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)
    runner = _PermanentInputRejectionRunner()

    end = _cycle_drains(repo, tid, runner, cycles=5)

    final = Q.reduce_ticket(tid, _tracker(repo), now_ns=end)
    assert final["done"] is True, "a permanently-rejected entry must reach a terminal state"
    assert tid not in Q.pending_enrichment(end, _tracker(repo))
    assert runner.calls == 1, (
        f"a permanent rejection must be attempted once, not once per lease "
        f"(saw {runner.calls} provider calls across 5 drain cycles)"
    )


def test_transient_failure_keeps_the_retry_posture(repo: str) -> None:
    """The terminal branch is narrow: an UNCLASSIFIED failure still retries after the lease.

    Guards the opposite error — classifying everything as permanent would tombstone the whole
    queue on a transient outage or a bad credential.
    """
    tid = rebar.create_ticket("task", "Transiently broken", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=1000)

    end = _cycle_drains(repo, tid, _BoomRunner(), cycles=3)

    assert Q.reduce_ticket(tid, _tracker(repo), now_ns=end)["done"] is False
    assert tid in Q.pending_enrichment(end, _tracker(repo))


# ── one store, one drain lock (bug nuclear-calm-heron da68-fc7c-068c-4c53) ───────────────────
#
# `make worktree` provisions a worktree whose `.tickets-tracker` is a SYMLINK to the canonical
# store while its `.rebar` is a real, per-worktree directory. The drain derived its lock and
# its log from `os.path.dirname(tracker)` without resolving that symlink, so two agents in two
# worktrees held two DIFFERENT lock files while draining the SAME queue — the lock's whole
# purpose defeated exactly when it matters — and the drain log was written into, and deleted
# with, the ephemeral worktree. The store's own contract is explicit:
# `_store.lock.canonical_tracker` exists "so symlinked and real-path callers contend on the
# SAME lock file". These tests pin the drain to that contract, the same way
# tests/unit/test_compact_trigger.py pins the compaction trigger (the landed half of this
# class fix, bug intangible-ladyish-vicuna 93a9-66cf-e681-4f49).


def _canonical_store(tmp_path: Path) -> str:
    """A canonical store: ``<root>/.tickets-tracker``. Returns the tracker path."""
    tracker = Path(os.path.realpath(tmp_path)) / "canonical-repo" / ".tickets-tracker"
    tracker.mkdir(parents=True)
    return str(tracker)


def _worktree_tracker(tmp_path: Path, tracker: str, name: str) -> str:
    """A worktree view of *tracker*: a real ``.rebar`` beside a ``.tickets-tracker`` SYMLINK
    into the canonical store, exactly as ``make worktree`` provisions one."""
    wt = Path(os.path.realpath(tmp_path)) / name
    (wt / ".rebar").mkdir(parents=True)
    (wt / ".tickets-tracker").symlink_to(tracker)
    return str(wt / ".tickets-tracker")


def test_two_worktrees_of_one_store_derive_one_set_of_drain_paths(tmp_path: Path) -> None:
    """Independence from the caller proved by INVARIANCE: two worktree views of one store must
    derive the SAME lock and log paths, and they must be the canonical store's rather than
    either worktree's. A sidecar keyed on the caller is as short-lived as the caller."""
    canonical = _canonical_store(tmp_path)
    a = _worktree_tracker(tmp_path, canonical, "worktree-a")
    b = _worktree_tracker(tmp_path, canonical, "worktree-b")

    for derive in (D._drain_lock_path, D._drain_log_path):
        assert derive(a) == derive(b), f"{derive.__name__} is keyed on the worktree"
        assert derive(a) == derive(canonical), f"{derive.__name__} is not on the canonical store"


def test_two_worktrees_of_one_store_contend_on_the_same_drain_lock(tmp_path: Path) -> None:
    """The lock's stated intent — two drain PROCESSES must not overlap on one store — must
    hold ACROSS worktrees. Path equality above is necessary but not sufficient: this drives
    the real acquire, so a fix that renamed a path without restoring exclusion still fails."""
    canonical = _canonical_store(tmp_path)
    a = _worktree_tracker(tmp_path, canonical, "worktree-a")
    b = _worktree_tracker(tmp_path, canonical, "worktree-b")

    held = D._acquire_advisory_lock(a)
    assert held is not None, "precondition: the first drainer must acquire"
    try:
        assert D._acquire_advisory_lock(b) is None, (
            "a second drainer on the SAME store acquired the drain lock concurrently"
        )
    finally:
        D._release_advisory_lock(a, held)


def test_the_drain_lock_is_never_written_inside_an_ephemeral_worktree(tmp_path: Path) -> None:
    """The durability half: the lock must land on the store, so it survives the worktree.

    Negative control for the two tests above — exclusion could in principle be restored by
    keying every worktree on the FIRST one, which would still be deleted with that worktree."""
    canonical = _canonical_store(tmp_path)
    wt = _worktree_tracker(tmp_path, canonical, "worktree-a")
    canonical_rebar = os.path.join(os.path.dirname(canonical), ".rebar")

    fd = D._acquire_advisory_lock(wt)
    assert fd is not None
    try:
        assert os.listdir(os.path.join(os.path.dirname(wt), ".rebar")) == [], (
            "the drain wrote its lock into the worktree that spawned it"
        )
        assert os.listdir(canonical_rebar) == ["enrich-drain.lock"], (
            "the drain lock did not land in the canonical store's .rebar"
        )
    finally:
        D._release_advisory_lock(wt, fd)


def test_detached_drain_child_is_handed_the_canonical_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child outlives the worktree that spawned it, and its ``argv[1]`` tracker is every
    path it will ever touch — a worktree SYMLINK there dies with the worktree
    (`Error: cannot list '<retired worktree>/.tickets-tracker'`). Its ``cwd`` was already
    resolved for exactly this reason (bug 3198-438c-72a5-470f); this pins the argv too."""
    canonical = _canonical_store(tmp_path)
    wt = _worktree_tracker(tmp_path, canonical, "worktree-a")
    spawned: list[tuple[list[str], dict]] = []

    def _fake_popen(argv: list[str], **kwargs: object) -> object:
        spawned.append((argv, kwargs))
        return object()

    # Patch the spawn owner's OWN `subprocess` reference (`_proc` holds the one Popen since
    # task 2dc4-9bcd-75b9-4544), never the real module: a global patch outlives this test's
    # body and breaks `subprocess.run` in fixture teardown.
    from rebar import _proc

    monkeypatch.setattr(
        _proc,
        "subprocess",
        types.SimpleNamespace(Popen=_fake_popen, DEVNULL=subprocess.DEVNULL),
    )

    D._spawn_detached_drain(wt)

    assert len(spawned) == 1
    argv, kwargs = spawned[0]
    assert argv[3] == canonical, "the detached child was handed an ephemeral worktree tracker"
    assert kwargs["cwd"] == os.path.dirname(canonical)
