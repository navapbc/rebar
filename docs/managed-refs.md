# Managed-reference provenance — the removal-sync gate

> The reusable primitive that lets a **local removal** of a cross-system reference
> (detach a parent, unlink a dependency) propagate to a peer system **without being
> resurrected** by the next inbound pass. Built once, provider-agnostic, so the same
> pattern serves Jira today and Linear / GitHub Issues later.

## The problem it solves

rebar syncs tickets bidirectionally with a peer (Jira). Two differs run each pass:
an **outbound** differ (local → peer) and an **inbound** differ (peer → local). For a
field that is only ever *added* — never cleared — these two fight whenever you remove
something locally:

1. You unlink a dependency locally (`rebar unlink A B`).
2. The outbound differ, being additive-only, emits nothing → the peer keeps the link.
3. Next pass the inbound differ sees the still-present peer link, finds no matching
   local dep, and **re-adds it** → your removal is silently reverted.

This "churn" is the symptom of an **asymmetric sync of removals**: SET propagates,
CLEAR/REMOVE does not. It bit both the parent epic-link (bug `drum-ruler-suit`) and
issue-links/deps (bug `wake-inn-parse`).

## The decision a removal gate must make

When the outbound differ sees a reference **present on the peer but absent locally**,
it must choose:

- **Propagate the removal** — delete it on the peer — *if we deliberately removed a
  reference we used to manage*; or
- **Adopt it inbound** — pull it into local, leave the peer alone — *if the peer added
  a reference we never managed* (e.g. a human created the link directly in Jira).

Getting this wrong either resurrects your removals (churn) or clobbers human-created
data. The discriminator is provenance: **did our side ever manage this reference?**

## `managed_refs` — a compaction-surviving provenance projection

That question is answered by **`managed_refs`**, a field the **reducer** maintains in
each ticket's `compiled_state`:

> the **strictly-monotonic union** of every logical reference this ticket has *ever*
> managed.

A logical reference is normalized, **provider-agnostic**, as `(kind, target)`:

| part     | meaning                                                                    |
|----------|----------------------------------------------------------------------------|
| `kind`   | `parent`, or a link relation: `blocks` / `depends_on` / `relates_to`       |
| `target` | the **local** ticket id the reference points at (never a peer/Jira key)    |

Each provider maps a local ref to its own entity at sync time, so the projection is
reused unchanged by future peers.

### Why it lives in the reducer / `compiled_state`

A naive "ever-seen" set projected over **raw event history** (as `local_label_intent`
does for labels) **fails closed across `compact_ticket`**: compaction collapses the
log to a `SNAPSHOT` whose `compiled_state` holds only *current* refs, so a removal
performed at/after the compaction boundary would be re-resurrected (the log no longer
proves we managed the ref). Because `managed_refs` is a `compiled_state` field, it is
restored by `process_snapshot` and **survives compaction** — closing that durability
hole.

### Fold points (reducer)

`managed_refs` is folded — never reduced — in `rebar.reducer._processors`:

- `process_create` — a parent set at creation.
- `process_link` — every `(relation, target)` (the matching `process_unlink` removes
  the dep but **not** the managed ref → monotonic).
- `process_edit` — a re-parent, *including* an inbound-ADOPTED parent the reconciler
  applies (so adopting a peer ref makes it ours; a later local detach then propagates).

The fold is **idempotent** (`managed_refs` is a set, serialized as a sorted
`[kind, target]` list for SNAPSHOT byte-stability), so it is safe under at-least-once /
duplicate event replay.

### Migration (pre-feature tickets)

A `SNAPSHOT` written before this field existed carries no `managed_refs`.
`process_snapshot` **seeds** it from the restored current `parent_id` + `deps` — those
refs are treated as managed (a ref already in `deps` was created locally or
inbound-adopted; rebar owns it). Forward-compatible: older clones preserve-and-ignore
the field. **Known limitation:** a removal performed *before* this feature shipped is
already gone from current state and a compacted log, so it cannot be recovered — only
post-feature removals self-heal.

## The shared gate

`rebar.reducer._managed_refs.should_propagate_removal(kind, target, local_ticket)` is
the single, provider-agnostic decision both the parent and link outbound paths call:

```python
should_propagate_removal(kind, target, local_ticket) -> bool
# True  -> we managed this ref and it's now absent locally -> DELETE it on the peer
# False -> never managed (adopt inbound) OR managed_refs absent/empty -> no delete
```

**Fail-open by construction.** A missing/empty `managed_refs` returns `False`, degrading
to additive-only: the gate never fires a delete it can't justify, so a transient or
absent projection only **delays convergence** — it never fires an irreversible wrong
peer delete or clobbers a human-created ref. This is also the **back-out posture**:
disable the fold and the gate becomes a safe no-op.

## Same-pass coordination

A local removal that the gate propagates must also **suppress the inbound re-add** of
the same reference in that pass (the local change is fresher than the differ snapshot,
so local wins — "remove-wins" at entity granularity). See the inbound differ's
bidirectional suppression.

## Reclamation (future)

`managed_refs` is strictly monotonic; it is never pruned in the reducer (pruning in the
UNLINK path would re-open the resurrection window). A **safe** prune needs the *peer*
snapshot — a ref is reclaimable only once it is absent **both** locally and on the peer
— and so must be a reconcile-time step emitting an explicit prune event. That is a
documented **future hook**, deliberately out of scope for v1. Per-ticket ref cardinality
is small (tens), so unbounded growth is not a practical concern at this scale.

## Extending to a new peer (Linear / GitHub Issues)

The provenance primitive and the gate are provider-agnostic. A new provider supplies
only: (a) a map from its peer entity to a local `(kind, target)` ref, and (b) outbound
apply calls for ADD and DELETE. It then consumes `should_propagate_removal` exactly as
the Jira reconciler does — no new provenance logic.
