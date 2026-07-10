"""Unit tests for the event-sourced enrichment queue (epic only-crave-art, story e1f4):
cert-triggered enqueue, soak, latest-wins, optimistic claim + lease, reducer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._store import event_append
from rebar.llm.overlap import queue as Q
from rebar.reducer._version import _NON_REPLAY_KNOWN_TYPES, is_unknown_newer_type

_MIN = Q._NS_PER_MIN


@pytest.fixture
def repo(tmp_path: Path) -> str:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    rebar.init_repo(repo_root=str(r))
    return str(r)


def _tracker(repo: str) -> str:
    from rebar._commands._seam import tracker_dir

    return str(tracker_dir(repo))


def test_event_types_registered() -> None:
    for et in ("ENQUEUE_ENRICH", "CLAIM_ENRICH", "DONE_ENRICH"):
        assert et in event_append.EVENT_TYPES  # write allow-list
        assert et in _NON_REPLAY_KNOWN_TYPES  # recognized non-replay (no fsck WARN)
        assert is_unknown_newer_type(et) is False


def test_enqueue_and_soak(repo: str) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    now = 1_000_000_000_000
    assert Q.enqueue(tid, soak_min=60, repo_root=repo, now_ns=now) is True
    tracker = _tracker(repo)
    # During the soak window → not pending.
    assert Q.reduce_ticket(tid, tracker, now_ns=now + 30 * _MIN)["pending"] is False
    # After the soak → pending.
    assert Q.reduce_ticket(tid, tracker, now_ns=now + 61 * _MIN)["pending"] is True
    assert Q.pending_enrichment(now + 61 * _MIN, tracker) == [tid]


def test_recert_bumps_soak_latest_wins(repo: str) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 2_000_000_000_000
    Q.enqueue(tid, soak_min=60, repo_root=repo, now_ns=now)
    # A re-cert 10 min later bumps not_before forward (latest-wins).
    Q.enqueue(tid, soak_min=60, repo_root=repo, now_ns=now + 10 * _MIN)
    st = Q.reduce_ticket(tid, tracker, now_ns=now + 61 * _MIN)
    # 61 min after the FIRST enqueue is still within the SECOND enqueue's soak (70 min mark).
    assert st["pending"] is False
    assert Q.reduce_ticket(tid, tracker, now_ns=now + 71 * _MIN)["pending"] is True


def test_claim_one_winner(repo: str) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    now = 3_000_000_000_000
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now)  # immediately eligible
    drain_now = now + 1
    assert Q.claim(tid, "drainer-A", lease_ttl_min=15, now_ns=drain_now, repo_root=repo) is True
    # A second drainer cannot claim while A's lease is live.
    assert (
        Q.claim(tid, "drainer-B", lease_ttl_min=15, now_ns=drain_now + 1, repo_root=repo) is False
    )
    assert Q.reduce_ticket(tid, _tracker(repo), now_ns=drain_now + 1)["claimed"] is True


def test_lease_expiry_self_heals(repo: str) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    now = 4_000_000_000_000
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now)
    Q.claim(tid, "drainer-A", lease_ttl_min=15, now_ns=now + 1, repo_root=repo)
    # After the lease expires, the ticket is claimable again (no separate reaper).
    after = now + 20 * _MIN
    assert Q.reduce_ticket(tid, _tracker(repo), now_ns=after)["pending"] is True
    assert Q.claim(tid, "drainer-B", lease_ttl_min=15, now_ns=after, repo_root=repo) is True


def test_done_ends_pending(repo: str) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    now = 5_000_000_000_000
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now)
    Q.mark_done(tid, repo_root=repo)
    st = Q.reduce_ticket(tid, _tracker(repo), now_ns=now + 61 * _MIN)
    assert st["done"] is True
    assert st["pending"] is False
    assert Q.pending_enrichment(now + 61 * _MIN, _tracker(repo)) == []


def test_recert_after_done_requeues(repo: str) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 6_000_000_000_000
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now)
    Q.mark_done(tid, repo_root=repo)
    # A later re-certification (new enqueue AFTER the done) makes it pending again.
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now + 100 * _MIN)
    assert Q.reduce_ticket(tid, tracker, now_ns=now + 101 * _MIN)["pending"] is True


def test_cert_enqueues(repo: str) -> None:
    # sign_plan_review (the certification path) enqueues the ticket for enrichment.
    from rebar.llm.plan_review import attest

    tid = rebar.create_ticket("task", "Cert enqueues", repo_root=repo)
    verdict = {"verdict": "PASS", "ticket_id": tid}
    attest.sign_plan_review(verdict, material="deadbeef", repo_root=repo)
    st = Q.reduce_ticket(tid, _tracker(repo))
    assert st["enqueued"] is True


# ── AC-named proving tests (epic only-crave-art / e1f4 acceptance criteria) ──────
def test_enqueue_and_recert(repo: str) -> None:
    """Certifying appends ENQUEUE_ENRICH with not_before = cert + SOAK; a re-cert
    supersedes and bumps not_before forward (latest-wins)."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 20_000_000_000_000
    Q.enqueue(tid, soak_min=60, repo_root=repo, now_ns=now)
    st = Q.reduce_ticket(tid, tracker, now_ns=now)
    assert st["enqueued"] and st["not_before_ns"] == now + 60 * _MIN
    # Re-cert 10 min later bumps not_before forward.
    Q.enqueue(tid, soak_min=60, repo_root=repo, now_ns=now + 10 * _MIN)
    assert Q.reduce_ticket(tid, tracker, now_ns=now)["not_before_ns"] == now + 70 * _MIN
    assert Q.reduce_ticket(tid, tracker, now_ns=now + 61 * _MIN)["pending"] is False


def test_soak_and_latest_wins(repo: str) -> None:
    """pending_enrichment returns only past-soak, unclaimed-or-expired tickets, at most one
    entry per ticket (latest-wins)."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 21_000_000_000_000
    Q.enqueue(tid, soak_min=60, repo_root=repo, now_ns=now)
    assert Q.pending_enrichment(now + 30 * _MIN, tracker) == []  # still soaking
    assert Q.pending_enrichment(now + 61 * _MIN, tracker) == [tid]  # past soak
    # A re-cert (latest-wins) still yields at most one pending entry for the ticket.
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now + 62 * _MIN)
    assert Q.pending_enrichment(now + 63 * _MIN, tracker) == [tid]


def test_lease_reclaim(repo: str) -> None:
    """A lease-expired claim is reclaimable on the next drain (self-healing); re-processing is
    idempotent (overwrite-by-content-hash in the digest sidecar, not this layer)."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    now = 22_000_000_000_000
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now)
    assert Q.claim(tid, "A", lease_ttl_min=15, now_ns=now + 1, repo_root=repo) is True
    after = now + 20 * _MIN  # lease (15 min) has expired
    assert Q.reduce_ticket(tid, _tracker(repo), now_ns=after)["pending"] is True
    assert Q.claim(tid, "B", lease_ttl_min=15, now_ns=after, repo_root=repo) is True
