# ADR 0099 — Binding-store internal ownership behind one unchanged facade

**Status:** Accepted
**Date:** 2026-08-18
**Ticket:** `patterned-fossillike-betafish` / `0eb8-08bd-f9cb-4798`
**Story:** `mica-governing-buck` / `5142-4dbf-9469-4672`
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

Decisions 1, 2, 4, 5, 6, 7, and 8 are **realized** in the landed S1 slice
(`vivacious-widish-indianabat`, `evadable-curious-mastodon`). Decision 3's replacement
seam and the recovery obligation in Decision 5 are **planned** for S2
(`brainy-cooked-hog`) and S3 (`convergent-pinelike-lunamoth`).

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

Lifecycle policy — bind/confirm/retire/tombstone/comment bookkeeping, absence grace, and
recovery — deliberately **stays in the facade for now**. Extracting it is S2; this
decision records only the persistence boundary.

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

Mutation-through-query is nevertheless the hazard RP-02 is closing. A narrow **named**
mutation operation on the facade will replace the pattern for rich emission, after which
production stops writing through a query result. That seam is **planned, not landed**
(S2 `morose-selfaware-unicorn`, cut over in S3 `likeminded-wearproof-barracuda`); this
ADR does not change `all_bindings()`.

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
(`test_live_failure_after_successful_retired_write_retains_the_tombstone`). Classifying
and completing it is S3's job (`polarized-servile-jenny`), invoked only at a validated
write boundary so read-only commands never repair.

### 6. Persistence is NOT publication

Local durability and tickets-branch publication are separate layers, and a repository
checkpoint **never** publishes. `BindingRepository.save()` / `save_retired()` write and
`os.replace` local files only. If either also committed, every intermediate checkpoint in
a pass would land its own tickets-branch commit, turning one pass into a stream of commits
and coupling local durability to git availability.

Publication stays in `reconcile_helpers.py::_commit_binding_store_snapshot`, behind an
**exclusive** five-file allowlist: `bindings.json`, `bindings-retired.json`,
`get_rotation.json`, the impossible-inbound-link record, and the per-link
peer-confirmation record. Broad `git add -A` staging in the tickets worktree is
**prohibited**: that worktree is shared and can legitimately hold unrelated operator work
in progress, a scratch file, or an editor artifact, and a reconciler commit must not sweep
them in. The allowlist is asserted negatively as well as positively
(`test_publication_stages_only_the_five_allowlisted_files`). Staging is per-file
idempotent by basename, so an **unchanged pass creates no commit**, and a
retirement-only pass is still published. Publication itself fails open — an error is
logged, alerted, and the pass continues on the filesystem copy. Lifecycle alerts are not
published at all; they live outside the committed tracker tree.

### 7. Explicitly rejected alternatives

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

### 8. Compatibility and rollback

Each slice is an **internal delegation** with unchanged stored formats and unchanged
public signatures, so a slice can be reverted independently to the preceding facade
implementation without a data migration or a coordinated consumer change.

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

### 9. Forward-looking notes for cross-epic coordination

Two references outside this ADR's scope move when these modules are restructured, and
both will fail CI if they are not updated in the same change.

- **The lifecycle config reads moved with lifecycle policy — REALIZED in S2 T2**
  (`sportive-statued-goose`). The `_env_int` helper, the `_DEFAULT_ABSENT_RETIRE_GRACE`
  default and the `RECONCILER_ABSENT_RETIRE_GRACE` read now live in
  `binding_lifecycle.py`, beside the `note_absent` policy they parameterize;
  `binding_store.py` keeps only an alias of the default for its long-standing inspection
  surface. The sourcing is unchanged and still **ambient** — a direct `os.environ` read,
  the same defensive parse (a malformed value degrades to the default rather than aborting
  the pass), the same default of 3, the same clamp to a minimum of 1. Cutting them to a
  configuration seam is still **not** RP-02's work; it belongs to the
  operation-scoped-config effort of ADR 0098 (RP-04 S7.3.a, `insincere-illogical-antlion`,
  with `best-kingly-monkey` tracking the retarget). Because
  `scripts/config_ownership_exceptions.py` keys its legacy exceptions on **path + symbol**,
  both rows (`RECONCILER_ABSENT_RETIRE_GRACE` and the dynamic `os.environ.get`) were
  re-registered under `_engine/rebar_reconciler/binding_lifecycle.py` in the same change and
  the stale `binding_store.py` rows removed, keeping the registry truthful; the gate passes.
- **`docs/env-vars.md` must be regenerated.** It is generated by
  `scripts/gen_env_registry.py` and records the **line numbers** of non-literal env reads,
  so restructuring these modules shifts those references. This is not hypothetical twice
  over: the landed delegation slice shifted the `_env_int` read in `binding_store.py` from
  line 110 to line 109 and the CI drift gate caught the staleness, and S2 T2 moved it again
  — the non-literal read is now `binding_lifecycle.py:69`, and
  `RECONCILER_ABSENT_RETIRE_GRACE` is attributed to `binding_lifecycle.py` rather than
  `binding_store.py`.

## Consequences

### Positive

- `binding_store.py` drops from 799 to 710 lines and persistence gains a cohesive
  360-line owner, so the next lifecycle change has room under the ADR 0058 cap.
- Every asymmetry now carries its incident rationale at the point of enforcement, so a
  future reader cannot mistake fail-open retired state or an unconditional `save()` for an
  oversight.
- The ordered partial states have direct fault oracles, which is what makes the remaining
  slices safe to attempt at all.
- Local durability and tickets-branch publication can now fail independently without
  either taking the other down.

### Negative

- The open-view contract is a real coupling: any future owner that copies at this boundary
  reintroduces silent write loss, and only mutation testing reliably catches the shallow
  variant.
- Mutation-through-query survives this slice. Until S2's named seam lands, production
  still writes to an entry obtained from `all_bindings()`.
- Multi-file crash windows remain observable, and the retirement overlap stays
  unrepaired until S3.

### Neutral

- No stored format, event, ticket, or bridge-state file layout changes, and the committed
  bytes are identical for an unchanged store.
- The GHA commit-back step and `src/rebar/_ids.py`'s read-only reverse lookup are
  unaffected.
