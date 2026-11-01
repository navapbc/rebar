# ADR 0121 — Reconciler architecture: consolidated decision record

**Status:** Accepted — consolidates ADRs 0004, 0026, 0027, 0028, 0029, 0031, 0055, 0060, 0062, 0083, 0084, 0085, 0092, 0094, 0095, 0096, 0097, 0099, 0103, 0107.
**Date:** 2026-09-07

## Why this record exists

Twenty accepted ADRs describe one subsystem — the reconciler and its Jira bridge. Read
individually they are accurate; read together they are not navigable, because several
record a decision that a later ADR has since narrowed, amended, or replaced. A reader
who opens one file cannot tell whether what it says is still the rule.

This record states each surviving decision **once, in its final form**. It is not a
verbatim copy of the twenty originals: those remain in `docs/adr/` as the evidence
record, carrying their context, their measurements, and the reasoning that produced
each decision. This file carries the conclusions.

**How to read it.**

- One section per surviving decision. The originating ADR is named in the section
  heading, so every statement here is traceable to the file that proves it.
- Where a later ADR overrode an earlier one, the decision appears **once, in final
  form**, with an explicit `supersedes ADR 00xx §y` note at the point of override.
- Where an ADR recorded a *transition* — a phased cutover, a migration sequence, a
  deployment gate — and that transition has since landed, this record states the
  resulting steady state and marks the transition as landed history.
- Every cross-reference to another ADR is a **filename link**, never a bare number.
  Three files in `docs/adr/` carry the number 0035, so a bare number is ambiguous in
  this repository (see [RENUMBERING.md](RENUMBERING.md)).
- Appendix B maps each consolidated ADR to the section or sections that carry it.

Three ADRs about adjacent subsystems are deliberately **out of scope** and remain
authoritative on their own: 0070 (config CLI), 0087 (LLM transport retry), and 0111
(no internal-only compatibility shims).

---

## Part A — Data contracts

### 1. Producer↔consumer snapshot contract and the rules for new reconciler tests — from ADR 0004

The snapshot that the fetcher produces and the differs consume is governed by one
contract, enforced in three places.

**One contract test, through the production path.** A single schema and round-trip
test drives the fixture client through `fetcher.fetch_snapshot` into the real differs.
There is no parallel serialization and no hand-built snapshot dict anywhere in the
test suite. This test is the keystone: a key or shape divergence between the fetcher
and any differ fails it. Full live-sandbox end-to-end testing and cross-team contract
brokers were both rejected as overkill for a single team.

**The snapshot-entry shape has one source of truth, and it is enforced.** The shape
lives in `src/rebar/schemas/jira_snapshot_entry.schema.json`, loadable through the
packaged-schema machinery. Each property's description names the consumer that reads
it. Two mechanisms keep the schema honest rather than aspirational: the schema is
generated into `src/rebar/types.py` as the `JiraSnapshotEntry` `TypedDict`, so the
generated-types drift gate fails CI when schema and generated layer fall out of step;
and the contract test validates every real `fetch_snapshot` entry against the schema,
so a fetcher emitting a diverging shape fails immediately. The generated `TypedDict`
is deliberately open-typed for nested REST sub-objects — the JSON Schema, not the
`TypedDict`, is the enforcement surface. When a field is added to the snapshot, the
schema is updated first.

**Assert structure, type, and semantic round-trip — never values or interactions.**
A test that pins exact payloads or mock-call counts breaks on every benign edit and
trains engineers to update the golden without thinking, detecting *change* rather
than *defects*. This rule governs every new reconciler test.

### 2. Typed mutation payload: a discriminated union by (direction, action), and its ownership table — from ADR 0107

**One discriminated payload type per live `(direction, action)` pair.** Ten frozen,
slotted dataclasses, one per live combination: `OutboundCreatePayload`,
`OutboundUpdatePayload`, `OutboundDeletePayload`, `OutboundProbePayload`,
`OutboundConflictPayload`, `InboundCreatePayload`, `InboundUpdatePayload`,
`InboundCleanLabelPayload`, `InboundRepairPropertyPayload`, and
`InboundConflictPayload`. There is **no eleventh generic payload type**, and no field
is optional on more combinations than already tolerate its absence.

This extends three in-repo precedents rather than inventing a fourth pattern:
`mutation.py`'s own direction/action enum pair plus its valid-combination allowlist;
the `"kind"` evidence discriminator in `common.schema.json`; and the explicit row
outcome field in `llm/usage_log.py`. The last of those supplies this ADR's own
validation discipline: an absent optional field encodes "not applicable", and is
**never** overloaded to also mean that something failed.

**Ownership is explicit, and nothing is centralized that was not already central.**

| Concern | Owner |
|---|---|
| Field validation | each payload dataclass's `__post_init__` — no new central validator |
| Canonical serialization and comparison | `serialize_manifest`, via a new `payload.as_legacy_dict()` on every payload type |
| Adapter projection (vendor field mapping) | the existing `OutboundMapper` / `InboundMapper` ports |
| Follow-ons and suppression | the existing untyped `follow_on: dict` on `ApplyResult` — deliberately NOT promoted to a discriminated field |
| Binding identity | `binding_store`, unaffected |
| Provider-neutral outcomes | `ApplyResult` in `apply_base.py`, unaffected |
| Retry, idempotency, and fuse | solely §12's retry budget and coordinator |

**Emitted bytes are a compatibility surface.** `serialize_manifest` keeps its existing
sort order and hashing; the single added responsibility is calling `as_legacy_dict()`
instead of `dict(m.payload)`, so the emitted JSON for any already-shipped
`(direction, action, target)` triple is unchanged. The in-memory typed representation
is comparison-only and may differ in shape, provided `as_legacy_dict()` round-trips
byte-identically.

**Reconciliation with the other decisions here.** §1's snapshot contract governs the
fetcher's output shape, not the payload, so there is no conflict. §§5–8's
field-arbitration policies are *inputs* to payload construction: the typed payload
carries the output of that arbitration and never re-decides it. §15's dynamic-loader
location pinning is respected — `mutation.py` stays at its current path. §19's
vocabulary translation stays in the mapper ports. §12's retry ownership is explicitly
reaffirmed, not reopened: the payload types carry no retry state at all.

*Landed history.* The original's migration sequencing and its open implementation
items were a phased transition that has since completed (story
`e9d5-c850-d505-4a5a`). The typed union above is the steady state.

### 3. Bridge state stays committed, and its formats stay cheap — from ADR 0060

Bridge state remains on the tickets branch, because ephemeral workflow runners would
otherwise lose durable state or require a second persistence service with its own
consistency protocol. Four format changes keep that commitment cheap.

**Delete the baseline timestamp and guard unchanged baselines.** `baseline_advanced_at`
is gone from new writes; no production reader consumed it, so historical copies are
harmless. `set_baseline` compares the canonical new value against the stored value
before serializing, so two unchanged passes leave `bindings.json` byte-identical and
create no commit.

**Rotation stamps move to a sidecar by expand-then-contract.** The expand phase writes
each stamp to both the legacy inline field and `get_rotation.json`, and readers take
the chronological **maximum** of the two. The maximum is required, not a preference: a
first-wins lookup selects stale state whenever an old writer advances only the inline
field, or a new writer advances the sidecar before an older binary runs. The contract
phase merges staged inline values into the sidecar by maximum, persists the sidecar
atomically, and only then removes the inline fields and replaces `bindings.json`. Every
intermediate failure state converges without losing rotation progress.

**The previous snapshot persists key membership only.** `prev_snapshot.json` stores
`{jira_key: {}}`. The mapping shape is intentional — consumers rely on mapping
membership and `value or {}` behaviour, so a set or other scalar form is not an
equivalent contract. The call-site filter remains the load-bearing protection against
prev-only synthetic mutations. Deleting `prev_snapshot.json` is **not** the default
rollback: an empty previous snapshot can re-derive inbound creates. If deletion is ever
required, it must be followed by a cap-zero, no-write pass proving no unexpected
inbound creates before writes are re-enabled.

**Deployment readiness is established by observed writer identity and behaviour, never
by elapsed time.** A fixed rollout delay does not identify the binary that actually
ran, does not detect a forgotten host process, and cannot prove workflow ancestry.
A pass-record version stamp cannot establish it either: the record carries a pass id,
mutation count, and completion status, but no executable or source SHA.

### 4. The project set belongs to the store; legacy migration writes no ticket events; the capability is stamped conditionally — from ADR 0097

**The sync list is a property of the STORE, not of a repo.** It lives in one committed
file per store, `.bridge_state/projects.json` on the tickets branch, keyed by project.
The reconciler reconciles the whole store against one shared previous snapshot per
pass, and every project's inbound records merge into that one snapshot before the
reducer applies them. A per-repo project list has no coherent place to attach that
shared snapshot: two repos claiming overlapping or disjoint project sets would each
need a private view of the remote, fragmenting the one snapshot the reconciler is
built around and making "did this pass see every project" unanswerable. Store
ownership keeps exactly one authority for the sync list and one snapshot reconciled
against it.

This satisfies §3's cheap-format constraint by construction: the mapping is one small
JSON object written only when an operator changes the sync list or when the one-time
seed runs — never per ticket and never per pass.

**The legacy migration emits ZERO ticket events.** Two ensure-registry units
(`projects-seed`, `projects-compat-stamp`) stamp two committed tickets-branch files.
Legacy tickets — those with no `bridge_project` field — are never rewritten; they
resolve to the mapping's `legacy_default` at read time through a tri-state `None`
sentinel. An edit-per-ticket backfill was rejected because the ensure units run under
the store write lock inside `run_ensures`: a backfill would emit one edit event per
legacy ticket under that single lock, blowing the short lock budget the ensure sweep
is designed to hold and making a routine `init` on a large legacy store pause for the
whole rewrite. The tri-state field makes the backfill unnecessary.

**The compatibility capability is stamped CONDITIONALLY.** `multi-project-bridge` is
written into the store-compat record's required capabilities only once the mapping
actually holds more than one project. The stamping unit is level-triggered on the
mapping's state, so it stamps whenever the store is genuinely multi-project, however
it got there. Stamping unconditionally on ship would make every store on a new binary
declare a capability it does not use, and the store-compat record is a fail-closed
forward guard.

---

## Part B — Arbitration

### 5. Three-way-merge baseline and direction arbitration — from ADR 0026

**The reconciler retains a three-way-merge baseline** — a per-pair last-synced value
for the inbound-mirrored fields. Direction arbitration is: `local == baseline` mirrors
Jira inbound and suppresses outbound; `local != baseline` is a local-wins outbound.

**An absent or empty baseline degrades to local-wins.** This is safe but lossy: it
loses Jira-edit mirroring, it does not corrupt data, and it is the only acceptable
fallback — first sight of a pair, for example. **A corrupt baseline fails the pass
closed.** Reconciling from an unknown or partial baseline is forbidden.

**The baseline may be re-keyed per binding** — a last-synced field cache on the
binding entry rather than a whole-Jira previous snapshot. This is encouraged: it makes
the level-triggered, binding-driven loop possible and unlocks per-field three-way
merge, so only the fields that actually changed on each side move, reducing false
clobbers when both sides edit different fields. Binding *presence* alone is not a
substitute for the baseline *value*; direction cannot be arbitrated without the
ancestor value.

**The baseline advance means last-SYNCED, not last-FETCHED.** Recording the pass-start
snapshot alone is wrong, because that snapshot is fetched before the outbound apply and
therefore holds Jira's pre-push value for every field this pass pushed. A revert then
reads as "local never changed", outbound stands down, and the inbound differ mirrors
the stale remote back over the revert. The advance therefore overlays the values the
outbound writes confirmedly landed on top of the fetch.

**The overlay is gated on PER-MUTATION success, never on the pass exit code.** A status
transition can soft-fail while the pass still exits 0. Recording a write that did not
land is strictly worse than the bug the overlay fixes: a clobber is self-limiting — the
baseline advances to Jira's value, local then differs, the next pass re-pushes —
whereas a falsely-advanced baseline asserts an agreement that does not exist and never
self-corrects. A baseline that lags is recoverable; one that leads is not. Under-
reporting is always the correct failure direction, which is why the dispatch layer
reports all-or-nothing per update call.

### 6. Canonical reverse status map, provenance plus single-pass echo suppression, exact-match assignee — from ADR 0029

**The reverse status map is canonical** and must round-trip without oscillation. Any
new outbound status write must have a defined, non-oscillating inbound counterpart.

**Adding an outbound `archived → Done` write requires closing the loop first.** Inbound
maps Jira `Done → closed`, not `archived`, so a bare archived-to-Done push would let
the next inbound pass flip local `archived → closed`. Before shipping it, either add
`archived` to the reverse status map — Jira `Done` on a locally-archived ticket stays
archived — or extend the single-pass suppression to cover it. Naive same-pass
suppression does **not** cover this case, because `archived` is excluded from outbound,
so there is no same-pass outbound status mutation to suppress against.

**Echo suppression is provenance-based and single-pass, not merely idempotent.** The
echo marker and the same-snapshot outbound/inbound computation both remain. Adoption
additionally seeds the baseline, so an adopted issue is not immediately pushed back
(§8).

**Assignee and identity round-trip stays EXACT-match.** Unmappable agent identities
converge to unassigned rather than churning an unsatisfiable assignment every pass.
Assignee is deliberately **not** normalized alongside the text, status, and priority
projection: normalizing it would discard identity candidates used for safe comparison
and can change conflict direction.

### 7. Bound-but-absent is not deleted: deletion is proven only by a bounded direct GET 404 — from ADR 0028

**Snapshot-absence is NOT a signal of deletion.** No destructive or terminal action —
binding retirement, a terminal transition, diffing against an empty dict — may be
driven by a key's absence from the fetched snapshot.

**Deletion is proven only by a bounded direct GET returning 404**, counted to grace. A
transport error defers; a 200 is an alive-overlay.

**The direct-GET budget is bounded and rotated** — `K` per pass, oldest GET first — so
confirmation cost is amortized and never becomes O(all out-of-window issues) in one
pass.

**The level-triggered, binding-driven loop must preserve this discrimination.**
Iterating the binding store and reconciling from currently-observed state is correct
only if "not currently observed" routes to the GET-probe-and-defer path rather than to
a "gone" verdict. Treating out-of-snapshot as deleted would mass-retire the entire Done
backlog on the first pass.

---

## Part C — Bindings

### 8. Binding lifecycle pending → confirmed → retired, adoption gating, and the audit vocabulary — from ADR 0027

**Binding states are `pending | confirmed | retired`.** Confirmed is the steady state;
retired is a reversible soft-delete, not a hard delete.

**Retirement of a confirmed binding requires a confirmed 404 counted to grace** — §7
governs why absence is not a 404. Retirement never follows from snapshot-absence or a
single miss. It writes to `bindings-retired.json` and is recoverable if the marker or
label reappears.

**Identity writes stay audited.** No path — including a unified audit that now *reads*
`bindings.json` — may write a `rebar-id` label outside the three audited leaves.

**Adoption of an unbound Jira issue runs the identity gates FIRST.** The policy is to
adopt, but gated: consult `bindings-retired.json` so a just-retired issue is not
resurrected into a delete-and-re-adopt loop; run the rebar-id audit and the
label-derived-binding suppression so an already-marked issue is treated as bound rather
than double-bound; seed §5's baseline from the adopted fields so the first outbound
diff is empty; and key create-plus-bind on the external id so overlapping passes cannot
double-create.

**The at-most-one-`local_id` and dual-identity invariants remain**, with their per-pass
quarantine caps, as the backstop against identity corruption from any new
binding-driven path.

**The canonical audit vocabulary is `rebar bridge fsck`.** The historical
`rebar bridge-fsck` spelling remains a compatibility alias routed through the same
implementation and auto-initialization policy. This does not alter the read-only
identity boundary above, nor the public `bridge_fsck` library and MCP symbol names.

### 9. Binding-store internal ownership behind one facade — from ADR 0099
Every rule below is load-bearing on code that exists; weakening one is a behaviour
change, not a plan revision.

**Internal ownership behind one unchanged facade.** `BindingStore` remains the single
identity owner; the repository behind it is private, and no collaborator gains a second
door to binding state.

**Open views, not copies.** The repository hands out the *original* dictionaries and the
facade aliases them, so the facade's data *is* the repository's data. The retired key set
and retired-locals index are likewise the same mutable objects retirement and `unretire`
keep in lock-step.

**`all_bindings()` stays SHALLOW.** It returns a fresh outer mapping over the live inner
entries. A caller may iterate it while the lifecycle adds or removes bindings, and the
baseline advance and the rich-text handler both mutate an entry obtained from it and
expect the next save to persist it.

**Three asymmetric failure dispositions, each preserved for its own reason.** The live
store fails **closed**: an unparseable `bindings.json` — typically git conflict markers
from a tickets-branch merge — raises an error naming the offending file and the exact
recovery route. Degrading to an empty store would report every ticket as unbound, so the
very next outbound pass would re-create every issue in Jira. That write is irreversible;
aborting the pass is not. Only an operator can decide how to resolve a merge conflict, so
the reconciler refuses to guess.

**Ordered partial states, not multi-file atomicity.** No multi-file atomicity guarantee
exists and none is claimed. Each file is individually atomic — a temp file in the
destination directory plus an atomic replace, unlinked on any failure including interrupt
and exit, so an aborted pass leaves no litter beside real state. The **order** is the
contract: rotation before live, and the legacy inline floor is scrubbed only once the
sidecar write durably took it (§3).

**The repair runs BEFORE the pass's first remote observation.** This is the decision most
likely to be silently undone, because undoing it breaks nothing visible. The repair is
invoked from the load phase, immediately after the under-lock staleness gate and before
the pass's first remote issue fetch. A pass that fetched first could observe a retired
issue answer 200 and act on it.

**A tombstone is revoked ONLY by an explicit `unretire`.** Liveness observations are not
revocation signals: neither an absent-404 count, nor a later 200 GET, nor clearing
absence, nor a same-key bind-confirm revokes a tombstone, and a historical
live-plus-tombstoned overlap still completes after any of them. Retirement is a soft
delete, reversible only because the tombstone survives; an automatic resurrection would
undo the operator's intent.

**Persistence is NOT publication.** Local durability and tickets-branch publication are
separate layers, and a repository checkpoint never publishes — saving writes and replaces
local files only. If it also committed, every intermediate checkpoint would land its own
tickets-branch commit, turning one pass into a stream of commits and coupling local
durability to git availability. Publication stays behind an exclusive five-file allowlist
in the reconcile helpers.

**Saving stays unconditional.** Dirty tracking would add a correctness question to the one
path whose failure mode is duplicate Jira issues.

## Part D — Coordination

### 10. Bare-ref CAS pass-lock and phase-gate, lease self-healing, commit-chain writes, ref backend only — from ADR 0031
The lock lives on its own git refs — `refs/reconciler/lock` and `refs/reconciler/gate`,
one shared schema. A ref is never in any working tree, is never union-merged, and never
touches the tickets branch.

**Blob schema.** Newline-terminated UTF-8 JSON in stable key order, carrying `holder`,
`lease_secs`, `heartbeat_ns`, and `fence`. `heartbeat_ns` is **diagnostic only**: the
skew-proof expiry rule reads `fence` and the ref OID, never a wall clock. `fence` is a
monotonic progress witness, not a full fencing token — no stale-writer rejection is
claimed of the protected resource. The ticket-event hybrid logical clock is deliberately
not reused: it orders ticket events, and a lock is a different concern.

**CAS contract, and one discriminator.** Acquire is a create-only compare-and-swap; a
mismatch means the lock is already held and is definitive, never retried. Release is a
compare-and-swap against the exact observed OID, and a mismatch is a benign idempotent
success. There is exactly **one** CAS discriminator in the reconciler, generalized to
take a ref name so every existing tickets-branch call site is preserved.

**Fail-closed reads and bounded timeouts.** A read raises on empty, non-UTF-8,
invalid-JSON, missing-field, or out-of-range content, and callers treat that — indeed
**any** read failure — as HELD, never free. Every git subprocess is time-bounded (5s
local, 30s remote); a timeout is likewise HELD.

**Lease self-healing, with no cross-clone clock comparison.** A contender records the OID
and fence, waits one lease measured on its **own** clock, and re-reads; the holder is
dead only if **neither** advanced over that full lease. The holder heartbeats about three
times per lease, so a live holder is never mistaken for dead. The steal is split so it is
testable without real time: a re-read finding the pair unchanged swaps in the new holder,
and a rejected swap means another contender won — it returns nothing and does **not**
also acquire. A free ref is never "stolen", a live holder is never stolen, and a corrupt
blob fails closed. The lease value travels in the blob, so a contender waits the
*holder's* lease. Renewal is owner-only and raises rather than silently retrying into
another holder's lock.

**Reconciler wiring.** The pass lock, its release, and the phase gate each dispatch at one
point. The gate blocks if and only if the target mode exceeds the gated mode, and a
corrupt gate blob fails closed. A daemon heartbeat renews on interval; on a lost lease it
signals and exits, and the pass polls that signal at **per-mutation checkpoints** through
an abort callback. The two failure modes are named apart: one means the lock could not be
acquired, the other that it was held and then lost.

**The ref backend is the only backend.** *(Supersedes ADR 0031 §"Band-aid retirement",
whose transitional text described an expand-contract switch that has since contracted.)*
The file backend and its dispatch are gone; a configured `file` backend is
deprecated-and-ignored with a one-time warning. The outer retry-with-backoff loop that
papered over file-lock contention is superseded and deleted with its helpers, constants,
and config key — self-healing removed the contention it fought. The merge carve-out for
committed lock files is dropped, an init-time migration strips the retired attributes
line, and a one-time startup migration deletes any pre-existing committed lock or gate
file from the tickets branch. Both migrations are idempotent and log-and-continue on git
failure, never aborting. A regression test asserts no reconciler lock path is ever
committed to the tickets branch.

**Every write is a commit chain, on every remote.** The blob-plus-lease protocol was
proven only on GitHub and local remotes; on an S3 store it cannot work, because the helper
refuses a bare object id and accepts only fast-forward updates unless forced — and a
forced push, like a delete, carries no compare. So every write is a commit carrying one
file, parented on the tip the writer read, pushed as a plain non-forced fast-forward from
a temporary ref. GitHub and local remotes refuse a non-fast-forward natively; the S3
helper checks ancestry, takes its per-ref conditional-write lock, re-lists, and refuses if
the ref moved. One mechanism gives one winner on every remote. A refused push is decided
by **re-reading the remote tip**, never by message text: our commit means success, a moved
tip means another writer won, an unchanged tip is a genuine failure. The cost is that the
chain only grows — trimming it would need a forced write, which has no compare on S3.

**Release and clear write markers instead of deleting.** Both markers read as free, and
older rebar builds interpret them benignly, so mixed versions interoperate during an
upgrade. A wedged lock is cleared by writing the released marker over it against the
observed tip; deleting the ref still frees a git remote, but a live holder's next renew
then fails, and on S3 a delete has no compare at all. The lease makes break-glass rarely
necessary. Exact commands live in the operator runbooks, not here.

### 11. Last-pass two-witness status — from ADR 0094
Status uses two separate witnesses with distinct jobs.

**`refs/reconciler/last-pass` is the authoritative rolling completion witness.** It points
at a schema-v1 JSON blob carrying the pass id, environment id, outcome, nullable failure
kind, UTC completion time, and lock-fence provenance. Publication observes the current ref
OID and uses §10's CAS classifier; a moved ref is refetched and retried at most three
times with short sleeps, while transport, authentication, permission, and other non-CAS
failures are immediate hard failures.

**`refs/reconciler/lock` remains the independent live-progress witness.** Status never
treats the fence copied into the last-pass record as proof that a process is still running
— that fence is provenance frozen at completion. The canary observes the live lock twice,
one holder lease apart: an advancing OID or fence proves running, an unchanged lease proves
crashed.

**Write-before-stop ordering.** The mutating process writes its success or
process-surviving hard failure *before* stopping the heartbeat and *before* releasing the
pass lock. If publication fails, the process exits nonzero but still releases the lock. A
successful process exit therefore implies a durable outcome.

**One optional local file carries richer rolling detail**, replaced atomically rather than
accumulated. A consumer may use it only when both its pass id and environment id match the
authoritative ref, so a stale tickets worktree can never override the ref.

**One snapshot, one precedence.** `rebar bridge status` reads one snapshot of the
completion ref, the pause ref, and the live lock. JSON and text apply the same precedence:
paused, running, foreign, failed, stale, healthy. Never-run applies only when neither
completion nor lock exists. Staleness is opt-in through an explicit maximum age; there is
no implicit threshold. Target resolution is the explicit flag, then the environment
variable, then the local environment id — and a missing, empty, or unreadable local id is
an **error** rather than an invented identity. The canary defaults to canonical status and
rejects leases above 480 seconds before waiting, so it keeps headroom inside its fixed
ten-minute job timeout.

### 12. Single retry budget and observe-before-replay, the eleven-disposition taxonomy, and the fuse — from ADR 0103
**One retry budget, owned by the reconciler.** `RetryBudget` is the sole owner of the
physical-invocation budget for a logical mutate operation: at most **three** invocations
and at most **15 seconds** of cumulative sleep. It is fed by an injected clock and a
zero-argument jitter callable, so every decision is deterministic under test and performs
zero wall-clock sleep. This is a deliberate retry-ownership delta against the LLM
transport ADR: transport retry stays for read-only gate calls, but the reconciler's
non-idempotent ticket writes are governed here, not by an SDK retry that cannot see the
logical-operation budget.

**Additive jitter, retained from §13.** The delay floor is `2 ** (retry_index + 1)`
seconds plus jitter — retry 0 in [2s, 3s), retry 1 in [4s, 5s). A provider-supplied delay
is an **authoritative lower bound** that is never shortened; when it exceeds the floor it
wins and the outcome records the provider as the delay source.

**Observe-before-replay after an ambiguous commit.** On a commit-unknown result the
reconciler first *observes* current provider state, then consults a decision table. An
already-desired observation is `recovered`, with no replay; an old-but-conclusive
observation replays only if budget remains, and is otherwise exhausted; a failed or
inconclusive observation is `commit_unknown` and **non-replaying**, mapped to forbidden
replay safety. That mapping is the contract-critical guard against duplicating a
non-idempotent write.

**Provider-neutral outcome values.** The outcome is a frozen value with a stable logical
id across physical invocations, bounded and **allowlisted** diagnostics routed through the
diagnostic-sanitization seam and capped in count and message length so no raw body,
header, or credential leaks, and canonical bytes produced only through the store's
canonical seam so equivalent outcomes serialize identically.

**Deltas apply to the selected route only**, never fanned out across bindings. §§1, 5, and
6 still arbitrate *what* the desired state is; this decision governs *how* one logical
operation is invoked, retried, and observed toward that already-arbitrated state.

**Eleven dispositions projected onto five buckets**, so the pass tally is exact and
byte-stable: `applied` and `already_satisfied` are applied; `recovered` is its own bucket —
an ambiguous commit observed to already hold the desired state, folded into successes yet
counted separately; `retryable_deferred`, `dependency_deferred`, and `scope_deferred` are
deferred; `commit_unknown`, `permanent_failure`, `exhausted_transient`, and
`safety_aborted` are failed; `skipped` is skipped. `commit_unknown` is **failed, never
deferred** — an ambiguous commit is never blindly replayed. The buckets are disjoint and
sum to the mutation count.

**Fuse and containment.** A run of same-scope exhaustion opens a pass fuse at the
**narrowest** scope that explains the failures — ticket, endpoint, tenant, provider, or
global — never wider. Only fuse-eligible work is deferred; a genuine permanent failure
under an open scope keeps its own failed bucket and is **never masked** as deferred, so a
real failure still drives the degraded exit. Independent scopes are never conflated. A
same-scope success arriving after the fuse opened re-closes that scope and is reported
applied, so observed health beats a stale open fuse and the fuse cannot wedge a scope that
has recovered within the pass.

*Landed history.* The original recorded a staged lifecycle in which the outbound-summary
seam shipped dormant and a later stage owned the production cutover, the durable-deferral
consumer, and the retirement of the legacy summary path. Those stages have landed; the
contract above is the steady state.

### 13. ACLI subprocess 429 backoff, retained beneath the operation budget — from ADR 0084

429 handling in the live subprocess loop is an **add-on**: it augments, never replaces,
the general retry policy, and it sits beneath §12's logical-operation budget.

Detection is from stderr markers, so it works regardless of the exit code. A parseable
`Retry-After` in seconds is honoured directly; otherwise the jittered exponential
backoff of §12 applies. Every delay is capped at 60 seconds, so a hostile or huge
`Retry-After` cannot hang a pass, and the attempt ceiling still bounds the total. The
contract is preserved: on a 429 attempt the rate-limit delay *replaces* the uniform
sleep for that attempt rather than adding to it, every other non-zero exit keeps its
current behaviour, exhaustion raises exactly what it raised before, and no new exception
type reaches the call sites. Detection, whether `Retry-After` was honoured or the
jittered fallback used, and the chosen delay are all logged at warning level.

Narrowing retries to a small retryable-code set was rejected as out of scope: it would
drop existing retry behaviour for other codes.

**The implementation is deliberately defensive, and its limits are recorded.** A live
429 could not be forced on demand, so the exact ACLI 429 exit code — and whether ACLI
retries 429 internally — remain unverified in production. Keying off stderr markers and
honouring `Retry-After` only when present means that if ACLI is later found to retry
internally, this layer simply never triggers: no double-retry, and no change needed.

### 14. A proven pagination stall aborts past every fail-open handler — from ADR 0062
**A proven pagination stall is raised as a stall error, and every reader re-raises it PAST
its fail-open handler.** The loud abort is the contract, not a per-reader judgement call:
a reader that catches broadly to fail open must first re-raise the stall type. Two
structural consequences are load-bearing.

**The stall error lives in core**, beside the other backend errors, so both the adapters
that raise it and the core handlers that must re-raise it can name one type. Core must
never import from the adapter packages (§15), so an adapter-local error would be
unnameable at exactly the boundary that absorbs it.

**Every vendor-specific stall type subclasses that core type**, so the single core
re-raise clause catches all of them. Deriving a stall type from a bare runtime error
re-opens the original bug.

Fail-open handlers keep their intended scope and are not weakened: the narrow re-raise
sits *above* the broad handler, so only the provable-stall case escapes. New enrichment
readers inherit the obligation.

## Part E — Vendor seam and the Jira family

### 15. Jira-neutral core plus `adapters/<backend>/`, and the proof-of-seam claim as narrowed — from ADR 0083

**The sub-package boundary IS the vendor seam.** Backend-specific modules live under
`adapters/<backend>/`; the differ, apply, dispatch, store, and invariant core stays at
the package root and is backend-neutral.

**No re-export shims.** Because tests patch module-qualified names, a shim at an old
path would create a patch-binding bug. A moved module has exactly one canonical path.

**The backend port is pinned and live.** The `Backend` facade, its five role protocols
and **two** opt-in capability protocols, and the remote-reference identity type are the
live seam; the differ and apply sites route through that port, the vendor leak-fixes are
single-sourced under the Jira adapter, and the remaining vendor modules were relocated
together with the file-location loader that discovers them. The original pinned three
capability protocols; one was removed as a zero-implementer, unwired capability.

**The migration was phased, and both phases have landed.** Phase 1 moved only the
loader-safe, low-reference vendor subset; phase 2 pinned the port and completed the
relocation. The phasing was forced by two constraints that still bind any future move:
modules discovered by the file-location loader are location-pinned, and modules with a
broad test-patch surface must have every patch string migrated atomically.

**The proof-of-seam claim, as narrowed.** *(Supersedes ADR 0083 §(g)'s proof-of-seam
claim, per [`0055-jira-family-sub-seam.md`](0055-jira-family-sub-seam.md).)* A
backend-agnostic contract suite certifies a backend **only as far as the port is
complete and typed**. Driving a second real backend against a live Jira proved the
limit concretely: the transport port declared six members while the core reached for
**twenty-one** distinct transport members, and twelve of those were absent from the new
transport and declared on no protocol at all. The methodological point is the most
transferable line in that record — a call-form-only AST scan finds only fifteen, missing
six the reconciler reaches by other forms. A contract suite proves conformance to the
port; it cannot prove the port describes what the core requires.

**Standing up a concrete second backend was out of scope for the seam itself.** The seam
is established and proven; building an adapter against it is a delivery epic's work,
enabled by a one-line backend switch once its package is registered. The first real
second backend was Jira Data Center (§16), not the GitHub adapter the seam originally
reserved that role for.

### 16. The Jira-family sub-seam, shared `jira` provenance, and the Data Center horizon — from ADR 0055

**The seam is TWO levels for one vendor family.** The adapters directory holds three
packages, not two: the Cloud adapter, the Data Center adapter, and a shared
`adapters/jira_family/` layer between them and the neutral core of §15. That ADR's
two-layer model, phasing constraints, and backend port are unchanged and still binding;
this layer sits *inside* the adapter half.

**Two contracts parameterize the family, and only two.** Only the axes that genuinely
differ are pinned as contracts — a rich-text codec and a user-identity model. Everything
else is shared code.

**Cloud and Data Center share ONE `jira` provenance identity, deliberately.** They are
the same vendor, so they share the single creation channel, the `jira-` local-id prefix,
and the binding vocabulary. The family is **declared by the backend**, not derived in
the core: each backend carries an identity-family attribute.

**The Data Center investment is deliberately time-boxed.** Data Center receives no new
features and goes read-only on **28 March 2029**. The guidance is explicit: the durable
asset is the **family seam**, not the Data Center adapter — the family layer, its two
contracts, and the identity-family declaration outlive Data Center and are what a third
Jira deployment, or a Jira-shaped tracker, would reuse. Investment in the Data Center
adapter should be **corrective, not expansive**: fix defects affecting real client
instances, and do not build new capability only it can express. When the horizon
arrives, retiring the adapter should be a package deletion plus a registry key, with the
family layer and the Cloud path untouched. If a future change makes that untrue, it has
re-coupled something this seam uncoupled.

**The Jira client dependency is extra-gated**, and Cloud was deliberately **not**
migrated onto it: that would risk the only live-validated path, against production Jira,
to serve a deployment that does not need it.

### 17. Data Center Markdown-to-wiki rendering is a segmenting, verify-then-fall-back renderer — from ADR 0095
Jira Data Center rich text is produced by a **segmenting** renderer that converts only
structurally-safe units through pandoc and passes everything else through byte-for-byte,
rather than converting a whole body in one call. pandoc is delivered by a pinned binary
wheel, so the path is self-contained with no host install. The renderer is **one-way**:
wiki markup is lossy for punctuation-dense prose, so loop prevention must not depend on a
codec fixed point.

Its invariants, each load-bearing:

1. **Nothing is ever corrupted.** A unit that cannot be converted losslessly is not
   converted. After conversion every code fragment of the source must reappear
   byte-identically and in order, or that unit falls back to its original Markdown.
2. **Code is content.** Arrows inside fences, indented blocks, and inline spans are never
   rewritten. Arrow substitution runs on the Markdown *before* pandoc, in one ordered pass
   in which comment delimiters map to themselves.
3. **HTML comments survive exactly.** pandoc deletes them, and the echo marker is an HTML
   comment, so comments are locked early and pass through untouched.
4. **ASCII tables are wrapped, never converted** — exact bytes, once.
5. **Rendering settles.** Repeat passes over the corpus are byte-identical. This requires
   distinguishing *unambiguous* wiki markers from *ambiguous* ones: a rendered bullet would
   otherwise be re-rendered as CommonMark, flipping emphasis and escaping monospace into an
   unstable two-cycle. Unambiguous markers settle a unit on their own; ambiguous markers
   settle it only when it carries no CommonMark-only marker.
6. **Degrading never loses content.** A missing renderer returns the body unchanged and
   warns; a conversion or preservation failure returns that unit unchanged. Reasons are
   fixed tokens and a body is never logged. The warning is emitted per call rather than
   deduplicated, because a once-guard would need mutable module state, which invariant 7
   forbids.
7. **Module state is immutable.**

**The pin is provenance, not preference.** The pandoc version, the wheel version, and the
exact observed conversions for headings, monospace, emphasis, and links are recorded and
asserted by non-skipping tests. The writer emits no colour form for any colour source, so
colour is never a pandoc *output* to pin — it is only pre-existing wiki the inline scanner
must recognize and pass exactly.

**Platform.** The pin is gated on Linux and macOS CI. A platform with no wheel falls back
to the identity path, which is echo-safe.

### 18. Complete Pandoc corpus replay belongs in external integration — from ADR 0096
The verification boundary for §17's renderer is split across two tiers.

**Gerrit Verify uses committed deterministic corpus outputs for the broad oracle.** Every
existing assertion class remains required — cardinality and scrubbing, protected
fragments, tables, HTML comments, settling behaviour, richness floors, prepared-input
alignment, and exact expected bytes. No assertion, source stratum, operating system, or
Python cell is removed.

**Verify retains a bounded real-binary contract.** One deterministic body from each of the
three source strata executes the real product conversion path, with the compact pin,
version, provenance, and non-vacuous-dispatch probes retained. The three bodies are chosen
by mutation sensitivity, so the sample exercises real conversion, protected fallback, and
settling behaviour rather than merely the first fixture entries.

**Complete replay moves to External Integration Tests**, running every corpus unit and
every required settling pass on both supported operating systems, on the weekly schedule
and on manual dispatch. That external job is the complete binary-integration oracle; the
Verify sample is never represented as complete replay.

**Drift fails fast in both tiers.** Verify checks the installed binary's version and the
supported platform's full binary hash before trusting committed outputs, so a pin or
binary change cannot silently keep using old fixtures. External replay compares live
product output against committed bytes on each platform.

**Fixture updates are deliberate and reproducible.** The deterministic generator is the
only supported regeneration path; it records the binary version and platform fingerprints
and emits machine-independent bytes. A renderer or pin change updates the fixture in the
same reviewed change, and before those bytes become a compatibility baseline the author
dispatches External Integration Tests against that exact proposed ref and records the
complete result on both platforms.

**Savings are measured, not assumed.** The attributed CI saving is reported only as a
one-run lower bound. If the result does not reduce the critical path as expected, the
implementation is reverted without weakening the coverage requirements above.

*This narrows §17 on exactly one point:* the requirement that **every** conversion
assertion execute the real binary. §17's production renderer, pin, provenance, platform,
safety, degradation, immutability, and cutover decisions remain authoritative.

## Part F — Operator surface

### 19. Bridge-primary vocabulary; the compatibility adapters are removed and one engine-only flag survives — from ADR 0092

**`rebar bridge preview` and `rebar bridge sync` are the primary operator vocabulary.**
Both normalize into one internal request before the existing pause, phase, lock, pass,
and exit-policy spine.

**Preview is lock-free, and is not a spelling alias for a check mode.** It runs the
ordinary differ once and returns a deterministic manifest of the proposed field changes
without acquiring or mutating the writer locks. Preview and sync retain comparable
manifest envelopes — route, mode, counts, and field-level plan — so an operator can
compare previewed work against the subsequent sync result.

**Selection resolves read-only and fails closed.** The include and exclude selectors
resolve every local id or bound Jira key read-only *before* lock inspection, then narrow
the local, previous-remote, and current-remote differ inputs. A partially unresolved or
subsequently vanished selection fails closed without running the differ or applying
anything.

**A capped sync remains a live mode.** It uses the existing deterministic mutation
ordering and retains a manifest carrying the cap and the full deferred list, even when
the ceiling exceeds the workset. Uncapped canonical sync retains a comparable manifest.

**The compatibility adapters are removed; one engine-only flag survives.**
*(Supersedes ADR 0092 §Decision ¶2, which kept explicit compatibility adapters.)* As a
pre-1.0 breaking change, the top-level legacy reconcile route, the direct-engine
check mode, the Python facade, the MCP tool, and the persisted diagnostics status field
were all removed. `rebar bridge preview|sync|fsck|status` and their library and MCP
twins are the only vocabulary. Exactly one adapter survives: the direct-engine legacy
route's local-id post-filter, restored for live Data Center harness scoping that the
canonical selectors cannot express for not-yet-bound inbound Jira keys. The
no-internal-only-shims ADR cites this record as its public compatibility-contract
carve-out, and that carve-out now covers exactly that one flag.

### 20. Live-validation harness: a deterministic core plus live probes under guaranteed bilateral teardown — from ADR 0085
The live-validation matrix is split into a **deterministic core** driven against the real
reconciler seams, plus a small number of gated probes that actually mutate the remote —
all under one guaranteed bilateral teardown harness. The module is marked so the default
test selection excludes it; it is run explicitly.

**Teardown is partial-setup aware and leak-loud.** An artifact tracker records exactly
which synthetic keys and local ids were *actually* created, so a create that fails halfway
leaves only what landed, and teardown removes precisely those — never a hard-coded list.
Teardown deletes every tracked artifact from **both** systems with bounded exponential
backoff, and runs to completion even if one delete throws, so a mid-teardown error never
strands the rest. On exhaustion the id is appended to a leak log published as a CI
artifact and returned, so the run can exit non-zero: **a leak is loud, never silent.**
Teardown is invoked from a `finally`, so an assertion failure still cleans up, and the
leak log is routed to a temporary directory so a leak never writes into the repository
root.

**One observable pass criterion per scenario, against the REAL seam.** Each scenario
asserts a single observable criterion against the exact code that implements it. The
deterministic scenarios need no network, run green offline, and are the durable,
re-runnable proof.

**Eventual-consistency discipline for live probes.** The remote search index lags both a
create and a delete by an **unbounded** interval, so two rules are encoded in the harness.
Before mutating a just-created issue, poll until it is index-visible — a
create-then-immediate-delete otherwise races the index and surfaces a *confusing*
not-found error that reads as a false permission failure. And **never assert on
post-mutation index state**: the authoritative signal for the delete-permission probe is
the delete's own return value, because a follow-up emptiness assertion or a re-delete is
flaky against an unbounded convergence delay.

**Reporting is one command**, producing a JUnit XML artifact and a derived JSON summary.
Both are gitignored run artifacts; the durable evidence is recorded on the originating
ticket. Operator invocation details live in the runbooks, not here.

## Appendix A — Rejected alternatives, collected

Recorded so they are not re-proposed. Each is stated with the reason that rejected it;
the originals carry the measurements.

**Vendor seam and family (§§15–16).** *Fork per deployment* — two copies of the field
maps and sanitizers drift, and they had already drifted; cross-adapter private imports
are a dependency direction the seam never sanctioned. *A hand-rolled REST client* — a
maintained library already absorbs the deployment-vs-Cloud auth, pagination, and payload
quirks that thousands of users have found, and confining it to an opt-in extra keeps the
cost off every install. *A different client library* — either would work; picking one and
confining it to an extra is what matters. *Generalizing the store vocabulary now* — it
touches 50+ sites and forces a store migration for no user-visible benefit, and the two
deployments are the same vendor, so there is nothing yet to generalize for. *Migrating
Cloud onto the new client* — puts the only live-validated path at risk to serve a
deployment that does not need it. *Moving the location-pinned modules early* — tidier,
but it requires teaching the dynamic loader a new directory and re-targeting every
patch string. *Documenting all of this only in the architecture guide* — without the
rejected alternatives, the fork and the hand-rolled client get re-proposed the next time
a Jira-shaped tracker appears.

**Bridge state (§3).** *Move bridge state off the tickets branch* — ephemeral runners
would lose durable state or need a second persistence service with its own consistency
protocol. *First-wins compatibility reads* — writer order would decide which rotation
stamp survives. *A fixed rollout delay as proof* — time identifies no binary, detects no
forgotten host process, and proves no workflow ancestry. *Normalize assignee with the
other baseline fields* — it discards identity candidates used for safe comparison and
can change conflict direction.

**Pagination (§14).** *Let each reader decide whether a stall is fatal* — that choice
produced three incidents. *Degrade around a stall but flag the snapshot partial* — the
differ treats absence as authoritative, so a flag every consumer must remember to honour
relocates the risk rather than removing it.

**Status (§11).** *Compare heartbeat timestamps across runners* — the same clock-skew
reason that governs §10. *Keep accumulating per-pass health and baseline files* — a
stale worktree is not authoritative and three producers can disagree. *Put rich detail
in the authoritative ref* — it enlarges the cross-clone protocol and its compatibility
burden. *Use the recorded lock fence as running evidence* — it is provenance frozen at
completion.

**Rendering and its verification (§§17–18).** *Whole-body conversion* — echo-safe but
corrupting. *The available Markdown-to-Jira libraries* — none matches the needed
fidelity. *Round-tripping through the wiki reader* — it erodes content across passes.
*Complete replay in every Verify run* — it repeatedly re-tests a pinned external writer
on the critical path. *More aggressive caching or parallelism* — it attacks scheduling
rather than removing redundant third-party work. *Keeping only the live samples* — a
real coverage loss.

**Binding store (§9).** *A journal or write-ahead log*, *a stored-format migration*, *an
eager rewrite on load*, *deep-copied views*, *write-elision or dirty-gating*, and *a
second identity writer* — each rejected for the reason stated in that section.

**Coordination (§12).** *Full or decorrelated jitter* — no herd-avoidance benefit under
two bounded sleeps and a 15-second cap, at the cost of a non-obvious schedule.

**Data contracts (§§1, 2, 4).** *Full live-sandbox end-to-end testing* and *cross-team
contract brokers* — overkill for a single team. *An eleventh generic action payload* —
it would re-open the option-bag the typed union exists to close. *A per-repo project
list* — it fragments the one snapshot the reconciler is built around. *An edit-per-ticket
legacy backfill* — O(tickets) events under one short-budget lock. *Unconditional
capability stamping* — a fail-closed forward guard should not declare a capability the
store does not use.

---

## Appendix B — Provenance

| Consolidated ADR | Section |
|---|---|
| [0004 — snapshot contract](0004-reconciler-snapshot-contract.md) | §1 |
| [0026 — three-way-merge baseline](0026-reconciler-three-way-merge-baseline.md) | §5 |
| [0027 — binding lifecycle](0027-reconciler-binding-lifecycle.md) | §8 |
| [0028 — bound-but-absent is not deleted](0028-reconciler-bound-but-absent-not-deleted.md) | §7 |
| [0029 — bidirectional status and echo](0029-reconciler-bidirectional-status-and-echo.md) | §6 |
| [0031 — ref-lock](0031-reconciler-ref-lock.md) | §10 |
| [0055 — Jira-family sub-seam](0055-jira-family-sub-seam.md) | §16 (and the narrowing in §15) |
| [0060 — bridge-state formats](0060-bridge-state-format-changes.md) | §3 |
| [0062 — pagination stall](0062-pagination-stall-aborts-past-fail-open.md) | §14 |
| [0083 — vendor-adapter seam](0083-reconciler-vendor-adapter-seam.md) | §15 |
| [0084 — ACLI 429 backoff](0084-acli-429-rate-limit-backoff.md) | §13 |
| [0085 — live-validation harness](0085-reconciler-live-validation-harness.md) | §20 |
| [0092 — bridge-primary vocabulary](0092-bridge-primary-vocabulary-compatibility-adapters.md) | §19 |
| [0094 — last-pass two-witness status](0094-reconciler-last-pass-two-witness-status.md) | §11 |
| [0095 — segmenting wiki renderer](0095-dc-segmenting-wiki-renderer.md) | §17 |
| [0096 — Pandoc corpus boundary](0096-pandoc-corpus-verification-boundary.md) | §18 |
| [0097 — many-to-many tracker projects](0097-many-to-many-tracker-projects.md) | §4 |
| [0099 — binding-store ownership](0099-binding-store-internal-ownership.md) | §9 |
| [0103 — operation coordination](0103-reconciler-operation-coordination.md) | §12 |
| [0107 — typed mutation payload](0107-reconciler-typed-mutation-payload-contract.md) | §2 |

## Consequences

- A reader needing the current rule opens one file. A reader needing the evidence — the
  measurements, the incident that forced a decision, the inventory that sized a
  migration — opens the original, which is unchanged and now labelled as the evidence
  record.
- Overrides are visible at the point of override rather than discoverable only by
  reading both files. Three are recorded here: §10 over the ref-lock ADR's transitional
  backend text, §15 over the vendor-seam ADR's proof-of-seam claim, and §19 over the
  vocabulary ADR's compatibility-adapter paragraph.
- Landed transitions are stated as steady state, so a phased plan is no longer mistaken
  for pending work.
- This record must be updated when any consolidated decision changes. A new decision in
  this subsystem gets its own ADR and a section here; it does not get appended silently
  to an archived original.
