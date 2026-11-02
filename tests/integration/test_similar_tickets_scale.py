"""Scale, self-sufficiency and store-purity properties of similar-ticket retrieval.

Ticket 47c8-97ac-eff3-4e88. These cost real time and so sit in the integration tier:
they build a 10,000-ticket store and drive 100 real git-backed writes.

Three claims are under test, each one a thing the unit tier cannot reach:

* the warm path holds its latency budget at the scale the live store already has;
* retrieval is lexical, so it answers with every model-provider credential removed
  from the environment, and needs no daemon, background thread, MCP server or CI;
* retrieval is read-only with respect to the store -- queries add no commit and no
  event type to the ``tickets`` branch.

The 10,000-ticket store is synthesized by writing event files directly. Going through
``rebar.create_ticket`` would cost one git commit each (measured at 110 ms, so about
18 minutes) to produce a store the reducer folds identically.
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
from rebar.similar import find_similar
from rebar.similar.catchup import catch_up, live_entries
from rebar.similar.index import index_path, open_index
from rebar.similar.lexical import query_text, search

pytestmark = pytest.mark.integration

#: The documented warm budget for a query at live-store scale.
WARM_BUDGET_MS = 500.0

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


def test_a_warm_query_holds_its_latency_budget_at_live_store_scale(
    large_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The warm path, measured end to end through the public seam.

    The sample deliberately includes the first query after the cold build. That read
    pulls the dirstate pages in and has been observed near 680 ms against a steady
    state near 100 ms; at this sample size the 95th percentile excludes exactly one
    such outlier, which is what a p95 is for. Hiding it with a discarded warm-up
    would measure a kinder path than a caller gets.
    """
    assert len(live_entries(large_store)) >= MIN_TICKETS

    conn = open_index(large_store)
    assert conn is not None
    cold = catch_up(large_store, conn)
    conn.close()
    assert cold["indexed"] >= MIN_TICKETS

    samples = []
    for _ in range(WARM_RUNS):
        started = time.perf_counter()
        find_similar(str(large_store), title="quota retry", description="backoff cache eviction")
        samples.append((time.perf_counter() - started) * 1000)

    p95 = _percentile(samples, 0.95)
    with capsys.disabled():
        print(
            f"\nwarm find_similar over {MIN_TICKETS} tickets: "
            f"median={statistics.median(samples):.1f} ms p95={p95:.1f} ms "
            f"max={max(samples):.1f} ms (n={WARM_RUNS})"
        )
    assert p95 < WARM_BUDGET_MS


def test_the_cold_catch_up_cost_at_that_scale_is_emitted_as_a_number(
    large_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cold cost is reported, not budgeted -- it is paid once per index lifetime.

    The sample is small because each rebuild indexes every ticket in the store; the
    95th percentile of ``COLD_RUNS`` samples is therefore its maximum, which is the
    conservative reading and is stated here rather than left to be inferred.
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
    assert isinstance(p95, float)
    assert p95 > 0.0


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
