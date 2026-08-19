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

### I4a — Parent-first claim/transition/reopen cascade
A child never runs ahead of its parent in the lifecycle: on a **cascading edge** the
parent is moved along the **same edge first** — recursively up the chain (top-most
eligible ancestor first) — before the child moves. There are exactly two cascading
edges, each with the parent status that is eligible on it:

| child edge | eligible parent | why |
|---|---|---|
| `open → in_progress` (`claim`, and the `transition`) | `open` | a descendant is never moved into progress while an ancestor is left merely `open` |
| `closed → open` (`reopen`, and the equivalent `transition`) | `closed` | a reopened descendant is never left under a still-`closed` ancestor |

A `claim` cascade carries the same assignee up the chain. A parent in any OTHER status
(or absent) is **not** cascaded — only the requested ticket moves. So a claim under an
already-`in_progress`/`closed`/`blocked` parent, and a reopen under an already-`open` or
`in_progress` parent, both leave the parent untouched.

The cascade is **sequential and fail-fast, not transactional:** the parent op runs
to completion (its own commit + push) first; **if it fails the child op is not
attempted**, and the failure is re-raised with a message naming the parent as the
cause (`cannot claim <child>: claiming its parent <parent> failed first …`) while
**preserving the parent failure's exit code** — so a parent concurrency conflict is
still **exit 10 / `ConcurrencyError`** at the leaf call. (There is intentionally no
rollback if the parent succeeds and the child then fails: an ancestor sitting in
`in_progress` is the conservative, harmless direction.) Recursion is cycle-guarded
(an id already on the cascade stack, including a self-parent, is skipped). Only the two
edges tabled above cascade — `* → closed` and `* → blocked` never do. Closing has its
own separate open-children guard, and that asymmetry is exactly why the `closed → open`
edge must cascade: the guard blocks CLOSING a parent that has open children but says
nothing about REOPENING a child, so without the cascade a `reopen` left the parent
closed — an invalid closed-parent-with-open-child state (bug `cranial-sulfur-peafowl`).
Implemented in `src/rebar/_commands/claim.py` (`claim_compute`) and
`src/rebar/_commands/transition.py` (`transition_compute` → `_cascade_parent_first`,
driven by the `_CASCADING_EDGES` table), via the shared `_resolve_parent_in_status`
helper (`_resolve_open_parent` is its `open` specialization, which `claim` uses).

#### Gate interaction (the plan-review claim gate)

The cascaded parent claim is a *full* claim — so it runs the parent's **own**
plan-review claim gate (`verify.require_plan_review_for_claim`) when that gate is
enabled. Epics and stories are **not** gate-exempt (only `bug` and `session_log`
are), so claiming a leaf task can be **blocked by the parent's missing/stale
attestation**, and the error names the **parent** as the cause. Earn the parent's
attestation (`rebar review-plan <parent>`) — or claim the parent yourself first —
before claiming the child, or pass `--force`, which propagates up the cascade and
bypasses the gate at **each** level with an audit note. Note that if the parent is
itself **not yet claimable** (its own prerequisites are still open), `review-plan
<parent>` fast-fails without running the LLM and cannot mint the attestation until
those prerequisites close — so settle them first (or `--force`). The same "the cascaded
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

**An expired budget is retried, not discarded (`retries`).** One acquisition *pass*
costs `timeout × attempts` seconds — note `attempts` multiplies a single deadline and
is **not** itself a retry loop. Historically a pass that expired raised `LockTimeout`
and the write was **thrown away**: measured against a clone of the live store, a 70s
holder plus three `rebar comment`s produced 3/3 failures with **none** of the three
comments present afterwards, and a `claim` behind a 45s holder died at exactly 30.30s
because it passes `attempts=1`.

`acquire()` / `write_lock()` therefore take a **`retries`** argument: after an expired
pass they sleep a jittered exponential backoff (0.5s, 1.0s, capped at 2.0s — reusing
`gitutil`'s `_jitter` / `_backoff_sleep` seams) and take another pass. The canonical
write path opts in with 2 retries, giving a ceiling of roughly **180s** (3 × 60s plus
backoff); holders measured at 88s, 103s and 163s all exceed one budget but fall inside
that ceiling, so they now cost latency instead of a lost write. When every pass is
spent the write still fails **loudly**, and the `LockTimeout` names the *cumulative*
wait so the message cannot understate how long the caller waited.

Retrying is safe — and cannot duplicate an append — only because it lives **inside**
`acquire()`: the caller's write body has not run when a pass expires, and once
`acquire()` returns the loop is finished and cannot re-enter. Retrying at any layer
*above* the lock would not have that property and could double-write.

`retries` defaults to **0**, so every call site that does not opt in keeps the exact
historical fail-fast behaviour. That default is load-bearing: compaction, `fsck`
repair, the S3 doctor, the reconciler pass and the best-effort sweeps (`sync`, the
advisory push merge, `ensures`) deliberately stay fail-fast. Giving compaction retries
in particular would undo its stand-aside and re-create the long holder that causes this
contention in the first place. The opted-in set is exactly `event_append` (the three
write-path sites), the `txn` critical section, and `push`'s locked commit-and-push.

Set **`REBAR_LOCK_RETRIES`** to override the opted-in count (default `2`, clamped to
`[0, 10]`; an unparseable value falls back to the default rather than breaking every
write). `REBAR_LOCK_RETRIES=0` restores the historical single-budget fail-fast for CI
or ops that prefer to fail immediately over waiting.

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

### I9b — Compaction runs out of band, never on the close path
Compaction is the store's longest lock holder, and it is **optional housekeeping** — an
unfolded event log is completely valid, and the reducer replays it. So it must never sit in
the path of an interactive command.

Closing a ticket used to run it inline (`_compact_on_close`). That held the ONE store write
lock for the whole fold — read, reduce, authorship ledger, snapshot write, retire renames, and
the git `add`/`commit`, whose nested `_store_git_op_lock` wait and index-lock retry budget
stack *inside* that hold with no aggregate ceiling. Measured on the rebar store, one close
held the lock for **13m53s** and three others the same hour held ~2.5 min each; every
concurrent writer burned its acquire budget and lost writes. The stand-aside probe added
earlier could not help, because the closing process had released the lock seconds before its
own probe, so the store always read free.

Today:

- **A close never compacts.** It ends after the STATUS write, signing, the force-close audit
  comment and scratch cleanup. Its lock holds are the short per-append acquisitions.
- **`rebar compact <id>` still folds on demand**, unchanged.
- **A close TRIGGERS compaction without performing it** — the floor for stores with no CI and
  no cron. Compaction has to work in environments without either; that is why the original
  design was linked to an operation, and a schedule alone would leave a library/CLI adopter
  with no trigger at all. So at the very end of a close — *after* the locked write released the
  store lock and after the best-effort push — two `O(1)` checks run: does the ticket just
  written satisfy the two-arm selection (one directory read, independent of store size), or is
  the last-sweep stamp older than `compact.trigger_interval_s`? If either fires, a **detached
  worker** runs the same `compact-all` sweep out of band and the session returns immediately.
  One worker at a time, arbitrated by a stamped advisory lock in `.rebar/` that reuses the
  store lock's v2 ownership stamp and `lock_owner.stamped_file_is_stale`, so an orphaned lock
  is reclaimed rather than disabling the trigger forever. `compact.trigger` selects
  `async` (detach, the default), `always` (inline — for tests/CI) or `off`. Windows is a v1
  no-op, as the enrichment drain is.

  This does **not** reintroduce the hold that moved compaction out: the P0 was never "a close
  compacts", it was that an operation agents *wait on* held the store lock for minutes. Here
  the waited-on operation holds nothing and waits for nothing; the worker's lock profile is
  that of a manual `rebar compact`.
- **The standing trigger is `rebar compact-all`, run out of band on a schedule.** The
  `Compaction Sweep` workflow (`.github/workflows/compact-sweep.yml`) runs it **every 6
  hours** (`cron: '0 */6 * * *'`, also `workflow_dispatch`-able with `dry_run` / `limit`), on
  a disposable CI runner. The isolation that matters is that the runner is a **separate
  checkout on a separate machine** — a fresh clone per run, with the store mounted as a
  `tickets` worktree of it — so it shares no `index.lock`, no `rebar-git-op.lock` and no store
  write lock with any interactive session. (Within the runner the mounted worktree shares that
  checkout's object store, as any worktree does; that is irrelevant here because nothing else
  on the runner touches the store.) The result reaches everyone through the ordinary push/merge path, which
  is safe by I9: a fold adds a SNAPSHOT and renames sources to `*.retired`, and concurrent
  sessions only add new event files, so the two merge as a union. There is **no long-lived
  clone to maintain** — each run re-derives the store from `origin/tickets`, and a failed push
  leaves only local commits on a workspace that is discarded.

  `compact-all` selects a ticket on **either** of two arms:

  1. **Backfill** — it has no SNAPSHOT yet and has at least one foldable event. Every ticket
     still earns its first SNAPSHOT regardless of size (the historical rule, preserved).
  2. **Recurrence** — its **foldable** event count exceeds `compact.threshold`, whatever its
     snapshot state.

  Arm 2 is what makes the sweep RECUR. Selecting on arm 1 alone made `compact-all` a one-time
  operation: a ticket folded once and since grown had a SNAPSHOT, so it was never folded
  again. Selecting on arm 2 alone would regress the other way — a ticket with fewer events
  than the threshold would never get a first SNAPSHOT at all. Both arms converge: after a
  fold the ticket has a SNAPSHOT and its live count is back under the threshold.

  "Foldable" means older than the compaction horizon, decided by the **same** `is_foldable`
  predicate the fold itself partitions on. Counting merely *live* events would select tickets
  whose excess events are all inside the horizon, fold nothing, and select them again forever
  — so selection and the fold ask one question through one predicate, and cannot drift.

The rule this generalizes: **any new long-running store maintenance belongs out of band, in
its own clone — not bolted onto a command a person or agent is waiting on.**

### I9a — Creation-channel provenance across a full downgrade (pause `compact`)
`creation_channel` / `creation_channel_inferred` are additive genesis fields (see
[event-schema.md](event-schema.md)). They ride the CREATE `data` and, after compaction,
`SNAPSHOT.data.compiled_state`. A **full downgrade** — running a rebar binary that predates
the field over a store that already has it — is safe for reads but has one durable-loss edge,
because the old binary's reducer does not project the field. During the downgrade window a
ticket sits in one of **three states**:

| Ticket state during the downgrade window | Provenance on disk | Recovers on upgrade? |
|------------------------------------------|--------------------|----------------------|
| **New-code `SNAPSHOT` replayed by old code** | **Retained** — the keys live in `compiled_state`, and old `process_snapshot` restores it with a generic `for key,value` loop, so the fields survive even though the old reducer never names them. | Yes — verbatim. |
| **Never-compacted ticket** (active CREATE) | **Retained** — the value stays in the active CREATE `data`; the old reducer simply ignores the key. | Yes — replaying the active CREATE re-projects it. |
| **First OLD-code compaction of a raw, creation-bearing CREATE** | **Dropped from the new SNAPSHOT's `compiled_state`** (the old reducer built it without the field) — **but the CREATE is retained as a `.retired` source** (invariant I1), so the raw genesis data is never lost. | Yes — via full-log rebuild. |

Only the **third** state loses the field from the *durable* SNAPSHOT, and compaction is the
**only** operation that can produce it. Therefore:

- **STOP every `rebar compact` invocation for the entire downgrade window.** Pause scheduled
  compaction and `compact` / `compact-all` until every clone is back on a binary that
  understands the field. (Closing a ticket does **not** compact it — see
  "Compaction runs out of band" below — so no close can enter state 3.) With `compact`
  paused, no ticket can enter state 3, so the downgrade is fully lossless (states 1–2 retain
  the value).
- **If the pause is violated, capture the affected IDs.** The tickets compacted during the
  window are exactly the ones whose SNAPSHOT `compiled_state` now lacks `creation_channel`
  while a retained `.retired` CREATE still exists — record them from the compaction commit
  range (the `ticket: COMPACT <id>` / `REBUILD SNAPSHOT <id>` commit messages on the `tickets`
  branch) as you find them. After upgrading, `rebar fsck` surfaces the same set as
  `SNAPSHOT_STALE_CHANNEL` findings.
- **Recover with a full-log rebuild.** `rebar fsck --repair-snapshots` rebuilds each flagged
  snapshot via `rebuild_snapshot_from_full_log`, which reduces with `include_retired=True` — it
  replays the retained `.retired` CREATE and re-projects `creation_channel` (including the
  legacy-Jira `jira` + `inferred` inference) into a refreshed SNAPSHOT. Reads are correct even
  before the rebuild: `process_snapshot` re-infers a channel-less snapshot at restore time
  (story 568c); the rebuild just persists it durably.

---

## The sync / reconvergence algorithm

Two paths move commits between clones; **both reconverge by MERGE-as-union, never
rebase** (bug 637b: an interrupted rebase strands picks as dangling commits, and
compaction `*.retired` renames conflict under rebase where merge unions cleanly).

### Outbound — push (on every write)

> **Never let the `tickets` branch trigger your CI.** Because every write auto-pushes,
> a CI system that watches all branches turns each comment, claim, and close into a
> pipeline run — and a busy day of ticket activity can saturate a shared runner pool
> and starve the builds that actually gate your merges. The branch carries an event log,
> not code, so there is nothing on it for a build to compile or test. Configure your CI
> so pushes to `tickets` (see `sync.remote` in
> [config.md](config.md#config-key-inventory)) match no workflow: on GitHub Actions add the
> branch to each workflow's `on.push.branches-ignore` (or use an explicit `branches:`
> allow-list that omits it); other systems have an equivalent branch filter. If a job
> genuinely needs to read the store — a reconciler, an audit, a metrics roll-up — run it
> on a **schedule** and let it fetch the branch, rather than on a push trigger. rebar's
> own repository does exactly this, and pins it with a test that enumerates every
> workflow file so a newly added one cannot reintroduce the trigger.

**Every** rebar write (`create`/`edit`/`transition`/`claim`/`link`/…) auto-commits
its event and then auto-pushes — so local ticket activity (including test/scratch
tickets) propagates to the shared `origin/tickets` **immediately**, with no
separate push step. `_push_tickets_branch` (`ticket-lib.sh:482`) pushes
`HEAD:tickets` whenever an `origin` remote exists (no remote → it is a no-op and
nothing is shared). On a non-fast-forward rejection it **fetches + merges**
`origin/tickets` (union) and retries (bounded). It refuses to merge through a
rebase/merge recovery state (`_check_no_rebase_in_progress`, `ticket-lib.sh:217`).
Push is **best-effort by default**: a failed push (no network, unresolvable
non-fast-forward, recovery state) never fails existing callers — it warns, leaves
local commits intact, and the branch stays diverged. `rebar fsck` surfaces that
divergence as a `PUSH_PENDING` notice (`ticket-fsck.sh`, Check 4.5) so it is not
silent. Existing callers inherit five push-first recovery cycles and, after a
clean fifth merge, one final push; they otherwise keep their prior warning/return
behavior.

`push_tickets_branch(..., strict=True)` is the opt-in delivery contract. It raises
`PushDeliveryError` rather than writing process output, with a stable `reason`,
detail, and the current unpushed-commit count. Reasons are: `push-disabled`,
`async-delivery-unobservable`, `invalid-destination`, `remote-not-found`,
`push-policy-declined`, `push-transport-failed`, `merge-recovery-blocked`,
`store-epoch-pre-merge`, `store-epoch-during-recovery`, `final-push-rejected`,
and `lock-timeout`. Strict delivery rejects the `off` and `async` policies because
they cannot prove synchronous delivery. The private process boundary
`python -m rebar._store.push push --tracker <path> [--strict]` catches that error,
reports it on stderr, and returns a nonzero status.

#### The push-pending marker — how a failure reaches a caller that cannot see stderr

The warning above is only DELIVERABLE to a caller watching this process's stderr. Three
supported surfaces are not: a `sync.push = async` push runs in a detached child whose
stderr is `/dev/null`; a **library** embedder gets rebar's `NullHandler`; and an **MCP**
client reads only the tool result. On those paths the warning was not merely
uninformative, it never arrived at all (bug `vapoury-attack-lamb`).

So every terminal delivery failure is ALSO recorded as durable state: `rebar-push-pending`
in the tracker's **git dir**, written by `rebar._store.push_state`. It records the
classification `reason`, the git rejection `detail`, the `remote_ref`, the `unpushed`
backlog count and `since`.

The git dir, not the working tree, is load-bearing: it puts the marker outside everything
git sees, so it can never be committed (a record that the remote is unreachable must not
itself need the remote), it cannot perturb the stash-aside/merge/restore dance a
non-fast-forward triggers, and it never appears as an untracked file in `git status`. It is
cleared by the next push that lands, so the signal cannot latch on past the outage it
describes.

Read it with `rebar.push_status()` from the library (no logging handler required), or from
the `push_status` field every MCP write tool now returns. The four reasons that mean *no
push was attempted* — `push-disabled`, `async-delivery-unobservable`, `remote-not-found`
and `invalid-destination` — are deliberately NOT recorded, so a local-only store (a
supported mode) never reports a phantom outage.

On the `async` policy the marker is written by the detached CHILD, so a failure can land
after the parent's call returns. The guarantee is that the failure becomes visible to a
subsequent write or read, not that it is visible within the same call.

This is a SIGNAL, never an exception: the best-effort contract above is unchanged, and a
marker that cannot be written (an unwritable tracker) degrades to "no status" rather than
failing the write.

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

> **INVARIANT (serial gc only).** After union recovery, every commit rebar cares
> about is ref-reachable from the `tickets` branch; therefore a **serially-run**
> `git gc` is safe by construction — it only ever collects truly *unreachable*
> objects. This says nothing about a **concurrent** gc (see the caveat below).

This is jujutsu's "gc-reachability == recovery guarantee" co-design, achieved for
free: if commits are never orphaned, a serial gc has nothing unsafe to collect. So
rebar no longer needs `gc.auto=0` to protect the reflog.

> **CAVEAT — concurrent background gc is NOT safe (bug 88eb / ADR 0051).** The tickets
> store is a **linked worktree sharing the object DB**, written concurrently by many
> processes. A *detached* auto-gc — `gc.autoDetach=true`, or git ≥ 2.47's
> `git maintenance run --auto` — repacks that shared object DB in the **background,
> outside the write lock**, racing in-flight writers and corrupting the store
> (`invalid object` / `Error building trees` → dropped writes; git's own docs warn a
> concurrent `git gc` "may corrupt the repository"). WU-1's original
> `gc.autoDetach=true` — chosen precisely so gc "never serializes a foreground write" —
> was therefore the bug, not the fix. The `gc-config` ensure unit now keeps auto-gc
> ENABLED but forces it **FOREGROUND** (`--unset gc.auto` + `gc.autoDetach=false` +
> `maintenance.autoDetach=false`): a triggered repack runs synchronously inside the
> lock-holding `git commit`, i.e. **serialized under the write lock**, so no writer
> races it. It reclaims loose/pack growth on its own, at the cost of an occasional
> brief global write pause when it fires (see Scale-up posture; ADR 0051). The two
union merges can in principle conflict only on the **shared mutable root files**
(`.bridge_state/bindings.json`, the `.reconciler-*` lock/gate files), which the
tickets-branch `.gitattributes` resolves `merge=ours` (they are per-pass derived
caches the reconciler rebuilds, never ticket events; `merge=union` would line-union
JSON into invalid JSON). UUID-named ticket-event files never collide. A genuine
conflict still aborts → keeps local → hints `fsck` (never a hard read failure).

**Scale-up posture.** Auto-gc's default cadence (`gc.auto`, ~6700 loose objects)
suffices for normal stores. Because the repack now runs **foreground under the write
lock** (ADR 0051), each firing is a brief GLOBAL write pause for that store — measured
~0.16 s at 2k objects, ~2 s at 10k, growing with total store size. It fires roughly
every ~1,700 commits; reads are never paused. The scaling ceiling is the 60 s
write-lock budget: a store nearing ~300k+ objects (where a single foreground repack
could approach that budget) should migrate to **disabled auto-maintenance +
`git maintenance run --task=gc` serialized under the write lock out of band** (the
git-upstream / GitLab-Gitaly pattern; the ADR 0051 escape hatch). Git's own ~30-day
unreachable-reflog window remains a free backstop — but rebar no longer *depends* on it
for correctness.

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
(story 23d2-e0f3 Rec 2) moved
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

## Staging a new ticket: `.tmp-newticket-*` (the atomic-create convention)

A ticket directory and its first event are published **together, by one `os.rename`**, which
is atomic on a single filesystem. The writer builds both inside a staging path
`<tracker>/.tmp-newticket-<pid>-<uuid4hex>` and renames that directory into the ticket-id
path (`src/rebar/_store/staging.py`). Previously the directory was created first and its
CREATE event landed much later, so an interruption in between (host sleep, kill, lock
timeout) stranded an empty, plausible-looking ticket directory — which `fsck` reports twice,
as `MISSING_CREATE` **and** `FOREIGN_STORE_PATH`. Eight such directories were swept by hand
before the fix (ticket `illsuited-erect-ibis`).

**The leading dot is load-bearing.** Every store scanner already skips top-level entries
beginning with `.` — `fsck._ticket_dirs`, `fsck.foreign_store_path_list`, fsck's JSON check,
and the plan reducer's `relation_snapshot` — and ticket ids never start with a dot, so the
two namespaces cannot collide. A staging path is therefore invisible to both checks with no
scanner change, exactly as the older `.tmp-event-*` event-staging files are. The pid and
uuid4 make each staging name unique per writer and per call, so concurrent creates cannot
collide. The rename itself still happens **under the write lock**, so an event never becomes
visible before the under-lock checks (rebase guard, optimistic-concurrency check) pass.

A bounded, best-effort sweep at ticket-create writer start reclaims staging paths whose
owning process is provably gone, using the same host + pid-namespace + process-start-time
ownership stamp the write lock uses (so pid recycling cannot fool it). **This does not
contradict "tolerate, never tidy"** (bug `043f`): that ruling forbids the writer from
deleting *store data* — an event-less ticket directory — because doing so races another
session's in-flight write, and because reader-side tolerance also repairs clones that
already carry debris. The sweep touches only `.tmp-newticket-*`, the writer's own staging
area, never a ticket directory. Event-less ticket directories are still tolerated and never
tidied.

## Doctrine compliance is a gate

A change that cannot satisfy I1–I9 is **redesigned, not merged**. The executable
form of this doctrine is `tests/integration/test_concurrency_regression.py`: two
clones writing disjoint and overlapping events, reconverging by fetch/merge, and
asserting union + one deterministic replayed state on both clones + identical
UUID-based fork resolution. Every write/sync change runs against it.

## Mutating the tracker: no AD-HOC raw git

The ticket store is event-sourced and has its own API. **Route every routine write through
rebar** — `create` / `comment` / `transition` / `link`, or, when you genuinely need to
commit and deliver pending tracker content yourself,
`rebar._store.push.commit_and_push_tickets_branch`, which does it under the write lock.

### The two-part tickets-branch mutation invariant

**EVERY mutation of tickets-branch state must (1) run under the unified write lock AND
(2) auto-commit + auto-publish it.** The two halves are not separable: taking the lock
without publishing leaves durable state stranded in the worktree (and forces the ad-hoc
hand-commits this section exists to forbid), while publishing without the lock races a
concurrent writer into a lost update. Both come from the shared seams, used as-is — never
hand-rolled:

- `rebar._store.lock.write_lock(tracker)` — hold it around the read-modify-write of the
  on-disk state (the `atomic_write` itself must land inside the lock's critical section).
- `rebar._store.push.commit_and_push_tickets_branch(tracker, message=…)` — commit all
  pending tracker changes under the same lock (+ rebase guard), then push.

This applies to sidecar state that lives on the tickets branch beside the event log too,
not only the event log. The `.bridge_state/projects.json` projects mapping is the worked
example: its mutators (`bridge_projects_set` / `bridge_projects_remove`) perform the
read-modify-write under `write_lock` via `fsutil.atomic_write`, then publish through
`commit_and_push_tickets_branch` — no bespoke lock, tempfile dance, or `git` invocation.

The rule is **no _ad-hoc_ raw git in the tracker**, not "no raw git ever". That distinction
is deliberate. A blanket prohibition with no sanctioned door is what produced bug `2fa6`:
the store was wedged, every rebar write failed, and improvised `git add -A` / `commit` /
`merge` / `rm` in the tracker worktree was the only way out. Improvisation is also how
source files reach the tickets branch, which `origin/tickets` legitimately never carries.

### Why `git stash` in particular is banned there

git's stash stack is **repo-global**: every worktree of a repository pushes onto and pops
from the same `refs/stash`. A `stash push` / `stash pop` pair executed in the tickets
worktree can therefore apply an entry created on a *source* branch. That is not
theoretical — it dropped `src/…` and `.rebar/…` into the store, left the index with
unmerged entries and no `MERGE_HEAD`, and blocked every ticket write until a human
intervened.

rebar's own push recovery no longer does this: it records the dirty tree with
`git stash create`, which writes a stash **commit object** and returns its sha without
touching `refs/stash`, and restores it with `git stash apply <sha>`. A commit named by sha
is unreachable from another worktree's pop. Hold the same line in anything you write.

### The supported door — `rebar tracker-maintenance`

For a store that rebar itself cannot write, use the maintenance entrypoint rather than
improvising:

```sh
rebar tracker-maintenance            # --status (default): report; makes NO writes
rebar tracker-maintenance --clean    # repair, inside the safety envelope
```

Its value is the envelope, not the repair:

- **a backup ref before the first write** — `refs/rebar-maintenance/<utc>` is created at
  the current HEAD before anything is mutated, and printed with its rollback command. (Its
  predecessor stamped a `pre-a3-remediation` tag *after* two of four batches had already
  run, which made it useless as a rollback point.)
- **a refusal when unpushed ticket commits exist** — `rev-list origin/tickets..HEAD` being
  non-empty is the one condition separating a recoverable local mess from real event loss.
  A refused run makes **no** writes at all, backup ref included. It fails *closed*: if
  `origin/tickets` is missing, local commits cannot be proven safe, so it refuses then too.
- **a durable audit record** — what ran, when, by whom, what changed, and whether the
  break-glass was used, appended to the tracker's git dir (not the worktree, so it can
  never become store content that a later merge conflicts on).

### The break-glass

`--force=<reason>` overrides the unpushed-commits refusal for the case the envelope does
not cover. It **requires a written reason**, is reported loudly on stderr, and is recorded
in the audit line as `forced: true` alongside the reason, so a reviewer can see later that
it was used and why. Treat it exactly as AGENTS.md treats `--force` on the claim/close
gates: an escape hatch for a human operator's judgment call, not a routine agent move.

`fsck` reports the condition independently — a `FOREIGN_STORE_PATH` finding means
something wrote source paths into the tracker, and is a counted integrity issue rather
than a warning, because a healthy store never has them.

## Re-cloning the tracker: carry the git-ignored local state over

A fresh clone of the `tickets` branch is **not** a working store. Four files are
git-ignored, live only on disk, and are never in any clone:

| File | What it is | What breaks without it |
|---|---|---|
| `.env-id` | this environment's identity, **stamped into every event** and the `principal` of every op-cert attestation | a new identity is minted; every existing attestation becomes unverifiable here (`foreign_key` at the claim/close gates) and must be re-earned |
| `.opcert-key` | the op-cert signing key (mode `0600`) | nothing verifies, even with the matching `.env-id` — the signature was made by the missing key |
| `.opcert-key.pub` | the op-cert public key the verifier reads (or re-derives from the private key) | verification cannot find a key to check against |
| `.ensure-applied` | the ensure-registry marker | harmless: every unit simply re-runs and re-converges |

Copy all four out of the old tracker **before** you replace it.

The first row is the one that bites, because losing it fails **silently and late**. rebar
therefore never mints an identity into a populated store *quietly*: when `.env-id` is
absent and the store already holds events from another environment, the new id is written
and a warning naming that environment, the attestation consequence, and this carry-over
list is printed to stderr.

It **warns rather than refuses** because the store cannot tell the two cases apart, and
only one of them is a fault:

- **A second clone collaborating on a shared tickets branch** legitimately needs its own
  identity. It simply starts with no attestations of its own. Refusing the mint here would
  break a first-class workflow.
- **A re-clone of a tracker you were already working in** has just orphaned that
  environment's attestations. Recover by copying the four files above out of the old
  tracker and re-running the command — but only if the old tracker still exists. Restoring
  `.env-id` alone does **not** help: the signature was made by `.opcert-key`, so without
  the key nothing it signed can ever be verified again.

Set `REBAR_ALLOW_ENV_REIDENTIFY=1` to acknowledge the situation and quieten the warning.

`fsck` reports the condition independently and store-wide: an `ENV_ID_MISMATCH` finding
means **this environment's own author(s) have also written events under a different env
id** — a re-clone, or the same person moving machines. It is deliberately scoped by
author rather than simply "the store holds more than one env id", because a healthy store
shared by several clones always holds several; a check that fired there would fire on
every well-formed team store until nobody read it. It is a counted integrity issue,
because the alternative is discovering the loss one ticket at a time at a gate, hours
later. Prior events are **not** rewritten — they are correctly stamped with whichever
environment actually wrote them.

Making an attestation from a previously-trusted environment survive a re-clone would mean
publishing each environment's op-cert **public** key into the store, the way author
identities already are. That is a real trust-model change (local same-environment
certification becomes a small federated trust root) and needs its own ADR; it is
deliberately not what the warning above does.
