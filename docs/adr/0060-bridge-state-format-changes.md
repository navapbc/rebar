# ADR 0060 — Keep bridge state committed, but make its formats cheap

- **Status:** Accepted (epic `0303-692c-55dc-4a18`; story `abb6-bf87-89de-4042`)
- **Date:** 2026-08-03

## Context

The Jira reconciler runs on ephemeral GitHub-hosted runners. Its durable cache therefore has to
travel with the `tickets` branch: a local-only cache disappears between runs, while reconstructing
all prior state from Jira would discard the previous-snapshot and rotation information used to
make reconciliation safe and bounded. Epic `6c9c` evaluated moving the cache off-branch and
rejected that direction (epic `6c9c-811f-6dc4-40e7`); the useful levers are the cache's content
and churn, not its location.

That distinction matters because `.bridge_state` dominated repository growth. Measurements
recorded in session log `1cb6-d553-6565-49b4` found a pre-change marginal packed cost of **12.79
KB per changed `bindings.json` version**. Removing the never-read baseline timestamp reduced the
same measurement to **1.74 KB**. The full `prev_snapshot.json` also cost about 4.5 MB/day, even
though **218 of 399** consecutive real snapshot pairs had identical Jira-key sets.

Four related changes alter the committed representation:

1. **A1 — remove `baseline_advanced_at`.** `BindingStore.set_baseline` now writes only when the
   baseline value changes. The timestamp was written for most bindings every pass and had no
   production reader.
2. **A2 — relocate `last_get_pass`.** GET-rotation stamps move from entries scattered throughout
   the large, sorted `bindings.json` file to `.bridge_state/get_rotation.json`.
3. **A3 — normalize baseline text fields.** Baseline `description`, `status`, and `priority` are
   stored in the scalar projection already consumed by live comparison instead of retaining the
   larger Jira vendor objects.
4. **A4-2 — narrow `prev_snapshot.json`.** The previous snapshot becomes a mapping-shaped key set,
   `{jira_key: {}}`, rather than a copy of the full Jira payload.

These are shared-store changes: old and new writers can overlap during deployment, and a bad
rollback can resurrect a removed representation or lose the state that makes a later pass
efficient. They need one explicit compatibility and rollback contract.

## Decision

### Keep `.bridge_state` committed

The reconciler continues committing `.bridge_state` to the `tickets` branch. This preserves state
across ephemeral workflow runners and keeps the branch self-contained. We reduce write
amplification by canonicalizing values, separating small high-churn data from large stable data,
and avoiding writes when observable state is unchanged.

This is an application of rebar's existing expand/contract migration practice, not a new migration
framework. `docs/migrations.md` describes the committed store-compat reader-first gate and the
`352b` legacy-signature-mirror retirement; epic `6c9c-811f-6dc4-40e7` used the same pattern for
its `file | ref` cutover. A2 follows that house pattern with a domain-specific merge rule.

### A1: delete the timestamp and guard unchanged baselines

`baseline_advanced_at` is deleted from new writes. Historical copies are harmless because no
production reader consumes the field. `set_baseline` compares the canonical new value with the
stored value before serializing, so two unchanged passes keep `bindings.json` byte-identical and
do not create a commit.

Rollback is mechanically safe: reverting A1 only resumes writing an ignored timestamp and restores
the former repository churn. It does not recover or change reconciliation semantics.

### A2: expand with dual write/read-max, then contract sidecar-first

A2-1 is the expand phase. New binaries write each rotation stamp to both the legacy inline field
and `get_rotation.json`, and readers use the chronological
`max(sidecar_stamp, legacy_inline_stamp)`. The fixed-width pass identifiers sort chronologically.

`max` is required. A first-wins lookup can select stale state whenever an old writer advances only
the inline field or a new writer advances the sidecar before an older binary runs. Taking the
maximum makes either order converge, while dual write keeps old readers functional during the
expand phase.

A2-2 is the contract phase. A successful save:

1. merges every staged inline value into the in-memory sidecar with chronological `max`;
2. atomically persists `get_rotation.json`; and
3. only after that persistence succeeds, removes inline `last_get_pass` fields and atomically
   replaces `bindings.json`.

`set_last_get` continues staging the inline maximum in memory as a failure floor. If sidecar
persistence fails open, `bindings.json` retains that floor. If the process fails after the sidecar
replace but before the bindings replace, the sidecar already contains the maximum and the old
bindings still contain its legacy floor. A later new-writer pass converges either state to
sidecar-only form without losing rotation progress.

The A2-2 deployment gate uses direct evidence rather than elapsed time:

- A2-1 is merged on `main`.
- A completed `reconcile-bridge.yml` run executes at a SHA descending from the A2-1 merge.
- The host probe binary is explicitly refreshed from a descendant build; its executable path and
  source/version are recorded, and no older reconcile/probe process remains.
- The old-writer/new-writer integration oracle proves that a later legacy inline stamp is promoted
  into the sidecar and scrubbed on the next new pass.

A pass-record version stamp cannot establish this gate. The reconciler pass record contains a pass
id, mutation count, and completion status, but no executable or source SHA, and it says nothing
about the separately refreshed host probe. Workflow ancestry plus an exact host executable proves
the actual writer set directly.

A2-1 is fully reversible because it keeps dual writing. After A2-2, the supported rollback floor is
the A2-1 merge: every A2-1-or-newer binary reads both representations. Rolling back below A2-1 is
prohibited after the direct deployment gate. Such a binary would lose only the rotation
optimization—not bindings or reconciliation correctness—and would rebuild coverage within
`ceil(N/K)` passes, but relying on that bounded degradation is not the supported rollback path.

### A3: store the comparison projection, except for assignee

Baseline `description` is stored as normalized text, while `status` and `priority` are stored as
their Jira `name` strings. The read/comparison path already canonicalizes both the old vendor-object
shape and the new scalar shape, so the change is idempotent and mixed-version reads are compatible.
Rollback is safe: an older writer can restore the object shape and a new reader canonicalizes it
again.

`assignee` deliberately remains raw. `_assignee_candidates` compares the scalar value and all
available identity forms from the vendor object. Collapsing that object to one string would discard
candidate identities and could turn a concurrent Jira-side assignee edit into a local-wins
overwrite. The size optimization does not justify that data loss.

### A4-2: persist only previous Jira-key membership

`prev_snapshot.json` stores `{jira_key: {}}`. The mapping shape is intentional: consumers use
mapping membership and `value or {}` behavior, so a Python set or another scalar representation is
not an equivalent contract. A first new-format pass can read an old full snapshot and writes the
key-set form at the end.

The cutover is supported by a real-snapshot corpus comparison over at least four pairs. Both the
forward full-to-key-set direction and reverse key-set-to-full rollback direction produced zero
difference in non-empty effects reaching Jira. The existing call-site filter remains the
load-bearing protection against prev-only synthetic mutations.

Rollback to the full payload shape is therefore supported by the recorded reverse corpus result.
Deleting `prev_snapshot.json` is not the default rollback: an empty previous snapshot can re-derive
inbound creates. If deletion is ever required, it must be followed by a cap-zero, no-write pass that
proves no unexpected inbound creates before writes are re-enabled.

## Consequences

- The tickets branch remains the complete durable state source for ephemeral runners.
- A1 and A3 reduce the large-file payload and make unchanged state byte-stable.
- A2 isolates the small rotating write set and retains mixed-writer self-healing after the legacy
  write is retired.
- A4-2 removes the largest remaining snapshot payload and eliminates commits for the 218/399
  measured pairs whose key membership did not change.
- Rollback is not one blanket promise: A1, A2-1, and A3 are reversible directly; A2-2 has the A2-1
  floor; A4-2 relies on its bidirectional corpus evidence and must not be “rolled back” by blindly
  deleting the snapshot.
- Deployment readiness is established by observed writer identity and behavior. A fixed 24-hour
  wait is neither required nor accepted as a substitute for that evidence.

## Rejected alternatives

**Move bridge state off the tickets branch.** Ephemeral workflow runners would lose durable state,
or require a second persistence service and its own consistency protocol. Epic
`6c9c-811f-6dc4-40e7` already rejected that trade.

**Use first-wins compatibility reads.** Writer order would decide which rotation stamp survives;
an old writer could advance the legacy value while a new reader continued from stale sidecar state.

**Use a fixed rollout delay as proof.** Time does not identify the binary that actually ran, does
not detect a forgotten host process, and cannot prove workflow ancestry. Direct runtime evidence is
both faster and stronger.

**Normalize assignee with the other baseline fields.** This would discard identity candidates used
for safe comparison and can change conflict direction, unlike the text/status/priority projection.

## Evidence

- Epic and dependency plan: `0303-692c-55dc-4a18`.
- Measurement and design trail: session log `1cb6-d553-6565-49b4`.
- A1: `f7ee-572b-1eb0-4af8`, merged as `750a56b3b004f3c3f4cc1b456d3c56bd7809b87d`.
- A2-1: `9594-1f1c-bcd1-4774`, merged as
  `e9a25ab84729be0bc9769614f8faa637a1411fac`.
- A2-2: `3592-0d1e-2bd6-4e22`.
- A3: `5fa1-aab2-e6b5-4fad`, merged as `90dd7193a76816dc3f02a14032fe77964eabd026`.
- A4-2: `2cc0-da4a-d736-4ba2`, merged as
  `6cdd4b00d9ee90b2ffe3e4da4780f81de127d7fd`.
- Migration prior art: `docs/migrations.md` and epic `6c9c-811f-6dc4-40e7`.
