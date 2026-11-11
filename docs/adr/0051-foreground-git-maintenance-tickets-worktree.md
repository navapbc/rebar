# ADR 0051 — Foreground git maintenance on the tickets worktree (never detached)

**Status:** Accepted
**Date:** 2026-07-17
**Supersedes:** the `gc.autoDetach=true` decision from **epic 97e7 / P1.4 / WU-1** (which
had no ADR — it was recorded only in `docs/concurrency.md` and the WU commits). Corrects the
"stock `git gc` is safe by construction" claim in `docs/concurrency.md` and
`src/rebar/_store/sync.py`.
**Bug:** `88eb-2beb-65f5-4bc0` (`amicable-unsure-barasinga`).
**Related:** `4c1c` (the `_recover_from_invalid_object` index self-heal — a downstream
symptom), `ac26`, `4f8d`, `eclectic-spotted-barb`.

## Context

The tickets store is a **linked git worktree** (`.tickets-tracker`) that SHARES the parent
repo's object database. Many independent `rebar` processes append events to it concurrently;
each commits under a single cross-process write lock (invariant I5).

Epic 97e7 (WU-1) unset `gc.auto` and set `gc.autoDetach=true` to "trust stock `git gc`," on
the argument that union recovery (WU-2) keeps every commit ref-reachable, so "stock `git gc`
is safe by construction — it only ever collects truly unreachable objects."
`gc.autoDetach=true` was chosen **specifically** so "a forked background gc never serializes a
foreground ticket write."

That argument covers only **serial reachability**. It never accounted for a **concurrent**
background repack. On git ≥ 2.47, `git commit` triggers `git maintenance run --auto`, which —
when detached — forks `git repack --cruft --write-midx` into the **background, outside rebar's
write lock**. Under concurrent writes, several such detached processes run at once against the
shared object DB, racing the writers and each other, and corrupt it: `git fsck` reports
missing/dangling objects and hundreds of `tmp_pack_*` garbage files, and subsequent commits
fail `error: invalid object <sha> … error: Error building trees`, silently dropping the
writes. git's own documentation warns that a concurrent `git gc` "may corrupt the
repository," and that plain `git gc` "does not take the [object-db] lock in the same way as
`git maintenance run`."

This was the root cause of the chronic
`test_enrich_queue_prune_never_drops_concurrent_locked_writes` CI flake (bug 88eb). Key
findings from reproduction:

- **git-version-sensitive:** reproduced reliably on git 2.54, never on git 2.47.
- **rebar's own write lock is sound** (0 lock overlaps observed across 7,296 acquisitions);
  the corruption is 100% from git's detached maintenance escaping the lock, with the racing
  `git maintenance run --auto --detach → git repack --cruft` processes captured live.
- **`gc.auto=0` does NOT fix it** (8/20 → 5/20, within noise) because git ≥ 2.47 gates
  auto-maintenance on `maintenance.auto`/`maintenance.autoDetach`, not the legacy `gc.auto`.

Prior art confirms the pattern: git-gc/git-maintenance docs; the ArgoCD git-2.47
detached-maintenance corruption incident (#25101, same shape — background maintenance
escaping a lock on a shared git dir); GitLab/Gitaly (`gc.auto=0` + own housekeeping);
Gerrit/JGit (`receive.autogc=false`, after a real concurrent-gc corruption incident);
Homebrew (`gc.autoDetach=false`).

## Decision

Keep auto-gc **enabled** but force it to run in the **foreground** on the tickets worktree,
never detached. `init._gc_config_unit` sets, idempotently (check-then-act):

- `--unset gc.auto` — auto-gc stays at git's default threshold, so repack still fires and
  bounds loose-object growth (WU-1's goal is preserved);
- `gc.autoDetach=false`;
- `maintenance.autoDetach=false` — git ≥ 2.47 routes auto-maintenance through
  `git maintenance run --auto` and honors this knob; `gc.autoDetach` is only its fallback, so
  **both** must be false or the background repack still detaches.

Auto-maintenance is triggered by the write command (`git commit`) that **already holds
rebar's write lock**, so a foreground repack runs **serialized under the lock** — no
concurrent writer touches the shared object DB during it. This removes the race while keeping
automatic compaction.

## Consequences

**Correctness (the point).** No detached maintenance runs concurrently with a writer; the
shared object DB is never repacked out from under an in-flight commit. Confirmed: **0/20**
losses with the fix vs **8/20** without, on git 2.54.

**Performance — a bounded, occasional GLOBAL write pause.** Because the repack now runs while
the write lock is held, it blocks **all** writers of that store (not only the triggering one)
for its duration. Measured on git 2.54 for this store's data profile (small append-only JSON
events, ≈4 objects/commit):

| Loose objects at trigger | Foreground repack pause |
|---|---|
| ~2,000  | 0.16 s |
| ~10,000 | 2.0 s  |
| ~40,000 | ~8 s (≈0.2 ms/object) |

- Fires roughly every **~1,675 commits** (default `gc.auto` ≈ 6,700 loose objects); between
  triggers, writes are unaffected. **Reads never take the write lock and never trigger
  maintenance**, so they are never paused.
- The pause **grows with total store size** (a repack processes the whole store). The scaling
  ceiling is rebar's **60 s write-lock budget** (`_DEFAULT_TIMEOUT` × `_DEFAULT_ATTEMPTS`): a
  store would need ~300k+ objects for one foreground repack to risk `LockTimeout` on
  concurrent writers. Today's stores are far below that.

This is a good trade: a rare sub-second-to-few-second write pause in exchange for eliminating
silent data corruption.

**Rejected alternatives:**

- **`gc.auto=0` alone** — does not disable git ≥ 2.47's `git maintenance run --auto`; still
  corrupts.
- **Disable auto-maintenance entirely (`maintenance.auto=false`)** — safe, but regresses
  WU-1's bounded-growth (no automatic repack) unless paired with an explicit serialized gc.
- **Disable + serialized explicit `git maintenance run --task=gc` under the write lock** — the
  most robust option and git-upstream's steer (that command takes the object-db lock; the
  GitLab/Gitaly pattern). Not adopted now on cost/complexity grounds (extra code + a
  compaction cadence to own). It is the **escape hatch** if the foreground-pause ceiling is
  ever approached at scale.

**Follow-up.** If a store grows large enough that the foreground pause becomes disruptive
(approaching the lock-budget ceiling), migrate to disable-auto-maintenance + serialized
explicit gc on a controlled cadence (tracked on bug 88eb).

## Amendment (2026-11-11) — incremental explicit maintenance; no implicit maintenance in bounded children

**Bug:** `51e0-5038-bf00-4653` (`nickelic-lazuline-gonolek`).

The production MCP tickets clone went from 2026-09-03 to 2026-11-11 with no maintenance
(78,737 loose objects, 235 packs, history walks 4–8× slower). Three facts, measured on that
clone (1.07M objects, 307k commits, git 2.47.3):

- `git fetch` and `git merge` each start a foreground `git maintenance run --auto` before they
  exit. bd66 suppressed it only on store commits, so sync and push-recovery fetches (30 s bound)
  and merges (120 s bound) still started it. On this store that run is a full gc: a full repack
  took 605.6 s and `gc --auto` with 7,000 loose objects took 133.9 s. The bound killed the parent
  only, and the orphaned gc kept repacking **outside the write lock** (observed live: adopted by
  the container's PID 1 while another process held the write lock).
- A hard kill (container replacement) of such a run strands `objects/maintenance.lock`. Git never
  expires that lock, and `git maintenance run --auto` then exits 0 without doing anything, so
  rebar's explicit step became a silent no-op from 2026-09-03 on.
- `gc` walks reachability (repack and prune), so its cost grows with the store and cannot fit the
  explicit step's 120 s bound at this size.

The decision changes three things. The foreground-under-the-write-lock rule above is unchanged.

1. **Bounded git children never start implicit maintenance.** `gitutil.run_git_bounded` appends
   `maintenance.auto=false` and `gc.auto=0` to the child's `GIT_CONFIG_*` environment. The
   container's start-up `fetch` in `reviewbot-ensure-tickets.sh` passes the same settings. The
   store's only maintenance is the explicit step below.
2. **The explicit step runs git's incremental tasks**:
   `git maintenance run --auto --quiet --task=commit-graph --task=loose-objects
   --task=incremental-repack`. None of them walks reachability. On a copy of the production
   store, they took 0.35 s, and 17.3 s when incremental-repack fired. `--auto` keeps git's
   per-task thresholds. Unreachable objects are packed, not pruned. At this store's rate of
   about 1,800 unreachable objects in 2.5 months, the extra disk use is negligible.
3. **A timed-out step is reaped as a process group** (SIGTERM, then SIGKILL), so git deletes its
   own lock and temporary packs, and no repack outlives the write lock.

The remaining way to strand `maintenance.lock` is a hard kill that lands during the explicit
step itself, which takes milliseconds when there is nothing to do. Recovering from that requires
rebar to remove a git-owned lock. That is tracked separately as bug `saccharine-unwise-barnacle`
(`3e48-86e5-45a7-4acd`). The broader SIGKILL-only timeout on other bounded git children is bug
`idle-ammoniacal-axisdeer` (`4eed-2e15-d018-4bd6`). Epic `dynamic-curium-sponge` proposed this
task set earlier.
