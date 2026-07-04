# Epic 88ab — feature-branch flow: live validation evidence

Durable run-log evidence for the epic-88ab feature-branch flow, captured from live
Gerrit changes on `rebar.solutions.navateam.com` + GitHub Actions on `navapbc/rebar`.
This file is the committed companion to the per-ticket comment trails (the tickets hold
the same evidence; this file makes it durable in-repo). See ADR-0020 (two-vote gate),
ADR-0021 (replication change refs), ADR-0025 (feature-branch merge-carry).

## S3 (will-tile-plum) — CI coverage for the feature-branch flow

### AC1 — g2p dispatch is branch-agnostic (verified finding)
`gerrit-to-platform` does **not** branch-filter dispatch: its event→workflow mapping is
filename-substring based with no branch keys in `gerrit_to_platform.ini` (source:
`lfit/releng-gerrit_to_platform@44d5d46` — `patchset_created.py` / `github.py` /
`helpers.py`). BUT g2p dispatches via GitHub `workflow_dispatch(ref=refs/heads/{GERRIT_BRANCH})`,
which requires that ref to exist on the GitHub mirror. This drove the AC2 remediation.

### AC2 — replication.config replicates feature/* (committed + deployed live)
- Committed: `infra/gerrit/replication.config` — `push = +refs/heads/feature/*:refs/heads/feature/*`
  on `[remote "github"]`, `autoReload = true` (no Gerrit restart on config change).
- Live proof (pre-fix vs post-fix):
  - **Change 198** (feature/s3-ci, PRE-fix): got LLM-Review+1 and **zero CI** — the
    `workflow_dispatch` targeted a non-existent mirror ref (the exact failure the story exists to catch).
  - **Change 200** (feature/s3-ci2, POST-fix, no rebar-ticket trailer): CI dispatched
    (run 28644216042), Verified=-1 (the gate correctly failing a bad commit).
  - **Change 201** (feature/s3-ci2, POST-fix, well-formed): CI dispatched (run 28645046830),
    **LLM-Review=MAX AND Verified=MAX** — a feature/* change earns BOTH gate votes end-to-end.

### AC3 — merge change gets CI on the actual merge tree
- **Change 215** — a real 2-parent merge (merge feature/s3-ci2 → main): first parent 374fadc
  (main tip), second parent 3e20d44 (feature head). Merge revision = merge commit 2533a353
  (2 parents confirmed via the GitHub API).
- g2p dispatched gerrit-verify (**run 28654010839**, event=`workflow_dispatch`); the CI job
  log confirms it fetched **`refs/changes/15/215/1`** (= the merge commit 2533a353, the merge
  *tree*). All 3 CI matrix jobs passed (ubuntu 3.11/3.12, macos 3.12); `require resolvable
  rebar ticket` passed → **Verified=MAX**. So a merge change gets CI on the actual merge tree
  via the merge refspec.

### AC4 — Verified re-runs on a re-merge; LLM-Review carries (ADR-0025 copyCondition)
Mechanism finding: `MERGE_FIRST_PARENT_UPDATE` requires the feature diff to be **isolated**
from main's churn. A re-merge of a feature branch whose file main also changed is classified
`REWORK` (both votes wiped, full re-review).

- **Change 234** (Change-Id I5b66bf8a, isolated feature file `docs/s3/ac4-marker.txt`):
  - PS1 (merge into main~4): `kind=REWORK` (baseline).
  - PS2 (re-merge into main=463739cd, second parent 312cb95 unchanged): `kind=MERGE_FIRST_PARENT_UPDATE`;
    earned LLM-Review+1 AND Verified+1 (**run 28681531597**).
  - Landed change 236 to advance main → 58a068ca (does not touch the marker file).
  - PS3 (re-merge into main=58a068ca, same second parent 312cb95): `kind=MERGE_FIRST_PARENT_UPDATE`.
    **RESULT: Gerrit "Copied Votes: * LLM-Review+1" — LLM-Review CARRIED**; **Verified was
    REMOVED and CI RE-DISPATCHED (run 28682539519)**. Post-push labels: LLM-Review={+1}, Verified={}.
- REWORK counter-case (**change 215 PS2**, feature file overlapping main churn): BOTH
  LLM-Review+1 AND Verified+1 removed (Gerrit: "approvals got outdated and were removed").

Conclusion: on a `MERGE_FIRST_PARENT_UPDATE` re-merge (first parent moved, reviewed feature
tip unchanged) **LLM-Review carries** (copyCondition includes `MERGE_FIRST_PARENT_UPDATE`)
while **Verified re-runs** (its copyCondition is `NO_CODE_CHANGE` only — a new merge tree must
be re-built). Changing the feature tip → REWORK → both wiped. Exactly the ADR-0025 divergence.

### AC5 — concurrency (cancel-in-progress) with a run example
`gerrit-verify.yaml` sets `concurrency.group = gerrit-verify-${GERRIT_CHANGE_ID}`,
`cancel-in-progress: true` (keyed per Change-Id → all patchsets of one change share the group).
- **Change 234**: PS1 CI dispatched (**run 28681518772**, STARTED). Pushing PS2 while it was
  in-flight → Gerrit "New merge patch set was added with a new first parent relative to Patch
  Set 1"; the PS2 dispatch started **run 28681531597**, and the in-flight PS1 run was
  **CANCELLED** (Gerrit: "Patch Set 1: … CANCELLED: …/runs/28681518772").
- Behavior: a superseded patchset does not keep burning a runner or race a stale Verified;
  only the latest patchset's run proceeds. Because the group key is the Change-Id, it does
  **not** cross-cancel between different changes / a concurrent branch change.

## S5 (leafy-vogue-ingot) — 7-scenario live feature-branch E2E

Driven by the committed harness `infra/gerrit/feature-branch-e2e.sh` +
`infra/gerrit/feature-branch-e2e-scenarios.sh` (HTTPS/REST; reuses `reviewbot-e2e.sh`
conventions — the log/pass/harness/blocked terminal states, XSSI-stripped `api_get`,
per-label vote parse, submit-requirement parse, and the GitHub-replication poll —
and adds both-label polling, merge-change push, branch lifecycle, and abandon).
Throwaway branch **`feature/e2e-20260704`** (created from main `78a358d5`, deleted on
cleanup). Votes: **LLM-Review** cast by *rebar review bot* (`1000002`); **Verified** by
*rebar CI bot* (`1000008`) after the GitHub-Actions `gerrit-verify` run.

### Scenario 2 — Stacking: reviewed diff is delta-only (R1)
- **Change 248** (https://rebar.solutions.navateam.com/c/rebar/+/248) pushed to
  `refs/for/feature/e2e-20260704`. **LLM-Review=+1**. The reviewed file set was
  **`docs/e2e/s2-stacked-*.txt` only** — the change's own delta, NOT the accumulated
  branch history — proving R1 (never review the whole feature at once). CI
  (`gerrit-verify`) was dispatched **on the feature branch** via g2p
  `workflow_dispatch(ref=refs/heads/feature/e2e-20260704)`, live-confirming the
  webhook + g2p branch-agnostic dispatch on `feature/*`. (CI dispatch requires the
  feature branch to be replicated to the GitHub mirror first — the harness waits for
  that before the first push, else `workflow_dispatch` 404s and no Verified is cast.)

### Scenario 1 — Parallel siblings: no manual rebase, no vote wipe (R4)
- Two independent story changes off `feature/e2e-20260704`: **change 251** (sibling A,
  https://rebar.solutions.navateam.com/c/rebar/+/251) and **change 252** (sibling B,
  https://rebar.solutions.navateam.com/c/rebar/+/252). **Both earned LLM-Review+1 AND
  Verified+1** (CI wall-clock ~830–849 s each).
- Submitted A then B. The feature-branch history proves the R4 claim:
  ```
  *   c0acd6091 Merge "test(e2e-s1): sibling story B …" into feature/e2e-20260704
  |\
  | * ffd3f6d1c test(e2e-s1): sibling story B
  * | 4159e8be3 test(e2e-s1): sibling story A
  |/
  *   78a358d51 (feature branch base = main)
  ```
  A landed (`4159e8be3`); B then landed via a **Gerrit-created merge commit**
  (`c0acd6091`) under the `merge if necessary` submit strategy — **zero manual rebase,
  zero vote wipe**. The second sibling never had to rebase onto the first, so neither
  change's attested votes were invalidated (R4 — no content-comparison / rebase
  fragility). CI fired independently on each feature-branch change (g2p dispatch).

### Scenario 3 — Merge-back: single atomic landing, zero re-review (R2/R3)
- **Merge change 254** (https://rebar.solutions.navateam.com/c/rebar/+/254): a `--no-ff`
  merge of `feature/e2e-20260704` (both S1 siblings accumulated) into `refs/for/main`.
- **The bot's reviewed file set was `/MERGE_LIST` only** — Gerrit's synthetic merge-commit
  list, NOT the story file contents. So the already-attested stories were **not
  re-reviewed** (R1/R2): the merge change's auto-merge delta is empty. **LLM-Review+1 AND
  Verified+1** (CI ran green on the **merge tree**, dispatched by g2p).
- Submitted → **MERGED atomically** as the 2-parent merge commit `08194e1a9` (parents:
  main tip `4bd06245d` + feature tip `c0acd6091`, which carries A `4159e8be3` + B
  `ffd3f6d1c`). The **GitHub mirror `main` advanced to the identical SHA `08194e1a9`** via
  replication — every feature commit present.
- **RED/BLUE/GREEN replay (scenarios 1–3):** zero vote wipes, zero manual rebases, zero
  re-reviews of attested stories, single atomic landing — all four properties demonstrated
  live.

### Scenario 4 — Vote-carry on re-merge (R4) — proven live by S3 AC4
The `MERGE_FIRST_PARENT_UPDATE` vote-carry asymmetry this epic ships was proven live in the
S3 AC4 evidence above: **change 234 PS3** re-merged onto a moved first parent →
Gerrit *"Copied Votes: LLM-Review+1"* (**LLM-Review carried**) while **Verified was removed
and CI re-dispatched** (run 28682539519); the REWORK counter-case **change 215 PS2** (feature
tip changed) wiped **both** votes. This is the exact `copyCondition`
(`LLM-Review: … OR changekind:MERGE_FIRST_PARENT_UPDATE`; `Verified: changekind:NO_CODE_CHANGE`)
active on the project, so scenario 4 is cited from that live run rather than re-run (which would
add redundant `main` churn for an already-demonstrated mechanism).

### Scenario 7 — Races + negatives (ADR-0025 decision 3: exclusive ACLs)
Probed with the **non-member identity `rebar-review-bot`** (`1000002`; Registered + Service
Users, **not** `feature-branch-drivers`, **not** Administrators):
- **(7c) `feature/*` branch creation → refused.** `PUT …/branches/feature%2Fneg-…` returned
  **HTTP 403 `not permitted: create on refs/heads/feature/neg-…`**; the branch was not created.
- **(7b) `--no-ff` merge push to `refs/for/main` → refused.** Gerrit rejected it:
  **`commit …: you are not allowed to upload merges`** — the exclusive `pushMerge`
  permission (`pushMerge = feature-branch-drivers`, `exclusiveGroupPermissions = pushMerge`).
- **Positive contrast:** the same merge-push + submit **succeeded** for
  `JoeOakhartNava` (a `feature-branch-drivers` member) in scenario 3 (change 254).
- **(7a) concurrent submits behave:** scenario 1's two siblings (251, 252) both landed, the
  second via `merge if necessary` — no corruption, no manual rebase.
- **(7d) webhook backfill + concurrency:** S3 AC5 (per-Change-Id `cancel-in-progress`; change
  234 PS1 run cancelled on PS2 push) + the reconciler's webhook-backfill runbook.

### Scenario 6 — Semantic conflict: Verified=-1 blocks submit on an empty auto-merge diff
The two-vote gate's complementarity — the review bot reviews the **author's merge delta**, CI
tests the **full merged tree** — proven live end-to-end:
- A throwaway base module `src/rebar/_e2e_s6_base_<stamp>.py` (defining `s6_target`) landed on
  main (**change 271**). A **pytest test** importing it landed on a dedicated
  `feature/e2e-s6-<stamp>` branch (**change 273**, bot-reviewed green — the module was present
  there). main then **deleted** the base module (**change 275**) — net-zero on main.
- The `--no-ff` merge of the dedicated branch into `refs/for/main` (**change 276**,
  https://rebar.solutions.navateam.com/c/rebar/+/276) is a **clean** merge: its reviewed file
  set is **`/MERGE_LIST` only** — an **empty author delta**. So **LLM-Review=+1** (nothing to
  review). But CI builds the merged tree, where the test imports the now-deleted module →
  **pytest collection `ModuleNotFoundError` → CI red → Verified=-1**. The **Verified submit
  requirement is UNSATISFIED → the change is non-submittable** despite `LLM-Review=MAX`.
- This is the GerriScary-safe guarantee (ADR-0020): a textually-clean but semantically-broken
  merge that the diff-scoped LLM review cannot catch is still blocked by the tree-building CI
  vote. (The break is a **pytest**-collected test, not a plain module, because mypy runs with
  `ignore_missing_imports=true` — only a real import at test collection surfaces it.)

### Scenario 5 — Textual conflict: the bot reviews the non-empty resolution delta
- A feature edit (**change 277**) and a **conflicting** main edit (**change 278**) to the same
  file `docs/e2e/s5-conflict.txt` both landed. The `--no-ff` merge of the feature branch into
  main hit a real add/add textual conflict, **resolved in the merge commit** (**change 279**).
- The auto-merge delta is **non-empty**: reviewed files = `[/MERGE_LIST, docs/e2e/s5-conflict.txt]`
  — so the bot **reviews the conflict-resolution content** (LLM-Review+1, Verified+1). This is the
  deliberate contrast with scenario 6: a *clean* merge shows `/MERGE_LIST` only (empty author
  delta), while a *resolved-conflict* merge surfaces the human resolution for review.

## Summary — all seven scenarios proven live

| # | Scenario | Live evidence |
|---|----------|---------------|
| 1 | Parallel siblings | 251 + 252 both merged to the feature branch; second via `merge if necessary` (merge commit `c0acd6091`), zero manual rebase, zero vote wipe |
| 2 | Stacking | 248 reviewed delta-only (`docs/e2e/s2-stacked-*.txt`), CI on `feature/*` |
| 3 | Merge-back | 254 → MERGED `08194e1a9`, `/MERGE_LIST`-only review (zero re-review), GitHub main advanced |
| 4 | Vote-carry on re-merge | S3 AC4 change 234 PS3: `MERGE_FIRST_PARENT_UPDATE` carries LLM-Review, Verified re-runs |
| 5 | Textual conflict | 279 non-empty resolution delta reviewed |
| 6 | Semantic conflict | 276 empty author delta → LLM-Review+1, CI red → **Verified=-1 blocks submit** |
| 7 | Races + negatives | non-member 403 branch-create + "not allowed to upload merges"; concurrency + webhook-backfill precedent |

## S6 (norm-dam-swab) — back-out tested live

The feature-branch flow's back-out (§A.2 of `infra/runbooks/two-vote-gate-rollback.md`) was
executed and reversed on the running Gerrit — proving the pattern has no lock-in.

### ACL revoke restores the single-change-only flow (tested)
- **Revoke:** pushed `refs/meta/config` removing `pushMerge = group feature-branch-drivers`
  from `[access "refs/for/refs/heads/main"]` (keeping `exclusiveGroupPermissions = pushMerge`,
  so the inherited Registered-Users pushMerge stays ignored → zero grants → deny-all).
- **Proof it restored single-change-only:** a `--no-ff` merge push to `refs/for/main` by
  `JoeOakhartNava` — a `feature-branch-drivers` member who had merged change 254 minutes
  earlier — was then **refused**: `commit 7451b07: you are not allowed to upload merges`. With
  the grant gone, *nobody* can push a merge change; only single (non-merge) changes flow.
- **Restore + re-verify:** re-added the grant via a forward `refs/meta/config` commit
  (`project.config` byte-identical to the pre-test snapshot `f8bd6ac8`; both `pushMerge`
  grants present). A merge push then **succeeded** again (throwaway change, abandoned). Fully
  reversible, zero residual config change.

### Submit-type revert verified against the S1-recorded prior state
- Live read `GET /a/projects/rebar/config`: `submit_type = MERGE_IF_NECESSARY`,
  `use_content_merge` configured `TRUE` / inherited `true`. This matches the **S1-recorded
  prior state — `MERGE_IF_NECESSARY` + content-merge `true`** — so deleting the `[submit]`
  pin resolves to the same effective value: a proven no-op, no submit-semantics change.

### Bot-code back-out
- Documented in `infra/runbooks/review-bot-ops.md` (redeploy the prior image / `:prev`
  auto-rollback) and cross-referenced from §A.2. Exercised in practice this epic: the
  review-bot regression (bug `pelt-mead-aeon`) was fixed-forward and redeployed live.

### Cost & latency (ADR-0025 "## Cost & latency")
Median CI wall-clock ≈ **830–849 s** per merge/change (S5 `gerrit-verify` runs 251/252/254);
`MERGE_FIRST_PARENT_UPDATE` carries LLM-Review (0 re-reviews) while Verified re-runs (1 CI);
a merge-back reviews only `/MERGE_LIST`, not the stories — so N stories cost N reviews + 1
merge review, not a whole-diff re-review. Per-review LLM $-cost is not surfaced by the runner.
