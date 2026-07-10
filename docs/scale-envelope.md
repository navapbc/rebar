# Scale envelope

How large a rebar store can comfortably get, with **representative measured
numbers**. rebar is an event-sourced, git-backed ticket store: every write appends
an event and auto-commits it. That makes reads cheap (replay is linear and fast)
and makes write throughput bounded by the per-event git commit — the price of
durability. The numbers below let you plan for a multi-year, multi-thousand-ticket
tracker.

> These are order-of-magnitude figures from a quick local run (see
> [Measurement method](#measurement-method)), not a tuned benchmark — treat them as
> "what to expect," not guarantees. They were taken on a developer laptop (macOS,
> local SSD, no sync remote configured).

## Representative numbers

Measured against fresh isolated stores of 200 / 500 / 1000 tickets (one event per
ticket):

| Store size | `list_tickets()` (full) | `search()` | raw replay (`reduce_all_tickets`) |
|-----------:|------------------------:|-----------:|----------------------------------:|
| 200        | ~80 ms                  | ~20 ms     | ~12 ms                            |
| 500        | ~160 ms                 | ~40 ms     | ~30 ms                            |
| 1000       | ~320 ms                 | ~75 ms     | ~65 ms                            |

**Reads scale linearly and stay sub-second** well past a thousand tickets — a full
`list`/`search` replays the whole event log, and even at 1000 tickets that is a
fraction of a second. Raw replay (reduce only, no filtering/formatting) is the
floor; `list`/`search` add filter + presentation cost on top.

### Write / import throughput

| Operation | Rate |
|---|---|
| `create_ticket` (1 event, auto-commit + lock) | **~25–30 tickets/sec** |
| `import_tickets` (NDJSON, 1000 events) | ~25 events/sec (~40 s for 1000) |

Write throughput is dominated by the **per-event git commit + lock**, not by
reduce cost. Each single-event write takes the store lock, appends an event, and
commits it (and, when a sync remote exists, pushes it). This is the deliberate
durability trade for interactive writes: every write is immediately persisted and
shareable.

There is a batched-commit fast path
(`rebar._store.event_append.batch_stage_and_commit`, epic cold-stall-chalk) that
takes the store lock **once** and collapses N events into a **single** `git commit`,
but it exists **only for bulk import** — interactive writes
(`create`/`edit`/`transition`/`claim`/`link`/`comment`/…) keep committing
**one-event-per-commit** and retain the per-write durability guarantee unchanged.
Import is exempt from that guarantee because it is already a fundamentally different
transaction: it **already defers push** (it runs with `REBAR_SYNC_PUSH=off` and pushes
once at the end) and it is **idempotent-by-`source_id` and crash-resumable** (a re-run
re-scans for already-imported `source_id`s and skips them). So batching import trades
commit **granularity**, not the durability guarantee: a crash mid-import leaves either
a whole batch's commit or none, and the re-run resumes cleanly. It is invariant-safe
because replay, dedup, cross-clone union-merge convergence, and SNAPSHOT compaction all
key off each event's per-event UUID, not commit boundaries — every event remains its own
I2 uuid-named file, so a batched commit is indistinguishable from N single commits to
every reader.

### Git object growth

At 1000 single-event tickets the tracker's `tickets` branch held roughly:

- **~10,000 objects in-pack**, packed to **~3.2 MiB**;
- ~800 loose objects (~9 MiB) pending the next gc — normal between packs.

So a low-thousands-of-tickets store is a few MiB of git objects. Growth is roughly
linear in total *events* (not just live tickets), since history is append-only —
edits, transitions, comments, and links each add an event.

## Git maintenance & gc

rebar **trusts stock `git gc`** on the tickets worktree — it does **not** force
`gc.auto=0`. Because union recovery (`_store/sync.py`) keeps every ticket commit
ref-reachable, background gc is safe by construction and only ever collects truly
unreachable objects. At init (and on every re-init, so older trackers self-heal)
rebar:

- **`--unset gc.auto`** — sheds any stale `gc.auto=0` an older rebar wrote;
- **`gc.autoDetach=true`** — a triggered background gc forks and never serializes a
  foreground ticket write.

(Older rebar builds *did* set `gc.auto=0`; that was reversed — see
`init._migrate_gc_config` and `docs/concurrency.md`.)

**When to run maintenance yourself.** Background gc handles packing. For the
event-store's own compaction of long/verbose tickets (e.g. large session logs),
use:

- `rebar compact <id>` / `rebar compact-all` — compact a ticket / the whole store.
- `rebar fsck` — health-check the store (reports `PUSH_PENDING`, newer-than-binary
  event types, etc.).

Session logs are the usual growth driver; they are kept out of the graph/health
hot paths precisely so their size never taxes the operations that run constantly.

## Measurement method

Reproducible with a throwaway store (no CI harness is wired — this is a one-shot
measurement):

```python
import os, time, tempfile, subprocess
from pathlib import Path
os.environ["REBAR_SYNC_PUSH"] = "off"          # keep commits local, no push latency
import rebar

base = Path(tempfile.mkdtemp())
repo = base / "store"; repo.mkdir()
subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.email", "b@e.com"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.name", "Bench"], cwd=repo, check=True)
os.environ["REBAR_ROOT"] = str(repo)
rebar.init_repo(repo_root=str(repo))

N = 1000
t = time.perf_counter()
for i in range(N):
    rebar.create_ticket("task", f"bench {i}", description="body " * 20)
print(f"create: {N/(time.perf_counter()-t):.0f}/s")

t = time.perf_counter(); rebar.list_tickets(); print(f"list: {(time.perf_counter()-t)*1000:.0f}ms")
t = time.perf_counter(); rebar.search("bench 42"); print(f"search: {(time.perf_counter()-t)*1000:.0f}ms")
```

Git object growth: `git count-objects -vH` inside the `.tickets-tracker/` worktree.

## Related

- [docs/import-export.md](import-export.md) — the NDJSON export/import path.
- [docs/concurrency.md](concurrency.md) — the lock, auto-commit/push, and union recovery.
- [docs/architecture.md](architecture.md) — the event-sourced store internals.
