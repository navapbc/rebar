# Reconciler comment-echo history reclaim

This runbook prepares and, only in a separately approved quiet window, coordinates removal of the
six proven reconciler comment-echo groups from both Jira and the `tickets` branch. It deletes one
rollout-created Jira duplicate per group, preserves the causally proven Jira survivor, removes the
matching and other manifest-enumerated local echo aliases/snapshot material, and publishes the
rewritten branch only after both sides agree. It is incident-specific. It is not a general
duplicate collector or retention policy.

The preparation ticket is `irongray-unlucky-discus` (`93c4-2b9f-749a-4243`). The destructive
execution ticket is `synonymous-senatorial-cuscus` (`6e2f-2814-4f92-4873`). Preparation must stop
and notify the operator when the package is ready. It must not pause writers, publish a lease,
mutate Jira, or update authoritative history. A new, explicit operator approval on the execution
ticket is required before any quiet-window step.

This procedure deliberately adds no recurring job, threshold, ratchet, schedule, or workflow
gate. Footprint measurements are observations, not pass/fail policy. The reclaim removes the
proven anomalous payload; it does not claim that high commit volume, enrichment retention, or
small-file allocation slack will disappear.

## Why the older reclaim procedure is not reusable unchanged

The 2026-08 bridge-cache reclaim preserved the old HEAD tree and changed only commit ancestry.
That assertion was correct for cache removal and is wrong here: this reclaim intentionally removes
manifest-enumerated COMMENT events and their derived snapshot representations. The valid semantic
delta is narrower than tree equality:

- one local echo whose Jira identity is still live survives in each of six groups;
- only enumerated non-survivor COMMENT aliases disappear;
- only enumerated active and retired snapshots lose those comments, source UUIDs, and authorship
  rows, with counts recomputed;
- the final `.store-compat.json` alone receives the new epoch;
- every other path, event byte, signature, bridge binding, field, link, status, attestation, and
  audit record remains equivalent.

Keep the earlier reclaim's proven safety lessons: direct writer observations instead of elapsed
time, a complete independently restored bundle before publication, an exact force-with-lease,
epoch-mismatch probes against the deployed runtimes, re-cloning persistent writers, and an
independently rehearsed rollback. Do not restore its identical-HEAD-tree oracle.

The incident RCA also proved the echo loop is no longer active. The deployed comment-ID and marker
repairs converged after posting run `32447039101`, import run `32447697242`, and the following
zero-comment pass. The purpose here is to remove newly reachable historical damage; do not replace
this procedure with another reconciler fix, a repack, ordinary compaction, or commit batching.

A zero-*total*-mutation dry-run oracle is also incorrect for this incident. The frozen old source
and rewritten candidate both produce the same three outbound, description-only updates for
REB-1567, REB-1931, and REB-2605, with empty comment, label, and link collections. That drift
predates this rewrite: `rich_text_cutover = "cloud"` was enabled by commit `5b5008bbfbec` for
`paramount-ducal-snake` (`52bf-bc40-3fcb-4729`), after the codec gate in `2664fcb237dc` and before
the soft-wrap migration precedent in `992b0656d40e`. The retained bridge baselines have the older
description shape. The earlier baseline repair `crusty-jovial-wren` (`5fa1-aab2-e6b5-4fad`)
deliberately proved baseline-shape equivalence without adding a migration pass or weakening the
comparator; preserve that decision here. Do not rewrite bindings, alter rich-text comparison, or
manually converge those Jira descriptions as part of this incident.

The safe oracle is differential: the old source and candidate must have byte-identical
non-collection plans, while the candidate must have no comment, label, or link operation. Hash the
complete canonical `result.plan` mutation list, not the whole preview result. Additive preview
metadata such as `lifecycle_intents` carries operation-scoped observation identity and is inspected
separately; folding it into the mutation-authority digest would create false drift between runs.
The preparation rehearsal's complete canonical `result.plan` digest is
`8004f77be6b1afe1da1dafde1c55a14ab0c34a40b41bb9c0a9fee8b29eaf80fa`; it is evidence for the
frozen preparation tips, not authority to reuse stale artifacts in the execution window. A fresh
window regenerates, independently inspects, and pins its own digest before Jira DELETE. The digest
allows comparison only: it does not authorize applying the description updates.

## Fixed incident authority

The manifest must bind these six pairs exactly. The left ID survives in Jira and in the rewritten
store; the right ID is the only Jira DELETE target. All twelve comments must have the manifest's
exact issue, normalized body, raw ADF, and bot account
`712020:6471376f-4e5e-4ed2-8c05-330827bc387e`.

| Jira issue | Surviving ID | Delete ID |
|---|---:|---:|
| REB-1567 | 963510 | 997143 |
| REB-1567 | 963511 | 997144 |
| REB-1567 | 963512 | 997145 |
| REB-1931 | 963788 | 997255 |
| REB-1931 | 963789 | 997256 |
| REB-2605 | 964272 | 997293 |

The six delete IDs are bound to posting run `32447039101`, import run `32447697242`, and import
commit `ef716a48cee23bafe155ed7cb256ccb49bf316e0`. This causal authority is load-bearing: never
substitute oldest/newest ordering, body equality alone, or the current unknown-ID bridge sentinel.

## Artifacts and custody

Create one operator-owned artifact directory outside every Git repository. It contains sensitive
Jira material and rollback capability; mode it `0700` and do not commit it. Record paths and
SHA-256 values on the execution ticket, not the artifact contents.

Required artifacts are:

1. the raw, paginated pre-cleanup Jira comment inventory for exactly REB-1567, REB-1931, and
   REB-2605;
2. the complete Git bundle from `prepare_reclaim_backup.py`;
3. that helper's backup manifest and full remote heads/tags census;
4. the canonical immutable incident manifest from `comment_echo_reclaim_manifest.py`;
5. `jira-before.json`, `jira-cleanup-plan.json`, the append-only
   `jira-cleanup-journal.jsonl`, and `jira-after.json` from
   `cleanup_comment_echo_jira.py`;
6. the canonical old-to-new commit map from `reclaim_comment_echo_history.py`;
7. one old-source receipt, two identical candidate receipts, and their canonical differential
   receipt, including the independently inspected exact-plan digest;
8. before/after verification and footprint receipts; and
9. rollback and post-publication receipts.

Never place these under the source clone. Neither helper overwrites an existing artifact. A refusal
is resolved by preserving the failed evidence and choosing a new empty artifact directory.

The Jira inventory is a complete content/provenance backup, but Jira Cloud cannot recreate a
deleted comment with its original ID. Git rollback is byte-exact; Jira recovery after DELETE is a
compensating operation with new IDs. Treat that asymmetry as irreversible authority at the DELETE
checkpoint, not as something the Git bundle can undo.

## Preparation without a writer pause

These steps are read-only to Jira and authoritative Git refs. A concurrent ticket write is normal:
the before/after remote census will reject the attempt, after which preparation starts again from a
new observed tip. Do not stop writers merely to make a rehearsal pass.

### 1. Establish the observed source

Use a new standalone, unfiltered clone of `origin/tickets`; do not reuse the project's partial
source checkout, its shared `.tickets-tracker`, a linked worktree, or any persistent writer clone.
Prove and record all of the following:

- `HEAD`, the local `tickets` ref, and live `refs/heads/tickets` resolve to the same exact OID;
- `git status --porcelain` is empty;
- `git rev-parse --is-shallow-repository` is `false`;
- no `remote.*.promisor` or `remote.*.partialclonefilter` key is active;
- `.git/objects/info/alternates` is absent or empty; and
- `git ls-remote --heads --tags origin` is captured byte-for-byte.

Use that exact OID as `SOURCE_TIP`. Counts and paths are measured at this tip; never copy the
historical `760` count or `247,719,284`-byte estimate into an execution assertion.

### 2. Capture a complete Jira inventory

Use authenticated Jira Cloud REST v3 GET requests only. Fetch every comment page for the three
bound issues. Preserve each page's `startAt`, `maxResults`, `total`, comment `id`, raw `body`, and
author `accountId`. Follow pagination until the number of distinct fetched IDs equals the reported
total. Do not use the ACLI flattened comment result as completeness evidence because it discards the
per-page totals.

Body comparison must use the same ADF-to-plain-text normalization as the deployed reconciler's
inbound path (`normalize_rich_text`), never Jira-rendered HTML and never an independently invented
renderer. The inventory artifact binds both the raw page material and the normalized body used for
the incident hash. The manifest builder rejects gaps, overlaps, duplicate IDs, inconsistent totals,
missing identities, and any target body that does not have exactly the fixed survivor/delete pair.

This step is a read, not a cleanup. Each group must have exactly two authorized Jira copies with the
IDs in the fixed-authority table. A third copy, a missing or edited pair member, an unexpected
author/body/issue, or disagreement with local provenance is an abort. Preparation may run
`cleanup_comment_echo_jira.py` without `--execute` to produce an independently inspectable GET-only
plan, but must not create a journal or send DELETE.

### 3. Create and independently restore the backup

Run `prepare_reclaim_backup.py` against `SOURCE_TIP`, with its bundle and manifest paths in the
artifact directory. It snapshots all remote heads and tags, names the exact old tip with a temporary
local helper ref, creates the bundle, verifies it, fetches every recorded bundle ref into an
independent throwaway bare repository, compares the restored OIDs, and removes its helper refs.

Do not proceed unless:

- the backup manifest's `old_tip` equals `SOURCE_TIP`;
- exactly one bundle ref ends in `/old-tip` and resolves to that OID;
- the independently restored commit and tree match; and
- the live remote census still equals the captured census.

### 4. Build the immutable manifest

Run `comment_echo_reclaim_manifest.py` with the exact source ref and tip, the verified bundle and
backup manifest, the complete Jira inventory, and a new off-repository output path. Record its
digest and logical delta.

Inspect the canonical JSON. It must contain exactly the six ticket/body-hash groups recorded on the
preparation ticket. For each group, verify the fixed Jira survivor/delete pair, causal rollout
authority, the protected bridge positions and real mapped IDs, one local survivor, every removed
event path/alias/OID/SHA/UUID/timestamp, and every affected snapshot old/new OID and removal set.
The bridge blob itself is evidence and is never transformed.

Any source-tip, blob, backup, Jira, bridge-map, snapshot, sharing, or remote-census ambiguity is an
abort. Do not hand-edit and re-digest a manifest. Regenerate it from a fresh clone and fresh Jira
capture.

### 5. Build the local candidate

Run `reclaim_comment_echo_history.py` with the standalone clone, immutable manifest, a new local
candidate ref, and a new off-repository commit-map path. The script has no push option or remote
update path. It uses a staging ref, verifies the complete candidate, then creates the requested local
output ref; on failure it removes staging/output refs and the incomplete map.

The candidate must pass all of these before it can be called ready:

- the old-to-new map covers every source commit exactly once;
- commit count, merge count, parent topology, author, committer, and message match through the map;
- per-commit old/new tree differences are limited to manifest aliases, enumerated snapshots, and
  final-tip `.store-compat.json`;
- retained event OIDs/bytes/signatures and bridge-state bytes are identical;
- removed event UUIDs and snapshot ledger/source/comment deltas equal the manifest exactly;
- no rewritten snapshot cites a removed UUID;
- `git fsck --strict` is clean; and
- source/local `tickets` and every remote head/tag still equal their initial OIDs.

### 6. Replay the whole store and measure without thresholds

Materialize old and candidate tips in separate disposable worktrees. Replay every ticket with the
current reducer. Unaffected ticket states must be identical. For the three targets, subtract only
manifest-enumerated echo comments from the old state, including the corresponding authorship rows
and counts, and require exact equality with the candidate. Verify one live-Jira-backed echo remains
per group and `.bridge_state/bindings.json` is byte-identical.

Measure both tips using the same commands and filesystem. Record, without a pass threshold:

- reachable pack bytes from `git pack-objects --stdout --revs`;
- logical checkout bytes;
- allocated checkout bytes;
- file count and the observed logical-to-allocated slack;
- Git-directory bytes; and
- a new standalone whole-clone footprint.

Do not describe logical event/snapshot bytes as pack savings or allocated-disk savings. Compression,
delta selection, filesystem block size, and retained reachability make those different quantities.

### 7. Rehearse rollback and stop

Run the digest-confirmed Jira cleanup against a stateful fake seeded from the frozen inventory.
Require the exact canary order, one attempt per target, complete intent/outcome journal, exact six
survivors, all non-target bytes/identities unchanged, and a successful crash/resume rehearsal.

Publish the Git candidate only to a disposable bare repository. Restore the bundle's exact
`/old-tip` ref there with an exact lease, then require the old commit/tree, complete ref census,
replay, and `git fsck --strict` to match. Reapply the candidate in the disposable repository to
prove the forward procedure is repeatable.

Against the simulated post-cleanup Jira inventory, run one production-equivalent dry run from the
old source and two from the disposable candidate. All three runs are cap-zero: they must invoke no
transport write, report zero applied/failed operations, and leave HEAD, tree, bindings, and every
tracked byte unchanged. Canonicalize the complete candidate plan, independently inspect every
target/action/field/payload, then pin its SHA-256 in the executable rehearsal before accepting a
second candidate run. Require all of the following:

- the candidate has no nonempty `comments`, `labels`, or `links` collection and no inbound COMMENT
  import;
- after removing only those three collection keys, the old-source and candidate plans are exactly
  equal;
- every remaining entry is the independently pinned incident plan--currently exactly one outbound
  `update` for each bound target, with `description` as its only changed field--and the complete
  candidate plan matches the pinned digest;
- the two candidate results, read-call sequence, stdout/stderr digests, HEAD/tree/binding/tracked
  byte observations, and complete plans are identical; and
- the old and candidate binding files are byte-identical.

A candidate plan with zero comment actions is the comment-convergence proof. A nonzero but
old-source-identical description projection is pre-existing drift, not cleanup authority. Do not
apply it, rewrite its baseline, or weaken the comparison to make the rehearsal pass.

Record explicitly that the Git rollback is exact while deleted Jira IDs are not restorable. The
execution decision therefore prefers a verified forward correction after Jira DELETE; any
compensating Jira recreation requires separate approval and a newly generated manifest.

Record the preparation receipts on `irongray-unlucky-discus`, then stop and notify the operator that
the quiet-window package is ready. Do not claim or start the execution ticket yet.

## Quiet-window execution (new approval required)

Everything below is forbidden during preparation. Begin only after the operator explicitly approves
the execution ticket and the ticket is reviewed and claimed.

### 1. Disable the GitHub Actions reconciler, then prove every writer is quiescent

The live reconciler is the GitHub Actions workflow `.github/workflows/reconcile-bridge.yml`. From an
operator shell whose `gh` identity has `actions: write`, disable it first. Disable the heartbeat
canary too; otherwise the intentional pause can create alert-ticket writes.

```sh
gh workflow disable reconcile-bridge.yml --repo "$GH_REPO"
gh workflow disable reconcile-bridge-canary.yml --repo "$GH_REPO"
gh api "repos/${GH_REPO}/actions/workflows/reconcile-bridge.yml" --jq .state
gh api "repos/${GH_REPO}/actions/workflows/reconcile-bridge-canary.yml" --jq .state
```

Both API reads must return exactly `disabled_manually`. Then query both workflows' run lists until
there is no `queued`, `in_progress`, `waiting`, `requested`, or `pending` run. Disabling first is
load-bearing: an in-flight continuous pass may attempt one final redispatch, which must be rejected
while the acting workflow remains disabled. Record every drained run ID and conclusion.

Enumerate all other current ticket writers; the following list is a minimum, not future authority:

- drain the deployed review bot, require `/health` `in_flight == 0`, and prove no current Gerrit
  patchset can emit a late review ticket event;
- stop local/remote agent sessions and every `rebar-mcp` process, with an explicit operator
  zero-session/stopped-MCP attestation;
- disable or drain every other GitHub workflow that can write `tickets`, including the current
  compaction and alert-ticket workflows;
- pause host/AWS scheduled services that can write tickets or replace a runtime while paused; and
- inspect `refs/reconciler/lock` and `refs/reconciler/gate` with the exact deployed lease parser.
  Require no live lease or advancing gate. A stale ref is evaluated by its signed lease fields; do
  not guess from age and do not delete either ref by hand.

There is no fixed sleep that proves quiescence. Use workflow state, run queues, process state,
current-patchset state, and parsed lease state. Capture the final `tickets` tip only after all drains
complete. Any later write keeps the workflows disabled and restarts artifact generation at the new
tip.

Record the executable path, package version, source/merge SHA or image digest for every persistent
writer. Using those exact deployed runtimes, prove on temporary real clones that an old-epoch writer
refuses a new-epoch remote at both union/recovery paths. A unit test or a newer local binary is not a
deployment receipt.

### 2. Freeze and verify both sides before the irreversible delete

Capture `OLD_TIP` and the complete live heads/tags census. In a new standalone unfiltered clone,
regenerate and independently verify the Git bundle, backup manifest, complete Jira inventory,
canonical incident manifest, local candidate, commit map, replay, fsck, measurements, epoch probes,
and disposable Git rollback/forward rehearsal. Do not reuse the preparation candidate.

Run `cleanup_comment_echo_jira.py` once without `--execute`, against the same manifest and a new
`0700` off-repository artifact directory. It is GET-only and must produce byte-inspectable
`jira-before.json` and `jira-cleanup-plan.json`. Require the plan digest, six actions, fixed pairs,
and canary order `REB-2605`, `REB-1931`, `REB-1567` to agree with the independently captured Jira
inventory. There must still be no journal or `jira-after.json`.

Before Jira DELETE, repeat the old-source/candidate differential rehearsal from preparation against
that captured inventory with only the six delete identities removed in memory. Independently
inspect and pin the current complete canonical `result.plan` digest. It must remain in the proven
structural class: zero comment/label/link actions on the candidate and an old-source-identical
non-collection projection containing no target or field outside the prepared description-only set.
If the deployed runtime emits additive preview metadata, inspect it outside that digest. For
`lifecycle_intents`, require the expected schema, target identities, dispositions, dependencies,
and intent kind/target projection; ignore only its operation-scoped observation-version values when
comparing old source to candidate, and require the two candidate metadata sections to be identical.
An unknown additive section or any other divergence is a preparation restart, not a reason to
expand the allowlist inside the window.

Stage and fully verify `NEW_TIP` locally before Jira DELETE. Re-read live `refs/heads/tickets` and
the full census; both must still equal the frozen values. The publication lease is exactly
`refs/heads/tickets:OLD_TIP`. Never widen or omit it.

### 3. Delete the exact six Jira rollout copies and verify every other comment

This is the irreversible checkpoint. Reconfirm the execution ticket approval and record the
manifest's 64-hex digest. Execute only the prepared command and artifact directory:

```sh
python infra/scripts/cleanup_comment_echo_jira.py \
  --repo "$SOURCE_CLONE" \
  --manifest "$MANIFEST" \
  --artifact-dir "$JIRA_ARTIFACTS" \
  --execute \
  --confirm-manifest-digest '<recorded-manifest-digest>'
```

The tool must write and fsync an intent before each DELETE, issue one attempt, re-read Jira after
every response or ambiguity, and fsync an outcome before advancing. It deletes only IDs `997293`,
`997255`, `997256`, `997143`, `997144`, and `997145` in canary issue order. It must produce
`jira-after.json` with the six fixed survivor IDs and every non-target comment byte/identity
unchanged.

Never switch to ACLI/manual DELETE, delete a survivor, truncate the journal, or automatically retry
an ambiguous request. If execution stops, preserve the artifact directory and rerun the same
digest-confirmed command only after inspecting its durable intent/outcome plus live Jira state. A
failed postcondition keeps every writer and workflow disabled.

### 4. Prove comment convergence twice while the workflow stays disabled

Publish `NEW_TIP` only to the disposable rehearsal remote and use a fresh clone of that remote as
the candidate store. With the exact production runtime and credentials, run one scoped dry-run from
the frozen old source and two from the candidate against live post-cleanup Jira. Use the exact
selection, renderer, canonicalization, and digest pin rehearsed before DELETE. All runs must be
read-only and invoke no transport write. Between observations, require Jira inventory, candidate
HEAD/tree, bridge bytes, bindings, and all tracked store bytes to remain unchanged. The two complete
candidate observations must be identical.

The candidate must plan no replacement comment, local COMMENT import, label, or link. Its
non-collection projection must equal the old source exactly, and its full `result.plan` mutation
list must equal the independently inspected pre-DELETE digest. Apply the separately inspected
additive-metadata rule above; do not broaden the mutation digest to include observation identity.
For the frozen preparation this is the three description-only projection described above. This
allowance is an oracle comparison, not execution authority: do not run a scoped live sync or
manually converge those descriptions in the window.

Any collection operation, extra target/field/payload, old-versus-candidate projection difference,
digest mismatch, changed persisted byte, transport write, or runtime mismatch blocks publication.
Keep `reconcile-bridge.yml` in `disabled_manually` and pursue a reviewed forward correction; do not
recreate comments, rewrite bindings, or expand the allowlist inside the window.

### 5. Publish the rewritten `tickets` branch once

Only the execution ticket authorizes this one raw Git force update. Immediately before it, require
live `refs/heads/tickets == OLD_TIP` and the unchanged full ref census. From the isolated clone,
publish `NEW_TIP` with
`--force-with-lease=refs/heads/tickets:OLD_TIP`. Do not update any other ref.

Fetch into an independent new verification clone and require:

- live `refs/heads/tickets == NEW_TIP`;
- `OLD_TIP` is not an ancestor of the new tickets ref or any default-fetched ref;
- replay, manifest delta, six survivor identities, bridge bytes, epoch, and `git fsck --strict`
  still pass;
- no deleted Jira ID or other manifest-removed echo survives in reduced local state; and
- measurements match the prepublication candidate within measurement-method noise, reported as
  observations rather than thresholds.

### 6. Replace every persistent clone before enabling any writer

Never let a pre-rewrite clone merge, rebase, reset onto, or push to the rewritten branch. Replace
the shared local tracker, review-bot storage, hosted-agent stores, and every newly discovered
persistent consumer with a fresh clone of `origin/tickets`.

Before replacing the shared tracker, securely preserve only its ignored `.env-id`, `.opcert-key`,
`.opcert-key.pub`, and `.ensure-applied` files and their modes. Restore them only to the fresh clone
for the same trusted environment. Do not copy refs, objects, indexes, caches, stashes, or worktrees.
Retain old clones offline as evidence. Restart MCP servers instead of reusing an in-process store
handle. For each new clone, record HEAD and prove `OLD_TIP` is absent or not an ancestor.

### 7. Re-enable in stages and prove non-resurrection

While Reconcile Bridge remains disabled, repeat the live Jira inventory and candidate replay, then
verify through the GitHub API that the workflow is still `disabled_manually` and has no queued run.
Only then enable `reconcile-bridge.yml`:

```sh
gh workflow enable reconcile-bridge.yml --repo "$GH_REPO"
gh api "repos/${GH_REPO}/actions/workflows/reconcile-bridge.yml" --jq .state
```

Require state `active`. Trigger or observe exactly one live pass and record its run ID, runtime SHA,
and conclusion. Before enabling another writer, prove it created no replacement target body or new
local echo, the six survivors remain, `OLD_TIP` remains absent, and any legitimate ticket event is a
descendant of `NEW_TIP`. Then enable the heartbeat canary and remaining writers one materially
different runtime at a time, repeating the first-write/non-resurrection check.

Record all receipts and notify the operator that service is restored. Keep the bundle, Jira
inventories, manifest, journal, commit map, retired clones, and rollback receipts until the incident
is closed.

## Correction and rollback after the Jira checkpoint

Before Jira DELETE, abandoning the local candidate is harmless: no authoritative data has changed.
After Jira DELETE, there is no exact two-system rollback because Jira Cloud cannot restore the six
original comment IDs. Keep all writers disabled and prefer a reviewed forward correction that
finishes the already-verified local rewrite.

The Git bundle still provides an exact branch rollback, but it must never be presented or executed
as a complete incident rollback by itself. If the operator nevertheless authorizes it, enter a
second quiet window, prove every writer quiescent, let `ROLLBACK_LEASE` be the exact current live
OID, independently restore the bundle, and publish its unique `/old-tip` with
`--force-with-lease=refs/heads/tickets:ROLLBACK_LEASE`. Re-clone every persistent consumer again.

Restoring Jira content would be a separate compensating operation: recreated comments receive new
IDs, so it requires an explicit operator decision, a fresh complete Jira inventory, a newly
generated identity manifest, and a reviewed local reconciliation/migration plan. Never POST old
bodies and then enable the old branch as though IDs still matched.

If any post-publication ticket event must be retained, stop. Exact Git rollback would discard it;
transplanting it requires a separately reviewed event migration. Never improvise either a Git
transplant or Jira compensation inside the window.

## Abort conditions

Abort without publication on any ambiguity, including a moving ref, nonempty writer queue, active
lease, workflow state other than `disabled_manually`, incomplete Jira pages, anything other than the
fixed two-copy state before DELETE or one-survivor state afterward, a malformed/truncated journal,
bridge-map disagreement, unexpected event envelope, shared target blob, noncanonical or stale
manifest, source-tip mismatch, partial/shallow/dirty source, snapshot provenance mismatch,
incomplete commit map, unexpected path delta, any candidate comment/label/link operation, any
old-versus-candidate non-collection difference, any plan outside the independently inspected digest,
replay/authorship difference, fsck failure, failed bundle restore, failed Git rollback rehearsal,
or epoch-refusal probe failure.

After publication, keep writers stopped and choose either a proven correction from the fresh
candidate or an explicitly approved coordinated second-window recovery. Never respond by weakening
the manifest, dropping the lease, resetting a persistent old clone onto the new ref, issuing an
unjournaled Jira mutation, restoring bodies while pretending their IDs are unchanged, or retrying
until a race happens to pass.
