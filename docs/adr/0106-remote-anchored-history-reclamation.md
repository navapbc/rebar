# ADR 0106: Remote-anchored below-horizon history reclamation for the `tickets` branch

- **Status:** Accepted
- **Context:** Store-growth program (Option A), epic *polite-antivirus-bedbug*
  (`536b-8930-b922-4063`). This ADR is the **causal-stability contract** that the
  reclamation tooling (stories S1–S3) implements; it does not itself mutate the store.

## Context

The `tickets` branch is an event-sourced, git-backed store that **auto-commits and
auto-pushes** every write to the `sync.remote` (`docs/concurrency.md` §I5, §I9). The CI
gate **Verify Authorship Identity** does a *blobless* full-history checkout of that branch
to re-verify every mutating event's signature against commit-ancestry-scoped keyrings, and
fails the build when the checkout pack exceeds a deliberate budget
(`REBAR_CHECKOUT_PACK_LIMIT_KIB`, commit `16a1a385`, ticket `037b`: "executable pack-size
budgets" — an intentional growth canary).

That pack is **~481 MiB against a 100 MiB budget**, and it is ~90% tree objects, ~10%
commit objects, and **zero blobs** (blobless clone). Two facts follow:

1. **`git gc`/`repack` cannot shrink it.** A prior investigation proved an aggressive
   repack *grew* it to 558 MiB: a blobless tree-heavy pack has no blob delta-bases to
   recover, so recompression is a loss.
2. **Compaction alone cannot shrink it.** Compaction (`docs/concurrency.md` §I1, §I9;
   `src/rebar/_commands/compact*.py`) folds an event log into a `SNAPSHOT` and renames the
   folded sources to `*.retired`. But `SNAPSHOT` and `.retired` files are **blobs**, and the
   guarded pack is **blobless** — so folding and even a full `.retired` garbage-collection
   sweep leave every historical **commit and tree** reachable, and remove **~0** from the
   guarded pack.

The only lever that reduces a blobless tree-heavy pack is **reducing the count of reachable
commit and tree objects** in the `tickets` history — i.e. collapsing old commits/trees. That
is a **history rewrite of a shared, auto-pushed branch**: high blast radius, and blocked
until now by the absence of the causal-stability proof this ADR supplies. Scoping the CI
checkout to avoid the cost is explicitly rejected — the gate genuinely needs full ancestry
for signature verification, and narrowing it would silence the canary.

## Decision

Adopt **remote-anchored below-horizon history reclamation**: an **offline, re-signable**
rewrite that collapses `tickets` commits/trees older than an **N-day horizon** into a single
parentless **checkpoint** commit, published only after the checkpoint has been
**remote-reachable for ≥ N days**, with a **stale-clone forced-override** reconnect protocol.
Retired-event GC is a *secondary companion* to this, never the pack lever.

### 1. Horizon (portable, config-driven)

A new config key **`[reclaim].horizon_days`** (default **30**; env
`REBAR_RECLAIM_HORIZON_DAYS`) sets the reclamation horizon. It is distinct from — and must be
**≥** — the compaction horizon (`compact.COMPACTION_HORIZON_NS`): reclamation may only drop
commits whose events are already **folded** by a committed SNAPSHOT. The horizon is a plain
duration in config, with **no dependency on any single CI provider** — the operation runs
from the library/CLI on any host (the `project.portability` criterion). CI scheduling is one
*trigger*, not a requirement: an operator with no CI can run it by hand or by cron, exactly as
`compact-all` already degrades (`docs/concurrency.md` §I9b).

**Rationale for the 30d default.** The horizon is the maximum time a clone may stay
disconnected and still fast-forward onto the rewritten graph without discarding local work
(§5). 30 days comfortably exceeds the auto-push cadence of any active clone (every write
pushes, §I5), covers a multi-week vacation/offline gap for a human-operated clone, and is a
round, auditable window; it is deliberately conservative because the *cost* of a larger
horizon is only a slightly larger retained pack, whereas the cost of too small a horizon is a
stale clone losing work. Operators with a tighter convergence guarantee (e.g. a CI-only store
whose every clone is a fresh disposable checkout) may lower it via `[reclaim].horizon_days`.

### 2. Remote-reachability anchor

A collapse **checkpoint** commit is *eligible to become the new root* only once it has been
**reachable from the sync remote's `tickets` ref for ≥ N days**. This is the convergence
guarantee: every live clone auto-fetches the shared branch (§I5), so N days of remote
reachability means every non-stale clone has had the full horizon to converge onto the
checkpoint before its pre-image commits are dropped. The eligibility test is a pure
git-reachability + committer-date computation on the remote ref — no CI provider, no wall
clock beyond the commit dates already in the graph.

### 3. The collapse (pure core-git, portable)

Performed on a **throwaway shadow clone**, never in place:

1. Assert the working repo is a disposable shadow clone (safety refusal otherwise).
2. Find the **boundary** commit: the newest commit with committer-date ≤ `now − N days` whose
   events are all folded (SNAPSHOT-covered).
3. `git commit-tree boundary^{tree}` with **no parent** → the parentless **checkpoint**
   carrying the boundary's *exact* tree (identical observable store state).
4. Replay `boundary..tip` with `git commit-tree`, re-parenting each commit onto the rewritten
   graph and **preserving author, committer, message, and timestamps** (so above-horizon
   authorship is byte-identical).
5. `update-ref refs/heads/tickets` to the rewritten tip.

Uses only core-git plumbing (`commit-tree`, `mktree`, `fast-import`) — **not** third-party
`git filter-repo`, which is not vendored and would break portability.

### 4. Signature / authorship verifiability is preserved, not broken

The verify-identity gate re-verifies **every mutating event's signature** against a
commit-ancestry-scoped keyring. Reclamation preserves this two ways, and MUST satisfy at
least one for every below-horizon event:

- **Ledger carriage.** The SNAPSHOT that folds an event already records, per event, the
  `authorship_ledger` entry `{event_uuid, content_hash, signature, signer_pubkey,
  position:{commit_sha, position}}` (`_build_authorship_ledger`,
  `src/rebar/_commands/compact_txn.py`). The `position` string `{timestamp}-{uuid}` is
  **invariant across the rewrite**; only `commit_sha` changes. So a below-horizon event's
  signature remains verifiable **content-addressably** from the checkpoint's SNAPSHOT even
  though its original commit is gone — re-resolve the `commit_sha` via
  `authorship_resolution.build_position_commit_map` on the rewritten graph.
- **Enforce-since re-anchor.** The identity `[identity].enforce_since` boundary is re-anchored
  to the checkpoint commit, **grandfathering** all below-horizon events (they precede the
  enforcement boundary and are verified via the carried ledger, not a live keyring walk).

Both are applied together in practice: the checkpoint carries the ledger **and**
`enforce_since` is advanced to the checkpoint, so a verifier never needs a dropped commit.

### 5. Stale-clone forced override (reconnect protocol)

A clone whose **last convergence predates the horizon** ("stale") cannot fast-forward or
rebase onto the rewritten graph — its local `tickets` still roots in dropped commits. On
reconnect it MUST **override local with remote** (forced fetch + reset to the anchor) rather
than reintroducing pre-collapse history by rebase. This is detected by comparing the clone's
`tickets` merge-base against the checkpoint: no common ancestor within the retained graph ⇒
stale ⇒ forced override. **Accepted residual:** a stale clone's *un-pushed divergent local
work is discarded* — it had ≥ N days (the whole horizon plus the remote-reachability window)
to push, and the auto-push design (§I5) makes un-pushed multi-week-old local writes a
pathological case, not a supported one.

### 6. Publish-swap must bypass the S3 auto-doctor

For the `git-remote-s3` backend (ADR 0093), the non-interactive **auto-doctor**
(`src/rebar/_store/s3_doctor.py`) folds divergent heads by lossless 2-parent merges and
**never discards a head** — so a *normally-pushed* rewrite would be **merged back** and the
pre-image resurrected. Reclamation on an S3-backed store MUST therefore be an **exclusive,
push-locked, bundle-set replacement** that bypasses the auto-doctor (there is no S3 ref CAS).
For a plain git remote, the swap is `--force-with-lease` against the anchor. Both are
implemented as publish adapters behind one interface (story S2).

## The C1–C5 causal-stability boundary

An object is **safe to reclaim** iff **all** of C1–C5 hold; anything failing any one is **not**
reclaimable.

- **C1 — Folded.** The event's effect is folded by a **committed SNAPSHOT**; replaying the
  SNAPSHOT reproduces the state without the source event (§I1, §I9).
- **C2 — Below horizon.** The carrying commit's committer-date ≤ `now − reclaim.horizon_days`.
- **C3 — Remote-anchored.** The collapse **checkpoint has been reachable from the sync-remote
  `tickets` ref for ≥ N days** — every live clone has converged, or is declared **stale** and
  will force-override (§5). *This is the guarantee that no reachable-elsewhere object is
  dropped.*
- **C4 — Verifiability carried.** The below-horizon events' signatures are carried in the
  checkpoint `authorship_ledger` **or** grandfathered by a re-anchored `enforce_since` (§4) —
  verify-identity stays clean.
- **C5 — No forward dependency.** No **retained** (above-horizon) event or SNAPSHOT references
  a below-horizon `commit_sha` that would dangle. Where an above-horizon SNAPSHOT **embeds** a
  below-horizon `commit_sha` (its `authorship_ledger` `position.commit_sha`), that reference is
  **rewritten during collapse** — this is the interleaved reconciliation core (story S1), not a
  sequential post-pass.

**NOT reclaimable:** anything above the horizon; any commit an **unconverged, non-stale** clone
still needs (fails C3); any event whose signature cannot be carried or re-anchored (fails C4);
any commit still referenced by retained state after reconciliation (fails C5).

## Invariant proof (I1–I9 hold under reclamation)

- **I1 (Append-only).** Reclamation is the compaction exception (`docs/concurrency.md` §I1)
  extended branch-wide: it runs **offline under an exclusive publish lock**, and the
  *observable* folded state is byte-preserved by the checkpoint tree (step 3.3). No live event
  file is modified or deleted; only the **carrying commits/trees** are rewritten. The swap is
  atomic at publish (ref CAS / bundle replacement).
- **I2 (Globally-unique event filenames).** Event files are unchanged in name and content;
  only the commits that carry them change. Uniqueness is untouched.
- **I3 (Reads are side-effect-free, caches rebuildable).** The reducer replays the retained
  active events + SNAPSHOTs; caches are rederived. The rewritten graph yields identical replay
  output, so read caches rebuild identically.
- **I4 / I4a (Optimistic concurrency; parent-first cascade).** Optimistic concurrency keys on
  event **positions** (`{timestamp}-{uuid}`), which are **invariant across the rewrite**.
  A CAS that passed before passes after; the cascade order is unchanged.
- **I5 (Single locked write path).** Reclamation does not append on the interactive write
  path; it publishes out of band and reaches clients through the ordinary fetch/merge path
  (like `compact-all`, §I9b).
- **I6 (No new cross-client lock; no shared mutable index).** The publish lock is the S3
  push-lock / ref CAS already in the backend; no new cross-client lock is introduced.
- **I7 (Derived data from replay or local-only).** Aggregates are recomputed from replay of
  the retained graph; nothing derived depends on a dropped commit sha except the SNAPSHOT
  ledger positions, which are reconciled by C5.
- **I8 (Best-effort ordering under clock skew).** Reclamation preserves each commit's original
  author/committer timestamps (step 3.4); ordering semantics are unchanged.
- **I9 / I9a / I9b (Compaction safety; out-of-band).** Reclamation is a *strict extension* of
  the §I9 rule — it never retires an event whose content a not-yet-folded state could still
  need (C1 requires the SNAPSHOT to already fold it), and it runs **out of band in its own
  disposable clone** (§I9b), never on an interactive path. A concurrent remote append merges as
  a union onto the rewritten tip exactly as it would onto the un-rewritten tip.

## Consequences

- **The pack shrinks in proportion to the reclaimed commit/tree count, but not below budget
  alone.** At the default N=30d horizon the retained graph is still ~**365–380 MiB** (over the
  100 MiB budget), because ~14K commits/day of enrichment+digest churn keeps the recent window
  large. So **the W3 budget bridge (raise to 550 MiB) is unavoidable as a bridge**, and durable
  reduction needs the **structural** fixes: **W1** (batch the enrichment+digest quartet into one
  write lane — cuts ~14K commits/day at source) and **W2** (shard the 6,008-flat-dir root tree
  — cuts ~270 KB/commit of root-tree churn). This ADR is a *necessary* lever, not a *sufficient*
  one.
- **Accepted residual:** a >N-day-drifted clone's un-pushed divergent local work is discarded
  (§5).
- **New periodic causal-stability GC operation**, portable (core-git + config; any host, no
  single CI provider).
- **High blast radius at publish** — a shared-branch rewrite — mitigated by the offline shadow
  build, the remote-reachability anchor (C3), backup refs, and staged rollout (story S2 +
  rollback design).

## Alternatives considered

- **`git gc` / aggressive repack** — proven ineffective (grew the pack to 558 MiB).
- **`.retired`-only GC** — ~0 pack effect on a **blobless** pack (`.retired` are blobs);
  narrowed to a secondary sidecar-cleanup companion, not the lever.
- **`git filter-repo`** — not vendored; a single-tool dependency the portability criterion
  rejects. Core-git plumbing is used instead.
- **Shallow / scoped CI checkout** — rejected: the gate needs full ancestry for
  commit-ancestry-scoped signature verification; scoping would silence the growth canary.

## Follow-up

`docs/concurrency.md` §I1's `.retired` lifecycle note is promoted from a *follow-up* to a
*specified, in-progress* model and cross-references this ADR as the reclamation contract.
Implementation is tracked by stories **S1** (collapse engine + ledger reconciliation), **S2**
(publish-swap + rollback), and **S3** (dry-run harness) under epic `536b`.
