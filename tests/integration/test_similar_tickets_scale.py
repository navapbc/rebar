"""Scale, self-sufficiency and store-purity properties of similar-ticket retrieval.

Ticket 47c8-97ac-eff3-4e88. These cost real time and so sit in the integration tier:
they build a 10,000-ticket store and drive 100 real git-backed writes.

Three claims are under test, each one a thing the unit tier cannot reach:

* the warm path does no hashing at the scale the live store already has, and costs a
  small fraction of the cold rebuild it avoids on whatever host is measuring;
* retrieval is lexical, so it answers with every model-provider credential removed
  from the environment, and needs no daemon, background thread, MCP server or CI;
* retrieval is read-only with respect to the store -- queries add no commit and no
  event type to the ``tickets`` branch.

The 10,000-ticket store is synthesized by writing event files directly. Going through
``rebar.create_ticket`` would cost one git commit each (measured at 110 ms, so about
18 minutes) to produce a store the reducer folds identically.

Every assertion in this module was swept for vacuity (ticket 458e-c5b2-96ef-4f8d). The
cold test carried a pair that could not fail -- its percentile figure is computed from
measured durations, so being a float and being positive were guaranteed by
construction -- and both are gone, replaced by the convergence claim that test now
makes. Two assertions were examined and KEPT because each can fail: ``assert not
present`` fails if a credential survives ``delenv``, and ``assert results`` fails if
the credential-free query ranks nothing.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

import rebar
from rebar.similar import catchup, find_similar
from rebar.similar.catchup import catch_up, live_entries
from rebar.similar.index import index_path, open_index
from rebar.similar.lexical import query_text, search

pytestmark = pytest.mark.integration

#: How much cheaper a warm query must be than the cold rebuild it avoids. Both are
#: measured on the same host in the same run, so this says something about the code
#: where an absolute millisecond budget would only say something about the machine.
#: Observed margins are far wider than this -- roughly 100x on an idle box -- so the
#: threshold catches an algorithmic regression without tracking hardware noise.
WARM_TO_COLD_RATIO = 10

#: The scale the timing test refuses to measure below -- the live store's own order.
MIN_TICKETS = 10_000

#: Enough samples that a single first-read outlier falls outside the 95th percentile.
WARM_RUNS = 25

#: A cold rebuild costs seconds, so the cold sample is small and says so.
COLD_RUNS = 5

_WORDS = (
    "quota retry backoff cache eviction reducer bridge gerrit index lexical "
    "ticket gate review verify signature migration rollback daemon latency schema"
).split()

#: Anything that could carry a model-provider credential, matched by SHAPE rather than
#: by vendor name: rebar targets many providers and a fixed vendor list would go stale.
_CREDENTIAL_RE = re.compile(
    r"(API_KEY|ACCESS_KEY|SECRET|SESSION_TOKEN|BEARER|CREDENTIAL|_TOKEN$|^AWS_|^ANTHROPIC_"
    r"|^OPENAI_|^AZURE_|^GOOGLE_|^GEMINI_|^VERTEX_|^BEDROCK_)"
)


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = max(0, int(fraction * len(ordered)) - 1)
    return ordered[index]


def _write_ticket(tracker: Path, n: int) -> None:
    tid = f"{n:04x}-aaaa-bbbb-cccc"
    ticket_dir = tracker / tid
    ticket_dir.mkdir()
    event_uuid = str(uuid.uuid4())
    ts = 1793585542519320001 + n
    words = [_WORDS[(n + i) % len(_WORDS)] for i in range(5)]
    payload = {
        "uuid": event_uuid,
        "env_id": "test-env",
        "timestamp": ts,
        "event_type": "CREATE",
        "author": "t",
        "author_email": "t@e.com",
        "data": {
            "id": tid,
            "alias": f"alias-{n}",
            "title": " ".join(words[:2]),
            "description": " ".join(words) * 3,
            "ticket_type": "task",
            "priority": 2,
            "tags": [],
            "parent_id": "",
            "creation_channel": "python",
        },
    }
    (ticket_dir / f"{ts}-{event_uuid}-CREATE.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


@pytest.fixture(scope="module")
def large_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A synthetic store at live-store scale, built once for the whole module."""
    tracker = tmp_path_factory.mktemp("large") / "store" / ".tickets-tracker"
    tracker.mkdir(parents=True)
    for n in range(MIN_TICKETS):
        _write_ticket(tracker, n)
    return tracker


@pytest.fixture
def git_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git-backed store, so the ``tickets`` branch exists to inspect."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    rebar.init_repo(repo_root=str(repo))
    return repo


def test_a_warm_query_hashes_nothing_and_undercuts_the_cold_rebuild_it_avoids(
    large_store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The warm path, measured end to end through the public seam.

    What is ASSERTED here is work done, not wall-clock. An absolute millisecond budget
    measures the machine: on a shared CI runner this same store has produced a p95 of
    545 ms against roughly 100 ms on an idle developer box, and neither number says
    anything about the code. The two claims below hold on any hardware:

    * steady state hashes NOTHING. The two-level filter is the whole reason the warm
      path is cheap, and a regression that let the mtime pre-filter fall through would
      hash every directory in the store -- visible as a count, not as a timing.
    * a warm query costs a small fraction of the cold rebuild it avoids, measured on
      THIS host in THIS run, so the hardware cancels out of the comparison.

    The first query after a cold build is excluded from the steady-state claim because
    it is EXPECTED to hash: re-indexing calls the reducer, which writes a
    ``.cache.json`` into each ticket directory AFTER catch-up recorded that directory's
    mtime, so the next run sees every just-indexed directory as moved. It re-hashes,
    finds the hashes unchanged, writes nothing, and the stored mtimes converge. That
    one converging query -- not page-cache warming -- is the outlier this test used to
    absorb into a percentile.
    """
    assert len(live_entries(large_store)) >= MIN_TICKETS

    conn = open_index(large_store)
    assert conn is not None
    cold_started = time.perf_counter()
    cold = catch_up(large_store, conn)
    cold_ms = (time.perf_counter() - cold_started) * 1000
    conn.close()
    assert cold["indexed"] >= MIN_TICKETS

    def _query() -> None:
        find_similar(str(large_store), title="quota retry", description="backoff cache eviction")

    _query()  # the converging query: expected to re-hash, so not part of the sample

    hashed = 0
    real_dir_hash_of = catchup.dir_hash_of

    def _counting_dir_hash_of(ticket_dir: str) -> str:
        nonlocal hashed
        hashed += 1
        return real_dir_hash_of(ticket_dir)

    monkeypatch.setattr(catchup, "dir_hash_of", _counting_dir_hash_of)

    samples = []
    for _ in range(WARM_RUNS):
        started = time.perf_counter()
        _query()
        samples.append((time.perf_counter() - started) * 1000)

    p95 = _percentile(samples, 0.95)
    with capsys.disabled():
        print(
            f"\nwarm find_similar over {MIN_TICKETS} tickets: "
            f"median={statistics.median(samples):.1f} ms p95={p95:.1f} ms "
            f"max={max(samples):.1f} ms (n={WARM_RUNS}) "
            f"| cold rebuild {cold_ms:.0f} ms | directories hashed while warm: {hashed}"
        )

    assert hashed == 0, (
        f"the warm path hashed {hashed} directories over {WARM_RUNS} queries; "
        "the mtime pre-filter is no longer skipping unchanged directories"
    )
    assert p95 < cold_ms / WARM_TO_COLD_RATIO, (
        f"a warm query's p95 ({p95:.1f} ms) is not {WARM_TO_COLD_RATIO}x cheaper than "
        f"the cold rebuild it avoids ({cold_ms:.0f} ms) on this host"
    )


def test_the_cold_rebuild_settles_so_an_immediate_second_catch_up_indexes_nothing(
    large_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cold cost is reported, not budgeted -- it is paid once per index lifetime.

    The sample is small because each rebuild indexes every ticket in the store; the
    95th percentile of ``COLD_RUNS`` samples is therefore its maximum, which is the
    conservative reading and is stated here rather than left to be inferred.

    The assertion is convergence, not duration. Re-indexing moves each directory's
    mtime after ``live_entries`` read it, so the run after a rebuild re-hashes every
    directory it just indexed (``catchup.py`` module docstring). Those hashes are
    unchanged, so that run must index nothing and drop nothing -- and it must have
    looked at the whole store while concluding so, which is what ``scanned`` pins
    down. Without that third clause a catch-up over an empty store would satisfy the
    other two.
    """
    samples = []
    for _ in range(COLD_RUNS):
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(index_path(large_store) + suffix)
            if candidate.exists():
                candidate.unlink()
        conn = open_index(large_store)
        assert conn is not None
        started = time.perf_counter()
        result = catch_up(large_store, conn)
        samples.append((time.perf_counter() - started) * 1000)
        conn.close()
        assert result["indexed"] >= MIN_TICKETS

    p95 = _percentile(samples, 0.95)
    with capsys.disabled():
        print(
            f"\ncold catch-up over {MIN_TICKETS} tickets: "
            f"median={statistics.median(samples):.0f} ms p95={p95:.0f} ms (n={COLD_RUNS})"
        )

    conn = open_index(large_store)
    assert conn is not None
    try:
        settled = catch_up(large_store, conn)
    finally:
        conn.close()
    assert settled["indexed"] == 0
    assert settled["dropped"] == 0
    assert settled["scanned"] >= MIN_TICKETS


def test_a_query_ranks_with_every_provider_credential_removed(
    large_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrieval is lexical. No credential, no network, no model."""
    cleared = [name for name in os.environ if _CREDENTIAL_RE.search(name)]
    for name in cleared:
        monkeypatch.delenv(name, raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_ACCESS_KEY_ID"):
        monkeypatch.delenv(name, raising=False)
        present = name in os.environ
        assert not present

    results = find_similar(
        str(large_store), title="quota retry", description="backoff cache eviction"
    )
    assert results
    assert all(0.0 <= r["score"] <= 1.0 for r in results)


def test_a_ticket_created_in_process_is_returned_by_the_next_query(
    git_store: Path,
) -> None:
    """Catch-up runs inside the read, so freshness needs nothing running alongside it."""
    tracker = git_store / ".tickets-tracker"
    threads_before = threading.active_count()

    rebar.create_ticket(
        "task", "ptarmigan dispatch", description="first body", repo_root=str(git_store)
    )
    assert find_similar(str(tracker), title="ptarmigan") != []

    created = rebar.create_ticket(
        "task", "wolverine dispatch", description="second body", repo_root=str(git_store)
    )
    results = find_similar(str(tracker), title="wolverine")
    assert [r["id"] for r in results] == [created]

    assert threading.active_count() == threads_before


def test_queries_add_no_commit_and_no_event_type_to_the_tickets_branch(
    git_store: Path,
) -> None:
    """The index is a derived cache. A hundred reads must leave the store byte-identical."""
    tracker = git_store / ".tickets-tracker"
    for n in range(100):
        rebar.create_ticket(
            "task",
            f"{_WORDS[n % len(_WORDS)]} heading {n}",
            description=" ".join(_WORDS[: (n % 8) + 2]),
            repo_root=str(git_store),
        )

    def _branch_head() -> str:
        return subprocess.run(
            ["git", "rev-parse", "tickets"],
            cwd=tracker,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _event_types() -> set[str]:
        """Event types read from the ``tickets`` TREE, not the working directory.

        Reading the tree is both what the criterion names and the stricter check: a
        cache file that leaked into a ticket commit would show up here, while a
        working-directory walk would also sweep in the tracker's own dotfiles.
        """
        listing = subprocess.run(
            ["git", "ls-tree", "-r", "tickets", "--name-only"],
            cwd=tracker,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        found = set()
        for rel in listing:
            parent, _, name = rel.rpartition("/")
            if not parent or parent.startswith(".") or not name.endswith(".json"):
                continue
            blob = subprocess.run(
                ["git", "show", f"tickets:{rel}"],
                cwd=tracker,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            found.add(json.loads(blob)["event_type"])
        return found

    head_before, types_before = _branch_head(), _event_types()
    assert types_before == {"CREATE"}

    for n in range(100):
        find_similar(str(tracker), title=_WORDS[n % len(_WORDS)], description="heading")

    assert _branch_head() == head_before
    assert _event_types() == types_before


def test_the_index_lives_outside_the_tracker_so_it_is_never_committed(
    git_store: Path,
) -> None:
    """A cache file inside the tracker would be swept into the next ticket commit."""
    tracker = git_store / ".tickets-tracker"
    rebar.create_ticket("task", "ptarmigan", description="body", repo_root=str(git_store))
    conn = open_index(tracker)
    assert conn is not None
    catch_up(tracker, conn)
    assert search(conn, query_text("ptarmigan", "body"))
    conn.close()

    built = Path(index_path(tracker))
    assert built.exists()
    assert tracker not in built.parents

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tracker,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status.strip() == ""
