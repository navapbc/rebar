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
replaying the events (the reducer, `reducer/`).

A reconciler bidirectionally syncs tickets with Jira; it is the one component
allowed a cross-client advisory lock (see I6).

---

## The invariants (I1–I9)

### I1 — Append-only
Never modify or delete an existing event file. The sole exception is
**compaction**, which runs under the write lock and writes a `SNAPSHOT` event
that folds the events it retires, renaming the folded files to `*.retired`
(git represents this as adds/removes — still merge-as-union). See
`src/rebar/_commands/compact.py` (the fold loop `os.rename(fp, fp + RETIRED_SUFFIX)`);
the shared `RETIRED_SUFFIX` + `is_active_event()` contract lives in
`src/rebar/reducer/_cache.py` and is the single definition imported by compaction,
the reducer (both listing paths), and fsck. The SNAPSHOT is written atomically
*before* the renames, so a crash mid-fold leaves a valid SNAPSHOT plus some
already-`.retired` sources; a re-compact short-circuits on the SNAPSHOT and skips
files already retired (idempotent), and a rename failure reverses the completed
renames (atomic — all sources retired or none).

**Rollback-failure recovery (compact → fsck).** When a forward rename fails,
compaction reverses the renames it completed. The uncommitted SNAPSHOT is removed
**only if that reverse is CLEAN** (every completed rename reversed) — that returns
the store to its exact pre-fold state, so the SNAPSHOT is a stray artifact. If **any
reverse-rename also fails**, the SNAPSHOT is **intentionally RETAINED** (it carries
the folded effect of the source now stuck as `*.retired`; removing it would lose that
effect from both an active event *and* the snapshot — silent data loss). The retained
SNAPSHOT plus a reversed-to-active source is a `SNAPSHOT_INCONSISTENT` state that
`rebar fsck --repair-snapshots` rebuilds from the full log; compaction emits a
`rollback incomplete … run fsck` diagnostic pointing there. **Reads are already
correct in this mixed window** — the reversed-to-active source keeps its original
(pre-snapshot) filename, sorts before the SNAPSHOT, and is positionally skipped during
replay, so it is never double-counted (the `fsck` repair is hygiene, not a read fix).

**`.retired` lifecycle.** Retired files are kept **permanently** for now — an
accepted storage tradeoff that guarantees a folded source is never lost and can
never be resurrected into a `SNAPSHOT_INCONSISTENT`. A branch-wide `.retired`
garbage-collection sweep is a documented **follow-up** — tracked as
`polite-antivirus-bedbug` (`536b-8930-b922-4063`, status `idea`, linked
`discovered_from` b306) — safe only past causal stability (once no clone can still
be mid-reconvergence against the pre-compaction events). `.retired` files are **benign under a code rollback**:
an older clone whose reducer/fsck predate `is_active_event` still ignores them,
because it lists events by the `*.json` glob / `.endswith(".json")` filter and a
`*.json.retired` name matches neither.

### I2 — Globally-unique event filenames
Every new event is `${timestamp}-${uuid}-${TYPE}.json`
(`ticket-lib.sh:85`, `ticket-lib.sh:647`), where `${timestamp}` is a high-resolution
(nanosecond) clock prefix and `${uuid}` is a fresh UUID. Two independent clients
writing concurrently therefore **never collide on a filename**; git merges the two
new files as a union with no conflict. **New event kinds MUST use this scheme.**

### I3 — Reads are side-effect-free except local, rebuildable caches
The only read-side write is the per-ticket `.cache.json`
(content/size-keyed, written tmp-then-rename: `reducer/_cache.py:25-30`).
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

### I4a — Parent-first claim/transition cascade (`open → in_progress`)
Grabbing a child grabs its **open** parent first. `claim`, and the
`open → in_progress` `transition`, run the same operation on the ticket's parent
**before** the child whenever that parent is itself `open` — recursively up the
chain (top-most open ancestor first), so a descendant is never moved into progress
while an ancestor is left merely `open`. A `claim` cascade carries the same assignee
up the chain. A parent that is already `in_progress`/`closed`/`blocked` (or absent)
is **not** cascaded — only the requested ticket moves.

The cascade is **sequential and fail-fast, not transactional:** the parent op runs
to completion (its own commit + push) first; **if it fails the child op is not
attempted**, and the failure is re-raised with a message naming the parent as the
cause (`cannot claim <child>: claiming its parent <parent> failed first …`) while
**preserving the parent failure's exit code** — so a parent concurrency conflict is
still **exit 10 / `ConcurrencyError`** at the leaf call. (There is intentionally no
rollback if the parent succeeds and the child then fails: an ancestor sitting in
`in_progress` is the conservative, harmless direction.) Recursion is cycle-guarded
(an id already on the cascade stack, including a self-parent, is skipped). Only the
`open → in_progress` direction cascades — `close`/`reopen`/`blocked` never do
(closing has its own separate open-children guard). Implemented in
`src/rebar/_commands/claim.py` (`claim_compute`) and
`src/rebar/_commands/transition.py` (`transition_compute`), via the shared
`_resolve_open_parent` helper.

#### Gate interaction (the plan-review claim gate)

The cascaded parent claim is a *full* claim — so it runs the parent's **own**
plan-review claim gate (`verify.require_plan_review_for_claim`) when that gate is
enabled. Epics and stories are **not** gate-exempt (only `bug` and `session_log`
are), so claiming a leaf task can be **blocked by the parent's missing/stale
attestation**, and the error names the **parent** as the cause. Earn the parent's
attestation (`rebar review-plan <parent>`) — or claim the parent yourself first —
before claiming the child, or pass `--force`, which propagates up the cascade and
bypasses the gate at **each** level with an audit note. The same "the cascaded
operation is the *full* operation" rule is why the cascade also stamps your
`--assignee` onto every ancestor it claims. See
[plan-review-gate.md](plan-review-gate.md) for the attestation model the gate reads.

#### Cross-agent race ownership policy (two agents, one open parent)

The cascade above is the *single-agent* contract. When **two agents concurrently
start work on children of the same still-`open` parent**, the outcome follows the
ordinary optimistic-concurrency model — there is **no fail-fast across agents** and
**no rollback of a losing agent's writes**. Two sub-cases:

- **Different children of the same parent.** Each child simply carries its own
  single claim (they never contend). The contention is only on the *parent*: both
  cascades move the parent `open → in_progress`, which is a concurrent status change
  on one ticket. On the same tracker the write lock **serializes** them (the second
  agent, arriving after the parent is already `in_progress`, does **not** re-cascade —
  it leaves the parent as-is). Across offline clones the two parent claims are a
  **STATUS fork** resolved deterministically by the HLC/UUID tie-break on merge, and
  the resolution is surfaced as **`STATUS_FORK_RESOLVED`** on the *parent* (via `fsck`
  and in `show`'s `status_fork_resolutions`). The losing agent thereby learns its
  parent ownership was superseded.
- **The same child.** The child is *also* a concurrent claim, so it forks too and is
  resolved by the **same tie-break independently of the parent** — the child's winner
  **may differ** from the parent's winner (they are separate tickets with separate
  forks). Both forks surface as `STATUS_FORK_RESOLVED` on their respective tickets.

The losing side is never rolled back, but *how* it loses differs by locality. On the
**same tracker** the write lock serializes the two parent cascades, so the losing
cascade's parent claim is rejected **under the lock, before any event is committed** —
there is no orphaned parent claim, and the loser simply proceeds to claim its own child
(both agents succeed). **Offline**, both agents' claims commit independently, so the
losing agent's already-written claim(s) — on the parent and/or the child — are **left
in place (orphaned under the losing assignee); NOT retroactively rolled back or
tombstoned.** Convergence is by the HLC/UUID tie-break + the `STATUS_FORK_RESOLVED`
signal, never by deleting a committed event (I1 append-only).
Regression coverage: `tests/integration/test_concurrency_regression.py`
(`…parent_cascade_same_tracker_race…`, `…parent_cascade_two_clone_offline_race…`).

### I5 — Single locked write path
All writes go through the lock-guarded append+commit path: atomic
tmp-then-rename + `git add <event>` + `git commit`, all under the tickets-tracker
write lock held by `rebar._store.lock` (`write_lock` / `acquire`). No side-channel
writes. The reconciler's event-file write shares this lock via the `event_append`
module (`write_lock` / `append_event`) rather than writing unserialized. (The
former bash `_flock_stage_commit` write core has been retired; only this Python
lock remains.)

**The dual-window lock (permanent contract).** By default the lock takes BOTH a
`fcntl.flock(LOCK_EX)` on `.ticket-write.lock` AND an atomic `mkdir` lock at
`.ticket-write.lock.d` (acquired fcntl-first, released mkdir-first). This is an
intentional, standing contract — not a migration residue. The fcntl leg is the fast
kernel-backed lock; the **mkdir leg is the portable second window** — `mkdir` is
atomic on POSIX, so mutual exclusion holds even where util-linux `flock` is absent
(default macOS), and the mkdir owner-stamp backs the foreign-host / shared-filesystem
reclamation check. Its behaviour under many concurrent local agents is pinned by the
writer-storm regression test
(`tests/integration/test_store_concurrency.py::test_concurrent_writer_storm_no_loss`):
N concurrent writers lose **zero** events, because every writer takes both legs.
Lost events fail the test. Callers may pass `dual_window=False` for an fcntl-only
lock, but that is an opt-out, not the default.

### I6 — No NEW cross-client lock; no shared mutable index
Cross-client coordination is **only** git merge-as-union + optimistic
concurrency. No feature may require a lock spanning clients/machines, nor a
committed index/aggregate that concurrent clients would both rewrite.

- **Sanctioned, grandfathered exception:** the reconciler's pass-lock/phase-gate is a
  single-writer-by-design cross-client advisory lock (only one reconciler runs at a
  time). Its backend (epic dust-troth-naval / ADR 0031) is a self-healing **bare-ref
  CAS lock on `refs/reconciler/*`** (`_ref_lock.py`) — a ref → blob, so it is **never
  in the tickets working tree and never union-merged**. Acquire is a create-only CAS;
  a lease + heartbeat lets a crashed holder's lock be reclaimed after one lease
  interval (skew-proof, no cross-clone clock comparison). Authoritative on `origin`
  via `git push --force-with-lease=<ref>:<old>`. (The legacy `file` backend — a
  committed tickets-branch `.reconciler-pass-lock` advanced by a `refs/heads/tickets`
  CAS — and the `[reconciler] lock_backend` selector key were removed pre-1.0; the ref
  backend is the only backend.) This is the one allowed cross-client lock — **not** a
  precedent for new ones. It keeps I6 cleaner: the lock is no longer a committed
  tickets-branch file needing a `merge=ours` union-merge carve-out.

### I7 — Derived/aggregate data is computed from replay or stored local-only
Search indexes, counters, memory stores, etc. are either recomputed from the
event log on demand or cached **local-and-rebuildable** (gitignored, uncommitted).

### I8 — Cross-client ordering is best-effort under clock skew; only STATUS fork resolution is skew-independent
Replay orders events by the `${timestamp}` filename prefix. With skewed client
clocks, COMMENT/EDIT interleaving across clients is best-effort. **STATUS forks
are resolved deterministically and skew-independently by the event's own UUID:**
the lexically-lower UUID wins (`reducer/_processors.py:81-115`,
`if not existing_uuid or incoming_uuid <= existing_uuid`). Any new
state-dependent merge logic MUST resolve forks by UUID (or another
skew-independent key), **never by timestamp alone**.

**Surfacing a resolved fork (story 3003).** A resolved STATUS fork means two clones
raced (e.g. both claimed the same open ticket) and one lost. This is now discoverable
rather than silent: the reducer records each resolution in pure derived state
(`status_fork_resolutions`, rebuilt identically on every replay), which `fsck` reports as
a `STATUS_FORK_RESOLVED` finding and `show`/`list` surface as a field. Separately, a
`claim` whose post-push merge reveals another clone already owns the ticket (the merged
`assignee` — the ownership authority — is not the claimant) exits **10** ("claim lost on
cross-clone merge") so the losing agent stops instead of duplicating work; when no merge
is visible at claim time, the durable `fsck`/`show` surfacing catches it after the fact.

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

**Push policy — `REBAR_SYNC_PUSH`** (read at the `_push_tickets_branch` chokepoint, so
CLI / library / MCP honour it uniformly; case/space-insensitive; default
`always`):

| value    | behaviour |
|----------|-----------|
| `always` | synchronous push before the write returns (default — real-time propagation is a first-order requirement). |
| `async`  | return immediately; the (identical, best-effort) push runs in a detached background job. Convergence is unchanged — `fsck` still reports `PUSH_PENDING` until it lands, and a non-fast-forward still fetches+merges+retries. Use when an agent claims a batch and per-write network latency would serialize the run. |
| `off`    | never push; commits stay local (`fsck` reports `PUSH_PENDING`). For offline/throwaway work. |

The failed-push resilience and non-fast-forward fetch+merge+retry behind these
modes are covered by
`tests/integration/test_concurrency_regression.py::test_failed_push_never_drops_local_commit`
and `tests/unit/test_push_retry_stash_pop.py`.

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
this. (This specific detached-HEAD-local-ahead edge is pinned by a dedicated
automated regression test,
`tests/unit/test_sync_union_recovery.py::test_sync_preserves_detached_head_local_ahead_commit`,
which drives the store into that state and asserts the un-pushed local commit
survives the sync.)

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
read tools all funnel through one implementation — `reads` in the engine-support
layer (`src/rebar/_engine_support/reads.py`), with `rebar/_reads.py` as the
library/MCP facade. `reads.ensure_fresh()` reuses the exact mechanism above:
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
honored by all interfaces; permanent alias `REBAR_NO_SYNC=1`) or pass the
`--no-pull` flag to any read subcommand (`rebar list --no-pull`). The reducer's
local `.cache.json` (I3/I3a) is still used; only the
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
