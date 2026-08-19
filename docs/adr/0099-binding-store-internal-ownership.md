# ADR 0099 — Binding-store internal ownership behind one unchanged facade

**Status:** Accepted
**Date:** 2026-08-18
**Ticket:** `patterned-fossillike-betafish` / `0eb8-08bd-f9cb-4798` (S1, authored this ADR)
**Finalized by:** `likeminded-wearproof-barracuda` / `ef69-e27b-d02d-4eef` (S3 T3)
**Story:** S1 `mica-governing-buck` / `5142-4dbf-9469-4672`;
S2 `brainy-cooked-hog` / `fc1c-1b08-70c2-4afb`;
S3 `convergent-pinelike-lunamoth` / `486a-69ef-6370-48ff`
**Epic:** RP-02 `citric-moudly-vipersquid` / `5e64-d9cd-2dd4-4036`
**Relates to:** [ADR 0027](0027-reconciler-binding-lifecycle.md),
[ADR 0028](0028-reconciler-bound-but-absent-not-deleted.md),
[ADR 0056](0056-decompose-pydantic-ai-runner-run.md),
[ADR 0058](0058-the-module-size-limit-file-is-the-only-loc-ceiling.md),
[ADR 0060](0060-bridge-state-format-changes.md), and
[ADR 0098](0098-operation-scoped-config-and-provider-composition.md).

## Context

`BindingStore` is the authoritative local-id ↔ Jira-key identity aggregate. Its module
had reached 799 lines — the 800-line hard cap of ADR 0058 — because JSON encoding,
unknown-field preservation, tempfile cleanup, GET-rotation ordering, retired-state
replacement, and alert persistence all lived beside identity and lifecycle policy. The
next lifecycle change had nowhere to go.

The usual answer, "split the module," is unusually dangerous here. Every asymmetry in
that file was bought with a production incident: mass-duplicated Jira issues from an
empty-looking store, a lost tombstone that turned a soft delete into a hard one, and a
rotation floor that re-GETs the world when it is dropped. Several of those behaviours
read like bugs until the incident is known. A mechanical split would preserve the shapes
and lose the reasons.

Two facts constrain the decomposition more than module size does. First, the store's
callers mutate binding entries **in place** and rely on the store's own `save()` to
persist them: `peer_state`'s baseline and peer-parent overlays, `seed_baselines_from_snapshot`,
and the rich-text handler's `peer_state.note_rich_emit` on an entry reached through
`all_bindings()` (`apply_handlers.py`). Any owner that hands out copies silently discards
those writes. Second, the four state files are written in a **specific order** under
crash, and one crash window intentionally leaves a detectable overlap rather than
rolling back — so the split must reproduce ordering faithfully, not merely reproduce
each write.

This follows ADR 0056's precedent: decompose along the seams the call graph already
has, and keep the public entry point.

## Decision

Every decision below is now **realized**. Decisions 1, 2, 4, 5, 8, 9, and 10 landed in S1
(`vivacious-widish-indianabat`, `evadable-curious-mastodon`); Decision 3's replacement seam
in S2 (`brainy-cooked-hog`), cut over in S3 T3; and the recovery obligation in Decision 5,
its invocation boundary (Decision 6) and its reversal rule (Decision 7) in S3
(`convergent-pinelike-lunamoth`). Nothing here is aspirational any more, which changes how
to read it: each rule below is load-bearing on code that exists, so weakening one is a
behaviour change, not a plan revision.

### 1. Internal ownership behind one facade

`BindingStore` remains the sole public entry point. Its public imports, its
`__init__(tracker_dir)` constructor, the `load_binding_store(repo_root)` module-level
entry point, and every public method signature are unchanged; consumers such as
`binding_walk`, `reconcile_check`, `reconcile_helpers`, and `bridge repair` bind to that
surface and were not touched.

Internally, `BindingRepository` (`binding_repository.py`, 360 lines) is the sole owner of
the four files the binding subsystem keeps on disk:

| File | Role |
|---|---|
| `<tracker_dir>/.bridge_state/bindings.json` | live bindings + reverse index |
| `<tracker_dir>/.bridge_state/bindings-retired.json` | retired (soft-deleted) tombstones |
| `<tracker_dir>/.bridge_state/get_rotation.json` | GET-rotation sidecar |
| `<repo_root>/bridge_state/bridge_alerts/*.jsonl` | best-effort lifecycle alerts |

Ownership is scoped to the reconciler's binding subsystem: the repository is the only
reader/writer inside it, and the repository instance is private, so no adapter or
reconciler caller can write binding state around the facade. It is not a global claim on
the bytes — `src/rebar/_ids.py::_resolve_via_binding_store` still performs a best-effort
**read-only** reverse lookup against `bindings.json` for CLI id resolution, and the GHA
commit-back step still commits the same files. Neither is affected.

`peer_state.py` and `get_rotation.py` remain independent owners of peer/baseline
semantics and rotation-stamp arithmetic; both are unchanged by this work.

Lifecycle policy — bind/confirm/retire/tombstone/comment bookkeeping and absence grace —
stayed in the facade through S1 and moved to `BindingLifecycle` (`binding_lifecycle.py`) in
S2; incomplete-operation recovery moved to `BindingRecovery` (`binding_recovery.py`) in S3.
The facade holds all three owners privately and delegates. Ownership is now three-way and
strict: the repository owns every byte on disk, the lifecycle owner owns identity
transitions, and recovery reaches transitions **through** the lifecycle owner rather than
re-implementing confirm/unbind — otherwise a recovered binding would not be byte-identical
to one the ordinary path produced and the store would slowly acquire two dialects.

### 2. Open views, not copies

The repository hands out the **original** dictionaries (`data`, `bindings`, `reverse`,
`rotation`, the retired key set, and the retired-locals index), and the facade **aliases**
them: `self._data` *is* `repository.data`, so `self._data["bindings"]` is the
repository's `bindings` object. `_retired` and `_retired_locals` are likewise the same
mutable objects `_retire`/`unretire` keep in lock-step.

This is required, not convenient. The in-place writers listed in Context persist through
the store's `save()`; a defensive copy at this boundary would drop every one of them
silently, with no exception and no diff.

Mutation testing found the specific trap that makes a weaker copy look safe. A merely
**shallow** copy of the document still shares the inner `bindings`/`reverse` dicts, so
every in-place entry write still lands and every other characterization assertion still
passes — only a **new top-level key** diverges. `record_comment_id` inserts exactly such
a key via `setdefault("comment_ids", …)`, which is the normal path on a legacy store
written before comment sync existed. Under a shallow copy that key would live only in
the facade and never reach the serialized bytes: comment identity would be lost and
every mirrored comment re-posted on the next pass (the DIG-5301 duplicate class). This is
the one delegation defect the rest of the suite cannot see, and
`test_top_level_insert_on_a_legacy_store_reaches_the_persisted_bytes` exists solely to
see it.

### 3. `all_bindings()` stays SHALLOW

`all_bindings()` returns a fresh outer mapping over the **live** inner entries. Its copy
depth is deliberately unchanged: a caller may iterate the result while the lifecycle adds
or removes bindings, while the baseline advance and the rich-text handler mutate an entry
obtained from it and expect the next `save()` to persist it. Deep-copying would break
them; that is why this slice preserves the shallow contract verbatim.

Mutation-through-query was nevertheless the hazard RP-02 set out to close, and it is now
closed. `BindingStore.note_rich_emit` (S2 `morose-selfaware-unicorn`) is the narrow
**named** mutation operation, and `apply_handlers._observe_rich_reemit` walks through it
(S3 T3 `likeminded-wearproof-barracuda`), so **no production module reaches a binding
through `all_bindings()` in order to write to it**. The copy depth itself never changed.

The distinction to preserve: `all_bindings()` is a READ-shaped query. Using it as a write
seam was not a performance problem — it was an *unowned* one. The store never saw those
mutations, so it could enforce no invariant on them, and the justification at the call site
long outlived its reason (it claimed the facade "cannot carry a narrower accessor" after the
accessor existed). Remaining callers are held to being reads by a standing allowlist census
that fails when a new caller appears, so the next `all_bindings()` caller has to be
classified in writing rather than added silently.

### 4. Three asymmetric failure dispositions, each preserved exactly

Each is preserved for its reason, not for parity:

- **The live store fails CLOSED.** An unparseable `bindings.json` — typically git
  conflict markers from a tickets-branch merge — raises `ValueError` naming the offending
  file and the exact recovery route
  (`git show tickets:.tickets-tracker/.bridge_state/bindings.json`). Degrading to an empty
  store would report every ticket as unbound, so the very next outbound pass would
  re-create every issue in Jira. That write is irreversible; aborting the pass is not.
  Only the operator can decide how to resolve a merge conflict, so the reconciler refuses
  to guess.
- **The retired store fails OPEN.** Corruption degrades to an empty set plus one deduped
  `binding-retired-file-corrupt` alert. A retired binding wrongly treated as live costs
  one wasted GET — it re-404s and re-retires — never a duplicate write. The alert is what
  keeps the degradation visible, since an empty retired set is otherwise
  indistinguishable from "nothing has ever been retired".
- **The rotation sidecar fails OPEN.** Rotation is an optimization worth a bounded extra
  GET, so a failed sidecar write must not abort the save.

Alerting is best-effort by the same logic: every failure inside `alert()` is swallowed.
Observability must never break a sync pass.

### 5. Ordered partial states, not multi-file atomicity

There is **no multi-file atomicity guarantee**, and none is claimed. Each file is
individually atomic: `tempfile.mkstemp` in the destination directory plus `os.replace` on
the same filesystem, with the temp file unlinked on any failure (including
`KeyboardInterrupt`/`SystemExit`) so an aborted pass leaves no litter beside real state.

The **order** is the contract:

- **Rotation before live.** `save()` writes the sidecar first and scrubs the legacy inline
  `last_get_pass` floor from the live entries **only** when that write durably took the
  stamp (`get_rotation.save` returning `True`). Scrubbing after a fail-open sidecar write
  would lose the newest stamp outright and re-GET everything. The live replacement happens
  either way; a rotation failure never aborts the save.
- **Retired before live on retirement.** `_retire` pairs `save_retired()` with `save()` so
  the tombstone is durable **before** the live forward/reverse pair is dropped. A crash
  between them in the other order would lose the entry from both files and make a soft
  delete indistinguishable from a hard one.

The retired-first order therefore produces one detectable partial state by design: a
crash after the tombstone lands but before the live replacement leaves **one exact
identity both live and tombstoned**. That state is intentional and completable — the
identity matches on both sides, so completion needs no guessing — and the repository must
produce it faithfully rather than rolling the tombstone back
(`test_live_failure_after_successful_retired_write_retains_the_tombstone`).

Classifying and completing it is **realized** in `binding_recovery.py`
(S3 T1 `polarized-servile-jenny`). The rules, as landed:

- **Completion requires EXACT agreement between three identities**: the tombstone's
  `local_id`, the live forward entry's `jira_key`, and the live reverse entry's local id.
  Anything else is refused with a typed abort that carries the identities which disagreed.
  A refusal names its conflict because "unsafe" names no next step, and the operator's first
  question is always *disagreed how* — by the time anyone reads the report the store has
  usually moved on, so the evidence has to travel with the refusal.
- **A tombstone with no live residue is SILENT** — neither completed nor refused. In a
  healthy store that is very nearly every tombstone, because retirement finished. Reporting
  those as aborts would bury the one genuine finding under a report the size of the retired
  file, and a per-pass "nothing to repair" line would train operators to skip the one line
  that matters.
- **Completion removes only the live forward/reverse pair, under one `save()`, and NEVER
  touches the tombstone.** One save because the removals are independent and a per-candidate
  save would leave a mid-batch crash in exactly the state this repair exists to clean up.
- **Repeating it writes nothing.** Nothing is written when there is no exact-match
  candidate — *even when there are refusals*. Otherwise every pass would rewrite (and
  re-commit to the tickets branch) the whole live store just to report that it changed
  nothing, and a store holding one permanently-refused tombstone would churn forever.
- **A failed live replacement is rolled back in memory and reported as a refusal.** Leaving
  the pairs popped would hand the rest of the pass a view that disagrees with disk, after
  which the next `save()` from any other owner would commit a deletion this method already
  declined to report as done. The failure is returned, never raised: a binding repair must
  not be the thing that aborts a sync pass.

Two behaviours are worth stating outright because the planning wording implied otherwise:

- **An unparseable live store cannot produce an abort at all.** It fails CLOSED in the
  repository loader (Decision 4), so construction raises and repair is never reached. The
  reachable "corrupt live" case is a malformed entry *shape* — it parses, so it does reach
  the classifier, and it is refused rather than interpreted.
- **A corrupt retired file fails OPEN**, which makes tombstones invisible and therefore
  yields no candidate. The consequence is a *missed* repair, not a guessed deletion. That is
  the correct direction: the missed repair survives to the next healthy pass, while a guess
  is unrecoverable.

### 6. The repair runs BEFORE the pass's first remote observation

This is the decision in this ADR most likely to be silently undone, because undoing it
breaks nothing visible. Write it down, and read it before moving the call.

The repair is invoked from `reconcile.py`'s load phase (`_load_snapshots`), **immediately
after the under-lock staleness gate and before the pass's FIRST remote issue fetch**
(S3 T2 `flamboyant-possessive-blackbuck` / `90a5-6a33-5cda-4064`).

**Why pre-observation is load-bearing.** A tombstone is authoritative retirement *intent*.
A pass that fetched first could observe the retired issue answer 200 — because a retired
binding's Jira issue may well still exist — and then complete that retirement in the same
breath as fresh evidence that the identity is alive. Nothing in the completion logic is
wrong in that ordering; what is wrong is that two contradictory facts about one identity
would be in play at once, and a future reader trying to reconstruct why a binding vanished
could not tell which one the pass acted on. Repairing first stops those facts being
interleaved at all. The other end of the window matters for a symmetrical reason: a pass
whose selection is stale has not established that its view is current, so it has no
standing to repair anything — hence *after* the staleness gate, not before it.

**Why NOT `run_differs.py`,** which both the epic's constraint list and this story's
original plan named. `reconcile._load_snapshots` constructs the store, runs the staleness
gate, and performs the first remote fetch all inside one call; `bind_operation_runtime` and
`run_differs()` both run only after that call returns. **No position inside
`run_differs.py` can precede remote observation.** The create-recovery call does live
there, which is exactly the trap: the two recoveries look like siblings and have
deliberately different boundaries, so "tidying" this one next to that one voids the
guarantee while leaving every other assertion green.

**The epic constraint "after the configuration snapshot AND before the remote fetch"
describes an EMPTY window,** for the same reason: the operation config snapshot is composed
*after* the load phase. The conjunction was unsatisfiable, so it was resolved rather than
forced. The repair reads only local binding state and therefore has no dependency on that
snapshot at all — the pre-observation half was kept, the snapshot conjunct dropped. Where
the operation config snapshot is composed remains ADR 0098 / RP-04's decision, not this
ADR's.

**The guard.** Repair happens only on a pass that is **write-bearing (`persist`) and
unscoped**. A cap-0 mode is documented read-only and completing a retirement is a write; a
filtered pass reasons about a hand-picked subset, and the repair is store-wide by nature
(it walks every tombstone) so it cannot be narrowed honestly — a scoped pass declines the
whole operation rather than half of it. **Refusing is free**: no classification runs and
nothing is read, which matters because every read-only pass reaches this line. Construction,
load, reconcile-check, dry-run, preview, selection and filtered paths write nothing.

**For whoever next splits `reconcile.py`.** That file sits at 799 of the 800-line cap, so
the next change needing room there is forced into an extraction, and one is already queued
against it. The spine therefore carries a single unconditional call line and *all* guard
logic lives in `binding_recovery.py`, so the position of that line is the entire ordering
guarantee. **Whoever splits this file must keep the call after the under-lock staleness gate
and before the first remote fetch.** An extraction can relocate it without breaking any
other assertion in the suite, and if it lands after the first fetch the guarantee is
silently void — the repair would still work, still be tested, and still be wrong. The
behavioural oracle that proves the ordering is
`test_repair_runs_before_the_first_remote_observation` in
`tests/unit/rebar_reconciler/orchestrate/test_recovery_write_boundary.py`.

### 7. A tombstone is revoked ONLY by an explicit `unretire`

Liveness observations are not revocation signals. None of `absent_404_count`, a later 200
GET, `clear_absent`, or a same-key `bind_confirm` revokes a tombstone, and a historical
live+tombstoned overlap still completes after any of them. **Only an explicit `unretire`
removes the tombstone**, and after it there is no completion candidate left to act on.

The reason is the incident the retired-first write order exists to prevent. Retirement is a
*soft* delete: reversible, and reversible only because the tombstone survives. If an
automatic liveness observation could resurrect a tombstoned identity, then the ordinary
consequence of retiring a binding whose Jira issue still exists — which is the normal case,
since retirement follows repeated 404s that may themselves have been transient — would be
that the next successful GET undoes an operator's retirement without anyone asking. So
completion deletes live residue and never a tombstone, and no automatic observation
resurrects a tombstoned identity. `unretire` is the documented, deliberate route back, and
it is the only one.

### 8. Persistence is NOT publication

Local durability and tickets-branch publication are separate layers, and a repository
checkpoint **never** publishes. `BindingRepository.save()` / `save_retired()` write and
`os.replace` local files only. If either also committed, every intermediate checkpoint in
a pass would land its own tickets-branch commit, turning one pass into a stream of commits
and coupling local durability to git availability.

Publication stays in `reconcile_helpers.py::_commit_binding_store_snapshot`, behind an
**exclusive** five-file allowlist: `bindings.json`, `bindings-retired.json`,
`get_rotation.json`, the impossible-inbound-link record, and the per-link
peer-confirmation record. **That five-file selective publication is unchanged by the
recovery work**, and it is the only publication route: no repair, checkpoint, or recovery
write may publish on its own. Broad `git add -A` staging in the tickets worktree is
**prohibited** — for a repair exactly as for an ordinary save, and the repair is the
tempting case precisely because it runs unasked mid-pass. That worktree is shared and can
legitimately hold unrelated operator work in progress, a scratch file, or an editor
artifact, and a reconciler commit must not sweep them in. The allowlist is asserted
negatively as well as positively
(`test_publication_stages_only_the_five_allowlisted_files`). Staging is per-file
idempotent by basename, so an **unchanged pass creates no commit**, and a
retirement-only pass is still published. Publication itself fails open — an error is
logged, alerted, and the pass continues on the filesystem copy. Lifecycle alerts are not
published at all; they live outside the committed tracker tree.

### 9. Explicitly rejected alternatives

- **No journal or write-ahead log.** The ordering contract in Decision 5 already yields
  states that are detectable and completable from the two files themselves. A journal
  would add a third durable format, its own corruption modes, and its own recovery code
  to solve a problem the order already solves.
- **No stored-format migration.** Both files keep their current shape, including the
  legacy list-form `retired` value and the legacy inline `last_get_pass` stamp, which are
  read additively.
- **No eager rewrite on load.** Construction **reads only** and does not even create
  `.bridge_state`; the first `save()` does. An eager rewrite would churn the tickets
  branch on every pass and, worse, would re-serialize a store a newer rebar may have
  written with fields this build does not understand — before anything has changed.
- **No deep-copied views.** See Decision 2: it silently drops the in-place writes the
  system depends on.
- **No write-elision or dirty-gating optimization.** `save()` stays **unconditional**: it
  always attempts rotation persistence before replacing live state, even when nothing
  changed in memory. Dirty tracking would add a correctness question ("is this dirty flag
  right?") to the one path whose failure mode is duplicate Jira issues, in exchange for
  saving a small local write.
- **No new identity writer.** `BindingStore` stays the single identity owner; the
  repository is private and no collaborator gains a second door to binding state.

### 10. Compatibility and rollback

Each slice is an **internal delegation** with unchanged stored formats and unchanged
public signatures, so a slice can be reverted independently to the preceding facade
implementation without a data migration or a coordinated consumer change.

**The retirement repair is the one intentional behaviour delta in this epic.** Everything
else — the persistence owner, the lifecycle owner, the recovery owner, the narrow rich-emission
operation and its caller cutover — is extraction: same bytes, same signatures, same
observable outcomes. Say so explicitly, because "RP-02 changed no behaviour" is otherwise the
easy summary and it is wrong in exactly one place, which is the place a future incident will
look at first.

That delta is **additive and idempotent**, and its rollback is correspondingly cheap.
Disabling the pre-observation call restores the prior state of the world exactly: the
live+tombstoned overlap goes back to being detectable but uncompleted, which is what the
retired-first order was already documented to produce. Coherent stores are never rewritten,
so the repair is a no-op on every healthy store and a revert cannot un-repair anything a
healthy pass did. **Rollback never deletes a remote issue** — the repair only removes local
live residue and issues no Jira write of any kind — **and never implies an automatic
unretire** (Decision 7): a reverted repair leaves the tombstone standing, because nothing in
either direction may resurrect a tombstoned identity.

Unknown and legacy fields — top-level and per-entry — survive a load/save round trip
**byte-identically**, because the file is shared with other writers and dropping what this
build does not understand would corrupt their state. Serialization is `indent=2`,
`sort_keys=True`, and a single trailing newline. That is a contract rather than a
preference: these files are committed to the tickets branch and diffed, so stable key
order is what keeps the diffs reviewable.

The compatibility surface is pinned by characterization tests rather than left implicit —
public parameter names and kinds (not rendered annotation text), the shallow copy depth,
pending/confirmed/reverse across a real reload, facade-versus-repository byte parity, the
shared corruption disposition, `peer_state`/`get_rotation` independence, and
non-exposure of the repository. Because a no-behaviour-change slice is green before and
after by construction, those tests earn their keep through defect-seeded mutation, not
through passing.

### 11. Forward-looking notes for cross-epic coordination

Both notes here were written while the config reads still lived in these modules. They have
since been cut out from under this ADR by RP-04, and both bullets are recorded in their
corrected form rather than deleted — the second one because the *lesson* outlived the
reference.

- **The lifecycle config read is GONE from this subsystem — cut to an owned configuration
  seam by RP-04 C3g** (`toadyish-magic-arrowana` / `2a95-236b-535f-460e`). S2 T2
  (`sportive-statued-goose`) had moved the ambient read from `binding_store.py` to
  `binding_lifecycle.py` beside the `note_absent` policy it parameterizes; RP-04 then removed
  it entirely. The `_env_int` helper is gone, `binding_lifecycle.py` no longer imports `os`,
  and `note_absent` calls `resolve_absent_retire_grace()` from `rebar.config` — which owns the
  read. `_DEFAULT_ABSENT_RETIRE_GRACE` remains in `binding_lifecycle.py` as the shared
  fallback default, and `binding_store.py` keeps an alias of it for its long-standing
  inspection surface. **Behaviour is preserved**: the accessor reads the same
  `RECONCILER_ABSENT_RETIRE_GRACE`, defaults to 3, and clamps to a minimum of 1, with the same
  defensive parse (a malformed value degrades to the default rather than aborting the pass).
  Consequently `scripts/config_ownership_exceptions.py` has been drained to EMPTY (`_ROWS =
  []`) and the two rows this ADR previously described as "re-registered under
  `binding_lifecycle.py`" no longer exist. Do not re-add them: a row there is a legacy
  exception, and there is no longer a read for it to except.
- **Generated references keyed on LINE NUMBERS are a standing hazard, even though this
  particular one is now moot.** `docs/env-vars.md` is generated by
  `scripts/gen_env_registry.py` and records the line numbers of *non-literal* env reads.
  There are no longer any such reads in `binding_lifecycle.py` or `binding_store.py`, so the
  specific reference this bullet used to name is gone along with the reads. The general lesson
  is worth keeping in one sentence, because it bit three separate times during RP-02 and each
  time it was a one-line shift in a generated file that failed the CI drift gate: **any
  change that moves code in a module with generated line-number references must regenerate
  those artifacts in the same change.**

## Consequences

### Positive

- `binding_store.py` went from 799 lines — the ADR 0058 cap — to 554 across the three
  slices, and each extracted concern gained a cohesive owner (`binding_repository.py` 360,
  `binding_lifecycle.py` 506, `binding_recovery.py` 475). The next lifecycle change has room,
  which is what the whole exercise was for.
- Every asymmetry now carries its incident rationale at the point of enforcement, so a
  future reader cannot mistake fail-open retired state or an unconditional `save()` for an
  oversight.
- The ordered partial states have direct fault oracles, which is what made the later slices
  safe to attempt at all.
- Local durability and tickets-branch publication can now fail independently without
  either taking the other down.
- Production no longer writes through a read-shaped query: every binding mutation goes
  through a named facade operation the store can hold an invariant on.
- The one crash window the write order leaves is now completed automatically on the next
  write-bearing unscoped pass, before that pass observes anything remote.

### Negative

- The open-view contract is a real coupling: any future owner that copies at this boundary
  reintroduces silent write loss, and only mutation testing reliably catches the shallow
  variant.
- The repair's correctness now depends on the *position* of one call line in a file that is
  at the module-size cap and already queued for extraction (Decision 6). Nothing fails when
  that line moves; only the ordering oracle notices.
- Multi-file crash windows remain observable. A store holding a permanently-refused
  tombstone stays inconsistent by design — it is reported every write-bearing pass and waits
  for a human, because no automatic completion can adjudicate identities that disagree.
- The repair's observations go to `RECON:` stderr rather than the pass's structured
  `sync_logger`, since threading the logger down would have widened the single call line in a
  file with no room. Promoting them to structured pass events is a recorded residual gap.

### Neutral

- No stored format, event, ticket, or bridge-state file layout changes, and the committed
  bytes are identical for an unchanged store.
- The GHA commit-back step and `src/rebar/_ids.py`'s read-only reverse lookup are
  unaffected.
