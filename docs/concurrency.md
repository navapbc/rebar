# rebar concurrency model

rebar is operated concurrently from multiple machines, clones, and interfaces
(library / CLI / MCP) over **one** logical ticket store. Its concurrency safety
comes from a small set of structural invariants, **not** from locks-in-the-large.

The model in one sentence: **every mutation is a new, globally-unique,
append-only event file; state is a pure deterministic replay of those events;
independent clients converge by git merge-as-union plus optimistic concurrency.**

This document is the authoritative, code-cross-referenced statement of that
model. Every invariant below (I1–I9) gates every change to the system.

> Citations are `path:line` into `src/rebar/_engine/` unless noted. Line numbers
> drift; the surrounding function names are the durable anchor.

---

## Storage shape

Tickets live on a dedicated `tickets` git **orphan branch**, checked out as a
worktree at `<repo>/.tickets-tracker/`. Each ticket is a directory; each mutation
is one JSON **event file** inside it. State is never stored — it is *computed* by
replaying the events (the reducer, `ticket_reducer/`).

A reconciler bidirectionally syncs tickets with Jira; it is the one component
allowed a cross-client advisory lock (see I6).

---

## The invariants (I1–I9)

### I1 — Append-only
Never modify or delete an existing event file. The sole exception is
**compaction**, which runs under the write lock and writes a `SNAPSHOT` event
that folds the events it retires, renaming the folded files to `*.retired`
(git represents this as adds/removes — still merge-as-union). See `ticket-compact.sh`.

### I2 — Globally-unique event filenames
Every new event is `${timestamp}-${uuid}-${TYPE}.json`
(`ticket-lib.sh:85`, `ticket-lib.sh:647`), where `${timestamp}` is a high-resolution
(nanosecond) clock prefix and `${uuid}` is a fresh UUID. Two independent clients
writing concurrently therefore **never collide on a filename**; git merges the two
new files as a union with no conflict. **New event kinds MUST use this scheme.**

### I3 — Reads are side-effect-free except local, rebuildable caches
The only read-side write is the per-ticket `.cache.json`
(content/size-keyed, written tmp-then-rename: `ticket_reducer/_cache.py:25-30`).
No feature may introduce a **committed** shared mutable file — it would create
cross-client merge conflicts.

- **I3a:** `.cache.json` and any per-clone index file MUST be in the tracker's
  committed `.gitignore` and MUST never be staged by a maintenance `git add -A`
  path. (See WS5a for the search-index case.)

### I4 — State-dependent mutations use optimistic concurrency
Any op whose correctness depends on current state (status `transition`, and any
compound op such as `claim`) MUST re-read the relevant state **under the write
lock** and reject on mismatch with **exit 10**, surfaced uniformly as
`ConcurrencyError` across library/CLI/MCP
(`ticket-transition.sh:397` `sys.exit(10)` / `:558` `exit 10`;
`src/rebar/__init__.py:110` maps `returncode == 10` → `ConcurrencyError`).

### I5 — Single locked write path
All writes go through the flock-guarded append+commit path: atomic
tmp-then-rename + `git add <event>` + `git commit`, all under
`.tickets-tracker/.ticket-write.lock` (`_flock_stage_commit`,
`ticket-lib.sh:270-...`, FD 200 at `:353`/`:493`). No side-channel writes. The
reconciler's event-file write shares this lock via the `event_append` module
(`write_lock` / `append_event`) rather than writing unserialized.

**No-flock platforms.** Where util-linux `flock` is absent (default macOS),
`_flock_stage_commit` falls back to an **atomic `mkdir` lock** (`mkdir` is atomic
on POSIX). Its behaviour under many concurrent local agents is pinned by a CI
stress test (`tests/scripts/test-mkdir-lock-stress.sh`, forced on Linux via the
`REBAR_FORCE_MKDIR_LOCK=1` hook): N concurrent writers lose **zero** events and
finish within a bounded wait — measured ~2 s for N=15, no starvation. Lost events
or unbounded blow-up fail the test.

### I6 — No NEW cross-client lock; no shared mutable index
Cross-client coordination is **only** git merge-as-union + optimistic
concurrency. No feature may require a lock spanning clients/machines, nor a
committed index/aggregate that concurrent clients would both rewrite.

- **Sanctioned, grandfathered exception:** the reconciler's
  `.reconciler-pass-lock` (`rebar_reconciler/_advisory_lock.py:62`) is a committed,
  tickets-branch, cross-client advisory lock. It is single-writer-by-design (only
  one reconciler should run at a time). It also performs a ref-advance
  **compare-and-swap** (`git update-ref refs/heads/tickets <new> <old>`,
  `_advisory_lock.py:86-99`) that retries on a concurrent writer. This is the one
  allowed exception — **not** a precedent for new cross-client locks.

### I7 — Derived/aggregate data is computed from replay or stored local-only
Search indexes, counters, memory stores, etc. are either recomputed from the
event log on demand or cached **local-and-rebuildable** (gitignored, uncommitted).

### I8 — Cross-client ordering is best-effort under clock skew; only STATUS fork resolution is skew-independent
Replay orders events by the `${timestamp}` filename prefix. With skewed client
clocks, COMMENT/EDIT interleaving across clients is best-effort. **STATUS forks
are resolved deterministically and skew-independently by the event's own UUID:**
the lexically-lower UUID wins (`ticket_reducer/_processors.py:81-115`,
`if not existing_uuid or incoming_uuid <= existing_uuid`). Any new
state-dependent merge logic MUST resolve forks by UUID (or another
skew-independent key), **never by timestamp alone**.

### I9 — Compaction is safe against concurrent remote appends
Compaction (under the per-clone write lock) writes a SNAPSHOT folding the events
it retires; a remote clone appending a *new* (unique-named) event merges as a
union. The SNAPSHOT must already fold any event its result depends on. New
compaction-like operations MUST never retire an event whose content a
not-yet-folded state could still need, and never assume the per-clone lock
excludes remote writers.

---

## The sync / reconvergence algorithm

Two paths move commits between clones; **both reconverge by MERGE-as-union, never
rebase** (bug 637b: an interrupted rebase strands picks as dangling commits, and
compaction `*.retired` renames conflict under rebase where merge unions cleanly).

### Outbound — push (on every write)
**Every** rebar write (`create`/`edit`/`transition`/`claim`/`link`/…) auto-commits
its event and then auto-pushes — so local ticket activity (including test/scratch
tickets) propagates to the shared `origin/tickets` **immediately**, with no
separate push step. `_push_tickets_branch` (`ticket-lib.sh:482`) pushes
`HEAD:tickets` whenever an `origin` remote exists (no remote → it is a no-op and
nothing is shared). On a non-fast-forward rejection it **fetches + merges**
`origin/tickets` (union) and retries (bounded). It refuses to merge through a
rebase/merge recovery state (`_check_no_rebase_in_progress`, `ticket-lib.sh:217`).
Push is **best-effort**: a failed push (no network, unresolvable non-fast-forward,
recovery state) never fails the caller — it warns, leaves local commits intact,
and the branch stays diverged. `rebar fsck` surfaces that divergence as a
`PUSH_PENDING` notice (`ticket-fsck.sh`, Check 4.5) so it is not silent.

**Push policy — `REBAR_PUSH`** (read at the `_push_tickets_branch` chokepoint, so
CLI / library / MCP honour it uniformly; case/space-insensitive; default
`always`):

| value    | behaviour |
|----------|-----------|
| `always` | synchronous push before the write returns (default — real-time propagation is a first-order requirement). |
| `async`  | return immediately; the (identical, best-effort) push runs in a detached background job. Convergence is unchanged — `fsck` still reports `PUSH_PENDING` until it lands, and a non-fast-forward still fetches+merges+retries. Use when an agent claims a batch and per-write network latency would serialize the run. |
| `off`    | never push; commits stay local (`fsck` reports `PUSH_PENDING`). For offline/throwaway work. |

Pinned by `tests/scripts/test-rebar-push-policy.sh`.

`rebar import` uses `off` internally for its whole run and pushes once at the end,
so a bulk import pays one round-trip rather than one per event; it still does one
commit + one lock cycle per event (no batch primitive yet). See
[import-export.md](import-export.md) for the accepted large-import limitation and
the pre-compact guidance.

### Inbound — background sync (periodic, on reads/commands)
`_reconverge_tickets` (`ticket-sync.sh`) runs at most once per minute per clone.
It runs **under the write lock** (`.ticket-write.lock`) so it cannot race a
concurrent local appender's `git add`/`commit`. The policy:

```
if tracker is in a rebase/merge recovery state:        # I9 / bug 637b
    skip — never reset/merge through recovery; hint fsck-recover
fetch origin tickets                                   # (network; best-effort)
if no origin/tickets: return

if merge-base(HEAD, origin/tickets) is empty:          # UNRELATED histories
    merge --allow-unrelated-histories origin/tickets   # UNION both orphans:
        on conflict: merge --abort; keep local; hint fsck  # keep EVERY local
                                                       # commit (UUID-named event
                                                       # files never collide;
                                                       # shared mutable root files
                                                       # -> .gitattributes
                                                       # merge=ours). Never reset.
else:                                                  # RELATED histories
    local_ahead = rev-list origin/tickets..HEAD        # measured by HEAD,
                                                       # NOT the branch ref!
    if local_ahead is empty:
        reset --hard origin/tickets                    # fast-forward adoption
                                                       # (origin ⊇ HEAD; discards
                                                       # nothing local)
    elif origin/tickets is ancestor of HEAD:
        return                                          # local strictly ahead
    else:                                               # diverged
        merge origin/tickets   (union)
        on conflict: merge --abort; keep local; hint fsck   # never reset,
                                                            # never hard-fail a read
```

**Why HEAD, not the branch ref (the WS3 data-loss fix).** The tracker worktree can
be in a detached-HEAD-local-ahead state (after an interrupted rebase, or on older
git): a local commit advances `HEAD` but not `refs/heads/tickets`. The previous
guard tested `origin/tickets..tickets` (the lagging *branch ref*), which read
empty in that state, so the sync `git reset --hard origin/tickets` **destroyed the
un-pushed local commit**. Measuring local-ahead by `origin/tickets..HEAD` closes
this. (Regression tests: `tests/scripts/test-ticket-sync-detached-head-local-ahead.sh`,
`tests/integration/test_concurrency_regression.py`.)

**Why union, not reset — and the safety invariant (epic 97e7 / P1.4).** The
unrelated-history case used to `reset --hard origin/tickets`, which **orphaned**
every local-only commit into the reflog. That is the lone reason older rebar
forced `gc.auto=0`: the reflog was the recovery net, and stock `git gc` could
expire it. The fix follows the universal peer pattern (git-bug, git-appraise,
jujutsu): make recovery **non-destructive** so the reflog is never load-bearing.

> **INVARIANT.** After union recovery, every commit rebar cares about is
> ref-reachable from the `tickets` branch; therefore stock `git gc` is safe by
> construction — it only ever collects truly *unreachable* objects.

This is jujutsu's "gc-reachability == recovery guarantee" co-design, achieved for
free: if commits are never orphaned, gc has nothing unsafe to collect. So rebar no
longer touches `gc.auto` (init `--unset`s any stale `gc.auto=0` and sets
`gc.autoDetach=true` so a forked background gc never serializes a foreground
write); stock background `git gc` reclaims loose/pack growth on its own. The two
union merges can in principle conflict only on the **shared mutable root files**
(`.bridge_state/bindings.json`, the `.reconciler-*` lock/gate files), which the
tickets-branch `.gitattributes` resolves `merge=ours` (they are per-pass derived
caches the reconciler rebuilds, never ticket events; `merge=union` would line-union
JSON into invalid JSON). UUID-named ticket-event files never collide. A genuine
conflict still aborts → keeps local → hints `fsck` (never a hard read failure).

**Scale-up posture.** `git gc`'s default cadence (`gc.auto`, ~6700 loose objects)
suffices for normal stores; very large/active stores can schedule `git maintenance
run` out of band. Git's own ~30-day unreachable-reflog window remains as a free
backstop — but rebar no longer *depends* on it for correctness.

### Read-freshness policy (uniform across CLI, library, and MCP)

Every **read** — `show` / `list` / `ready` / `search` / `deps` — runs the same
throttled (≤1/min) best-effort fetch + reconverge **before** replaying, so the
result reflects collaborators' pushes within at most one minute. This is a single
contract shared by all three interfaces: the CLI dispatcher's read arms, the
library functions (`rebar.show_ticket`, `rebar.list_tickets`, …), **and** the MCP
read tools all funnel through one implementation — `ticket_reads` in the engine
(`src/rebar/_engine/ticket_reads.py`), with `rebar/_reads.py` as the
library/MCP facade. `ticket_reads.ensure_fresh()` reuses the exact mechanism above:
the `/tmp/.ticket-sync-<md5>` throttle marker **and** the `_reconverge_tickets`
function in `ticket-sync.sh` (one fetch/merge implementation, no reinvention). The
CLI and in-process reads share the same marker, so they never double-fetch within
a minute.

Previously this fetch lived only in the bash dispatcher's `_ensure_initialized`,
so CLI reads synced but library/MCP reads did **not** — making MCP (the primary
agent surface) the *stalest* interface. Collapsing the dual read path
([story 23d2-e0f3](../session-logs/2026-06-09-architecture-review.md) Rec 2) moved
freshness into the native read path so all three interfaces agree.

**Opt out** of the fetch when you want a pure-local replay (offline, hot loops,
or when a write already synced): set `REBAR_SYNC_PULL=off` (the `sync.pull` policy,
honored by all interfaces; deprecated alias `REBAR_NO_SYNC=1`) or pass the
`--no-pull` flag to any read subcommand (`rebar list --no-pull`; deprecated alias
`--no-sync`). The reducer's local `.cache.json` (I3/I3a) is still used; only the
network fetch/merge is skipped. (Temp repos with no remote set `REBAR_SYNC_PULL=off`
together with `REBAR_SYNC_PUSH=off` to skip both directions; the former private
`_TICKET_TEST_NO_SYNC` flag was removed in favor of these.)

---

## Doctrine compliance is a gate

A change that cannot satisfy I1–I9 is **redesigned, not merged**. The executable
form of this doctrine is `tests/integration/test_concurrency_regression.py`: two
clones writing disjoint and overlapping events, reconverging by fetch/merge, and
asserting union + one deterministic replayed state on both clones + identical
UUID-based fork resolution. Every write/sync change runs against it.
