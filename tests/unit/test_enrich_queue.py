"""Unit tests for the event-sourced enrichment queue (epic only-crave-art, story e1f4):
cert-triggered enqueue, soak, latest-wins, optimistic claim + lease, reducer.
"""

from __future__ import annotations

import builtins
import json
import os
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


def test_cert_enqueues(repo: str, monkeypatch) -> None:
    # sign_plan_review (the certification path) enqueues the ticket for enrichment.
    from rebar.llm.plan_review import attest

    # Simulate an active attested session: the sign seam's no-null-pin invariant
    # (bug 5128-0856) refuses to sign with no snapshot SHA at all.
    monkeypatch.setattr("rebar.llm.config.current_code_sha", lambda: "c" * 40)
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


# ── bounded reduce read (ticket emersed-utopic-whiterhino) ───────────────────────
# reduce_ticket used to json.load EVERY queue event file in a ticket dir, three times over
# (once per event type), so the enrich-drain gate's cost grew without bound as enrichment
# re-ran. The reduce now walks newest→oldest and stops early. These tests pin that bound.

_BASE_NS = 1_700_000_000_000_000_000  # a realistic fixed-width ns stamp (Nov 2023)


def _seed_event(ticket_dir: Path, ts: int, etype: str, data: dict, uuid: str) -> str:
    """Write one raw event file straight into ``ticket_dir`` (no git), using the store's
    ``{timestamp}-{uuid}-{TYPE}.json`` name. Returns the filename."""
    fname = f"{ts}-{uuid}-{etype}.json"
    (ticket_dir / fname).write_text(
        json.dumps(
            {
                "timestamp": ts,
                "uuid": uuid,
                "event_type": etype,
                "env_id": "e",
                "author": "a",
                "data": data,
            }
        ),
        encoding="utf-8",
    )
    return fname


def _count_queue_opens(monkeypatch) -> list[str]:
    """Record the basename of every queue-event file opened while the patch is active."""
    opened: list[str] = []
    real_open = builtins.open

    def counting_open(file, *a, **kw):
        name = os.path.basename(str(file))
        if name.endswith(tuple(f"-{et}.json" for et in Q.QUEUE_EVENT_TYPES)):
            opened.append(name)
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", counting_open)
    return opened


def test_reduce_opens_at_most_one_file_per_event_type(repo: str, monkeypatch) -> None:
    """A ticket dir carrying a long queue history costs ONE open per event type, not one
    per file — the asymptote the enrich-drain gate was paying."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    ticket_dir = Path(tracker) / tid
    for i in range(30):
        _seed_event(ticket_dir, _BASE_NS + i, Q.ENQUEUE, {"not_before_ns": _BASE_NS}, f"e{i:03d}")
        _seed_event(
            ticket_dir,
            _BASE_NS + 100 + i,
            Q.CLAIM,
            {"drainer_id": f"d{i}", "lease_expires_ns": 0},
            f"c{i:03d}",
        )
        _seed_event(ticket_dir, _BASE_NS + 200 + i, Q.DONE, {}, f"d{i:03d}")

    opened = _count_queue_opens(monkeypatch)
    state = Q.reduce_ticket(tid, tracker, now_ns=_BASE_NS + 10_000)

    assert len(opened) <= 3, f"reduce opened {len(opened)} queue files: {opened}"
    for et in Q.QUEUE_EVENT_TYPES:
        per_type = [n for n in opened if n.endswith(f"-{et}.json")]
        assert len(per_type) <= 1, f"{et}: {per_type}"
    # …and it still reduced correctly: the newest DONE (ts +229) postdates the newest
    # ENQUEUE (ts +29), so nothing is pending.
    assert state["enqueued"] is True
    assert state["done"] is True
    assert state["pending"] is False


def test_claim_arbitration_skips_pre_enqueue_claims(repo: str, monkeypatch) -> None:
    """claim() arbitration reads only the CLAIM suffix newer than the latest ENQUEUE.

    Claims older than the enqueue can never be contenders, so the scan must stop at the
    enqueue boundary instead of reading back through the whole history. The single newest
    pre-enqueue claim may still be opened — that is reduce_ticket's own one-open-per-type
    probe, which runs before arbitration — but every OLDER one must be untouched.
    """
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    ticket_dir = Path(tracker) / tid
    stale = [
        _seed_event(
            ticket_dir,
            _BASE_NS + i,
            Q.CLAIM,
            {"drainer_id": f"old{i}", "lease_expires_ns": _BASE_NS + 10**12},
            f"c{i:03d}",
        )
        for i in range(20)
    ]
    enq_ts = _BASE_NS + 1_000_000
    _seed_event(ticket_dir, enq_ts, Q.ENQUEUE, {"not_before_ns": _BASE_NS}, "enq0")

    opened = _count_queue_opens(monkeypatch)
    won = Q.claim(tid, "drainer-new", lease_ttl_min=15, now_ns=enq_ts + 1, repo_root=repo)

    assert won is True, "the only post-enqueue claimant must win"
    touched_stale = [n for n in opened if n in stale]
    assert touched_stale in ([], [stale[-1]]), (
        f"arbitration read back past the enqueue boundary: {touched_stale}"
    )
    for older in stale[:-1]:
        assert older not in opened, f"{older} predates the enqueue and must never be opened"


def test_corrupt_newest_event_falls_back_to_older(repo: str) -> None:
    """An unparseable newest file does not blind the reduce — it walks to the next-older."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    ticket_dir = Path(tracker) / tid
    good_not_before = _BASE_NS + 500
    _seed_event(ticket_dir, _BASE_NS + 10, Q.ENQUEUE, {"not_before_ns": good_not_before}, "eok")
    (ticket_dir / f"{_BASE_NS + 20}-ebad-{Q.ENQUEUE}.json").write_text("{not json", "utf-8")

    state = Q.reduce_ticket(tid, tracker, now_ns=good_not_before + 1)
    assert state["enqueued"] is True
    assert state["not_before_ns"] == good_not_before
    assert state["pending"] is True


def test_all_events_corrupt_reads_as_absent(repo: str) -> None:
    """Terminal case of the newest-first walk: every matching file unparseable → the type
    reduces as absent, exactly as if no file existed."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    ticket_dir = Path(tracker) / tid
    for i in range(3):
        (ticket_dir / f"{_BASE_NS + i}-bad{i}-{Q.ENQUEUE}.json").write_text("{", "utf-8")

    assert Q._latest(str(ticket_dir), Q.ENQUEUE) is None
    state = Q.reduce_ticket(tid, tracker, now_ns=_BASE_NS + 10)
    assert state["enqueued"] is False
    assert state["pending"] is False


# ── O(1) drain-gate fast path (bug moist-short-lionfish 958e) ────────────────────
# The write-path gate (`enrich_drain.maybe_drain`) probes the queue on EVERY store write
# against a declared 20 ms budget. Reducing every ticket dir to answer it made the common
# "nothing soaked" answer cost a full store walk — ~486 ms / ~24,000 metadata syscalls at
# 4,831 tickets, and worse than linearly so when several agents' gates overlap. These pin
# the ALGORITHMIC property (work does not scale with the store) rather than a wall clock,
# which would be flaky under exactly the contention that motivated the fix.


def _count_reductions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Instrument ``reduce_ticket`` and return the list it appends a ticket id to."""
    seen: list[str] = []
    real = Q.reduce_ticket

    def counting(ticket_id: str, tracker: str, **kwargs: object) -> dict:
        seen.append(ticket_id)
        return real(ticket_id, tracker, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Q, "reduce_ticket", counting)
    return seen


@pytest.mark.parametrize("n_tickets", [4, 40])
def test_gate_cost_does_not_scale_with_store_size(
    repo: str, monkeypatch: pytest.MonkeyPatch, n_tickets: int
) -> None:
    """A repeat gate probe on a store with nothing soaked reduces NO tickets, at any store
    size — so the gate's cost is independent of the ticket count.

    Counted, not timed: the acceptance criterion is algorithmic, and a wall-clock bound is
    precisely the flake class this bug's own measurements exhibited.
    """
    tracker = _tracker(repo)
    now = 30_000_000_000_000
    for i in range(n_tickets):
        rebar.create_ticket("task", f"T{i}", repo_root=repo)
    # First probe is allowed to be cold (it establishes the fast path).
    assert Q.pending_enrichment(now, tracker) == []
    seen = _count_reductions(monkeypatch)
    assert Q.pending_enrichment(now + 1, tracker) == []
    assert seen == [], f"repeat gate probe reduced {len(seen)} tickets at n={n_tickets}"


def test_gate_fast_path_expires_at_the_soak_deadline(repo: str) -> None:
    """The fast path must never HIDE a ticket: a probe taken during the soak may skip the
    walk only until the soak deadline it recorded."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 31_000_000_000_000
    Q.enqueue(tid, soak_min=60, repo_root=repo, now_ns=now)
    assert Q.pending_enrichment(now, tracker) == []  # soaking; records the deadline
    assert Q.pending_enrichment(now + 59 * _MIN, tracker) == []  # still soaking
    assert Q.pending_enrichment(now + 61 * _MIN, tracker) == [tid]  # deadline passed


def test_lost_gate_marker_degrades_to_a_full_scan(
    repo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Self-healing: a marker that is deleted, empty or corrupt degrades to the full scan —
    never to a missed drain."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 32_000_000_000_000
    Q.enqueue(tid, soak_min=60, repo_root=repo, now_ns=now)
    assert Q.pending_enrichment(now, tracker) == []  # records a deadline at now+60m
    marker = Q._gate_marker_path(tracker)
    assert os.path.exists(marker)
    # Simulate a crash between the queue append and the marker invalidation: the ticket
    # becomes eligible immediately, but the recorded marker still says "not before now+60m".
    monkeypatch.setattr(Q, "_clear_gate_marker", lambda _tracker: None)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now + 1)
    monkeypatch.undo()
    assert os.path.exists(marker)  # the stale marker survived the (simulated) crash
    for corruption in (b"", b"{not json", b'{"next_eligible_ns": "nonsense"}'):
        with open(marker, "wb") as fh:
            fh.write(corruption)
        assert Q.pending_enrichment(now + 2, tracker) == [tid]
        with open(marker, "wb") as fh:  # rewrite the stale-but-valid marker for the next case
            fh.write(json.dumps({"next_eligible_ns": now + 60 * _MIN, "written_ns": now}).encode())
    os.unlink(marker)
    assert Q.pending_enrichment(now + 2, tracker) == [tid]


def test_stale_gate_marker_is_ttl_bounded(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A marker left stale by a crash suppresses the walk for at most the marker TTL, so a
    drain is delayed but never permanently missed."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 33_000_000_000_000
    Q.enqueue(tid, soak_min=60, repo_root=repo, now_ns=now)
    assert Q.pending_enrichment(now, tracker) == []
    monkeypatch.setattr(Q, "_clear_gate_marker", lambda _tracker: None)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now + 1)
    monkeypatch.undo()
    ttl = Q._GATE_MARKER_TTL_NS
    assert Q.pending_enrichment(now + 2, tracker) == []  # suppressed by the stale marker
    assert Q.pending_enrichment(now + ttl + 1, tracker) == [tid]  # TTL expiry self-heals


def test_queue_mutations_invalidate_the_gate_marker(repo: str) -> None:
    """Every queue append clears the marker, so a state change is never hidden behind it."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 34_000_000_000_000
    marker = Q._gate_marker_path(tracker)
    for mutate in (
        lambda: Q.enqueue(tid, soak_min=60, repo_root=repo, now_ns=now),
        lambda: Q.claim(tid, "A", lease_ttl_min=15, now_ns=now + 61 * _MIN, repo_root=repo),
        lambda: Q.mark_done(tid, repo_root=repo),
    ):
        Q.pending_enrichment(now, tracker)
        assert os.path.exists(marker) or Q.pending_enrichment(now, tracker) != []
        mutate()
        assert not os.path.exists(marker)
