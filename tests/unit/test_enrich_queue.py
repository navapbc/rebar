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
    # sign_plan_review (the certification path) enqueues the ticket for enrichment
    # when the overlap feature is on and the drain is not "off" (the producer mirrors
    # the drain's own gate — rebar-ticket 4eae-c207-7d7b-41f3).
    from rebar.llm.plan_review import attest

    # Simulate an active attested session: the sign seam's no-null-pin invariant
    # (bug 5128-0856) refuses to sign with no snapshot SHA at all.
    monkeypatch.setattr("rebar.llm.gate_context.current_code_sha", lambda: "c" * 40)
    monkeypatch.delenv("REBAR_LLM_OVERLAP_DRAIN", raising=False)
    tid = rebar.create_ticket("task", "Cert enqueues", repo_root=repo)
    # Enable the overlap feature; leave overlap_drain at its default ("async" — enabled).
    Path(repo, "rebar.toml").write_text("[verify]\nsuggest_duplicate_tickets = true\n")
    verdict = {"verdict": "PASS", "ticket_id": tid}
    attest.sign_plan_review(verdict, material="deadbeef", repo_root=repo)
    st = Q.reduce_ticket(tid, _tracker(repo))
    assert st["enqueued"] is True


def test_cert_with_drain_off_appends_no_queue_event(repo: str, monkeypatch) -> None:
    """With `[llm] overlap_drain = "off"` a plan-review certification appends NO
    ENQUEUE_ENRICH event: the producer is gated on the same effective config the drain
    reads, so nothing is ever written into a queue nothing consumes (regression:
    rebar-ticket 4eae-c207-7d7b-41f3)."""
    from rebar.llm.plan_review import attest

    monkeypatch.setattr("rebar.llm.gate_context.current_code_sha", lambda: "c" * 40)
    monkeypatch.delenv("REBAR_LLM_OVERLAP_DRAIN", raising=False)
    tid = rebar.create_ticket("task", "Cert with drain off", repo_root=repo)
    Path(repo, "rebar.toml").write_text(
        '[verify]\nsuggest_duplicate_tickets = true\n\n[llm]\noverlap_drain = "off"\n'
    )
    attest.sign_plan_review(
        {"verdict": "PASS", "ticket_id": tid}, material="deadbeef", repo_root=repo
    )
    tracker = _tracker(repo)
    assert Q.reduce_ticket(tid, tracker)["enqueued"] is False
    # No queue event file at all — not merely a reduced "not enqueued".
    assert list(Path(tracker).rglob(f"*-{Q.ENQUEUE}.json")) == []


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


def test_marker_defers_a_claimed_entry_to_its_lease_expiry(
    repo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimed entry's next-eligible instant is its LEASE EXPIRY, not its soak deadline.

    A claim holds a ticket back past its `not_before_ns` until the lease lapses, so recording
    the soak deadline instead would make the marker expire the moment the claim was taken —
    correct answers, but the fast path would never engage for a claimed store, which is
    exactly the wedged-queue case this bug is worst in. Pinned by watching the fast path
    engage mid-lease, then correctly stand down once the lease has lapsed.
    """
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 35_000_000_000_000
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now)  # eligible immediately...
    assert Q.claim(tid, "A", lease_ttl_min=15, now_ns=now, repo_root=repo) is True  # ...but held
    assert Q.pending_enrichment(now + 1, tracker) == []  # records the LEASE expiry, not now
    seen = _count_reductions(monkeypatch)
    assert Q.pending_enrichment(now + 10 * _MIN, tracker) == []  # mid-lease: fast path holds
    assert seen == [], "marker expired at the soak deadline instead of the lease expiry"
    monkeypatch.undo()
    assert Q.pending_enrichment(now + 16 * _MIN, tracker) == [tid]  # lease lapsed → reclaimable


def test_marker_written_in_the_future_is_not_trusted(repo: str) -> None:
    """A marker whose ``written_ns`` is AHEAD of now is untrusted.

    The TTL is a subtraction, so a clock that steps backwards (or a marker carried in from a
    host whose clock ran fast) makes ``now - written`` negative — the TTL can then never
    elapse, and the fast path would be trusted until real time caught up. That would turn the
    bounded-delay guarantee into an unbounded one, so the window is treated as untrusted
    rather than as infinitely fresh.
    """
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 36_000_000_000_000
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now)  # eligible right away
    marker = Q._gate_marker_path(tracker)
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w", encoding="utf-8") as fh:
        json.dump({"next_eligible_ns": None, "written_ns": now + 365 * 24 * 60 * _MIN}, fh)
    assert Q.pending_enrichment(now + 1, tracker) == [tid]


def test_gate_marker_lives_outside_the_tracker(repo: str) -> None:
    """The marker is machine-local cache state, so it must live in the repo-local ``.rebar``
    dir and NEVER inside the tracker — the tracker auto-commits and auto-pushes, so a marker
    written there would travel to every clone and be reported as store drift.

    The lockstep half of this test is SUBSUMED by story ``6f18-05de-beaf-42be``: the marker
    and the drain lock no longer have two implementations of the tracker→``.rebar`` sibling
    convention to keep in step — both derive from the one owner,
    :class:`rebar._store.paths.StorePaths`, so the assertion now names that owner directly.
    """
    from rebar._store.paths import StorePaths

    tracker = _tracker(repo)
    marker = Q._gate_marker_path(tracker)
    assert os.path.dirname(marker) == StorePaths(tracker).rebar_dir
    assert not os.path.abspath(marker).startswith(os.path.abspath(tracker) + os.sep)
    Q.pending_enrichment(37_000_000_000_000, tracker)  # writes one
    assert os.path.exists(marker)
    assert Q._GATE_MARKER_NAME not in os.listdir(tracker)


@pytest.mark.parametrize("backlog", [4, 40])
def test_existence_probe_cost_does_not_scale_with_the_backlog(
    repo: str, monkeypatch: pytest.MonkeyPatch, backlog: int
) -> None:
    """Bug 6148-5d81-8e80-41e8 (draughty-callous-tanager): the write-path gate needs a
    yes/no, and answering it with the O(backlog) list probe priced EVERY tracker write at
    ~2-8 s against a 20 ms budget once a ~1,050-entry backlog stood — the task-4144
    marker-scoped walk re-reduced every live entry per probe. The existence probe must stop
    at the FIRST pending hit: ONE reduction, whatever the backlog size, on the marker-scoped
    path AND on the cold full-walk path.

    Counted, not timed, for the same flake-class reason as the store-size test above."""
    tracker = _tracker(repo)
    now = 45_000_000_000_000
    tids = [rebar.create_ticket("task", f"B{i}", repo_root=repo) for i in range(backlog)]
    for tid in tids:
        Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now)
    # One full walk primes the marker's live set — the steady state a backlogged store is in.
    assert len(Q.pending_enrichment(now + 1, tracker)) == backlog
    seen = _count_reductions(monkeypatch)
    assert Q.has_pending_enrichment(now + 2, tracker) is True  # marker-scoped path
    assert len(seen) == 1, f"scoped existence probe reduced {len(seen)} of {backlog} entries"
    monkeypatch.undo()
    os.unlink(Q._gate_marker_path(tracker))
    seen = _count_reductions(monkeypatch)
    assert Q.has_pending_enrichment(now + 2, tracker) is True  # cold full-walk path
    assert len(seen) == 1, f"cold existence probe reduced {len(seen)} of {backlog} entries"


def test_existence_probe_matches_the_list_probe_verdict(repo: str) -> None:
    """DECISION-SEMANTICS pin for bug 6148-5d81-8e80-41e8: the existence probe answers
    exactly ``bool(pending_enrichment(...))`` in every queue state the reducer
    distinguishes — empty, soaking, pending, claimed-with-live-lease, lease-expired, done —
    so the gate's yes/no is unchanged; only the enumeration is skipped."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 46_000_000_000_000

    def agree(t: int) -> bool:
        verdict = Q.has_pending_enrichment(t, tracker)
        assert verdict is bool(Q.pending_enrichment(t, tracker))
        return verdict

    assert agree(now) is False  # empty queue
    Q.enqueue(tid, soak_min=60, repo_root=repo, now_ns=now)
    assert agree(now + 30 * _MIN) is False  # soaking
    assert agree(now + 61 * _MIN) is True  # past soak → pending
    assert Q.claim(tid, "A", lease_ttl_min=15, now_ns=now + 61 * _MIN, repo_root=repo) is True
    assert agree(now + 62 * _MIN) is False  # claimed, live lease
    assert agree(now + 77 * _MIN) is True  # lease expired → reclaimable
    Q.mark_done(tid, repo_root=repo)
    assert agree(now + 78 * _MIN) is False  # done


def test_existence_probe_quiet_repeat_reduces_nothing(
    repo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quiet FULL walk in the existence probe still writes the gate marker, so the repeat
    probe on a quiet store stays O(1) — reducing NO tickets — exactly like the list probe."""
    tracker = _tracker(repo)
    now = 47_000_000_000_000
    for i in range(4):
        rebar.create_ticket("task", f"T{i}", repo_root=repo)
    assert Q.has_pending_enrichment(now, tracker) is False  # cold walk; writes the marker
    seen = _count_reductions(monkeypatch)
    assert Q.has_pending_enrichment(now + 1, tracker) is False
    assert seen == [], f"repeat quiet existence probe reduced {len(seen)} tickets"


def test_bool_typed_marker_fields_are_rejected(repo: str) -> None:
    """``bool`` is an ``int`` subclass in Python, so a marker carrying ``true`` where a
    timestamp belongs would otherwise be arithmetic-compared and silently trusted."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 38_000_000_000_000
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now)
    marker = Q._gate_marker_path(tracker)
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    for payload in ({"next_eligible_ns": None, "written_ns": True}, {"next_eligible_ns": True}):
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"written_ns": now, **payload}, fh)
        assert Q.pending_enrichment(now + 1, tracker) == [tid]


# ── one store, one gate marker (bug nuclear-calm-heron da68-fc7c-068c-4c53) ──────────────────
#
# Same class as the drain lock and the compaction sidecars (bug 93a9-66cf-e681-4f49): the
# marker was derived from `os.path.dirname(tracker)` without resolving the `.tickets-tracker`
# SYMLINK a `make worktree` worktree holds, so each worktree kept its own marker for the ONE
# shared queue. The marker only ever says "nothing is pending", so a worktree-local one can
# assert quiet for a store another worktree just made noisy — and a mutation's invalidation
# (`_clear_gate_marker`) unlinked the WRONG file, leaving the stale claim standing.


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


def test_two_worktrees_of_one_store_share_one_gate_marker(tmp_path: Path) -> None:
    """Path invariance: two worktree views of one store derive the SAME marker path, and it is
    the canonical store's — so the marker describes the store, not the caller's view."""
    canonical = _canonical_store(tmp_path)
    a = _worktree_tracker(tmp_path, canonical, "worktree-a")
    b = _worktree_tracker(tmp_path, canonical, "worktree-b")

    assert Q._gate_marker_path(a) == Q._gate_marker_path(b), (
        "the gate marker is keyed on the worktree"
    )
    assert Q._gate_marker_path(a) == Q._gate_marker_path(canonical), (
        "the gate marker is not on the canonical store"
    )


def test_a_mutation_in_one_worktree_invalidates_the_marker_another_wrote(
    tmp_path: Path,
) -> None:
    """The behavioural consequence the path test cannot see: a queue mutation's invalidation
    must hit the marker regardless of which worktree wrote it, or a stale "nothing pending"
    claim outlives the write that falsified it."""
    canonical = _canonical_store(tmp_path)
    a = _worktree_tracker(tmp_path, canonical, "worktree-a")
    b = _worktree_tracker(tmp_path, canonical, "worktree-b")

    Q._write_gate_marker(a, 1_000, None)
    assert Q._read_gate_marker(b) is not None, "precondition: one marker, visible to both"

    Q._clear_gate_marker(b)

    assert Q._read_gate_marker(a) is None, (
        "a mutation from one worktree left another worktree's stale marker standing"
    )


# ── scoped gate marker: a standing backlog no longer disables the fast path store-wide ───────
# (task tireless-convenable-canvasback 4144-75d3-6af1-47d3)
#
# The 958e marker was all-or-nothing: written only by a scan that found NOTHING pending, so a
# single standing pending entry forced a full store walk on every write. The marker now also
# records the LIVE set (ids with an enqueued, not-DONE entry at full-scan time), asserting
# "nothing is pending OUTSIDE this set" — probes then reduce only the live entries. Honesty is
# unchanged: pending verdicts always come from reducing real queue events; the marker only
# ever asserts absence. Counted by instrumenting reduce_ticket, never wall clock.


@pytest.mark.parametrize("n_tickets", [4, 40])
def test_standing_backlog_probe_scales_with_backlog_not_store(
    repo: str, monkeypatch: pytest.MonkeyPatch, n_tickets: int
) -> None:
    """One permanently pending entry (never claimed, never done) must not cost a full store
    walk per probe: after one cold scan, a repeat probe reduces exactly the live set — one
    ticket — independent of store size."""
    tracker = _tracker(repo)
    now = 40_000_000_000_000
    for i in range(n_tickets):
        rebar.create_ticket("task", f"T{i}", repo_root=repo)
    tid = rebar.create_ticket("task", "backlog", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now)
    assert Q.pending_enrichment(now + 1, tracker) == [tid]  # cold: full walk, records the set
    seen = _count_reductions(monkeypatch)
    assert Q.pending_enrichment(now + 2, tracker) == [tid]
    assert seen == [tid], (
        f"a standing backlog of 1 reduced {len(seen)} tickets at n={n_tickets} — the gate is "
        "paying a full store walk"
    )


def test_scoped_marker_never_hides_a_new_enqueue(repo: str) -> None:
    """An enqueue AFTER the live set was recorded is never hidden behind it: the append
    invalidates the marker, so the next probe sees the newcomer."""
    tracker = _tracker(repo)
    now = 41_000_000_000_000
    t1 = rebar.create_ticket("task", "A", repo_root=repo)
    Q.enqueue(t1, soak_min=0, repo_root=repo, now_ns=now)
    assert Q.pending_enrichment(now + 1, tracker) == [t1]  # live set = {t1}
    t2 = rebar.create_ticket("task", "B", repo_root=repo)
    Q.enqueue(t2, soak_min=0, repo_root=repo, now_ns=now + 2)
    assert sorted(Q.pending_enrichment(now + 3, tracker)) == sorted([t1, t2])


def test_scoped_probe_does_not_refresh_the_ttl(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The TTL bounds the time since the last FULL walk. Scoped probes must not refresh it,
    or an entry hidden by the accepted append-during-scan race would stay hidden forever."""
    tracker = _tracker(repo)
    now = 42_000_000_000_000
    others = [rebar.create_ticket("task", f"T{i}", repo_root=repo) for i in range(3)]
    tid = rebar.create_ticket("task", "backlog", repo_root=repo)
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now)
    assert Q.pending_enrichment(now + 1, tracker) == [tid]  # full walk at now+1
    ttl = Q._GATE_MARKER_TTL_NS
    for probe_ns in (now + 2, now + ttl // 2, now + ttl):  # scoped probes across the window
        assert Q.pending_enrichment(probe_ns, tracker) == [tid]
    seen = _count_reductions(monkeypatch)
    assert Q.pending_enrichment(now + 1 + ttl + 1, tracker) == [tid]
    assert set(seen) >= {tid, *others}, (
        "a probe past the TTL did not perform a full walk — scoped probes refreshed the TTL"
    )


def test_wrong_typed_live_field_is_untrusted(repo: str) -> None:
    """A marker whose ``live`` field is wrong-typed is untrusted AS A WHOLE — degrading to
    the full scan, never to a trusted quiet claim standing beside a garbage live set."""
    tid = rebar.create_ticket("task", "T", repo_root=repo)
    tracker = _tracker(repo)
    now = 43_000_000_000_000
    Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now)
    marker = Q._gate_marker_path(tracker)
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    for bad_live in ("nonsense", [1, 2], {"a": 1}, [None]):
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"next_eligible_ns": None, "written_ns": now, "live": bad_live}, fh)
        assert Q.pending_enrichment(now + 1, tracker) == [tid], (
            f"marker with live={bad_live!r} was trusted"
        )


def test_soak_expiry_is_answered_from_the_scoped_path(
    repo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Time-driven transitions stay honest AND cheap: a soaking live entry becomes pending
    at its deadline via scoped reductions, not a full walk. The soak here is shorter than
    the marker TTL — a deadline beyond the TTL is (correctly) a full rescan under rule 3."""
    tracker = _tracker(repo)
    now = 44_000_000_000_000
    others = [rebar.create_ticket("task", f"T{i}", repo_root=repo) for i in range(3)]
    del others
    tid = rebar.create_ticket("task", "soaking", repo_root=repo)
    assert 6 * _MIN < Q._GATE_MARKER_TTL_NS  # precondition: the deadline probe is TTL-fresh
    Q.enqueue(tid, soak_min=5, repo_root=repo, now_ns=now)
    assert Q.pending_enrichment(now + 1, tracker) == []  # quiet; records deadline + live set
    seen = _count_reductions(monkeypatch)
    assert Q.pending_enrichment(now + 6 * _MIN, tracker) == [tid]
    assert seen == [tid], (
        f"the soak-deadline rescan reduced {len(seen)} tickets instead of only the live entry"
    )


def test_scoped_path_preserves_the_service_order(repo: str) -> None:
    """The f400 service-order contract holds on the scoped path: same ids, same
    ``(not_before_ns, id)`` order as the full walk for the same store state."""
    tracker = _tracker(repo)
    now = 45_000_000_000_000
    tids = [rebar.create_ticket("task", f"T{i}", repo_root=repo) for i in range(3)]
    # Distinct soak deadlines, deliberately NOT in creation order.
    for tid, offset in zip(tids, (2, 0, 1), strict=True):
        Q.enqueue(tid, soak_min=0, repo_root=repo, now_ns=now + offset)
    cold = Q.pending_enrichment(now + 10, tracker)  # full walk; records the live set
    scoped = Q.pending_enrichment(now + 11, tracker)  # scoped
    Q._clear_gate_marker(tracker)
    full = Q.pending_enrichment(now + 11, tracker)  # full walk again, same store state
    assert scoped == full == cold
    expected = [
        tid for _, tid in sorted((offset, tid) for tid, offset in zip(tids, (2, 0, 1), strict=True))
    ]
    assert scoped == expected, "the scoped path broke the (not_before_ns, id) service order"
