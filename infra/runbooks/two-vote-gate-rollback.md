# Runbook — the two-vote gate: activate, back out, verify (epic 1fa8)

The rebar submit gate is **two independent votes** (ADR-0020): a change is submittable
only when **both** are `MAX` and there are no unresolved comments —

> `label:LLM-Review=MAX AND label:Verified=MAX AND -has:unresolved`

- **`LLM-Review`** — the review-bot (LLM code review). Live since epic d251.
- **`Verified`** — CI (build/test/lint/typecheck) via gerrit-to-platform → GitHub
  Actions → an SSH vote back into Gerrit (ADR-0022/0023).

This runbook is the operator control surface for the `Verified` leg: how to **activate**
it, how to **back out** to single-vote (LLM-Review-only) gating if CI breaks, and the
**E2E verification** that gates activation. The authoring lives in
`infra/gerrit/project.config`; the push tool is `infra/gerrit/setup-project.sh`.

---

## The `Verified` submit requirement ships INACTIVE

`project.config` authors the `Verified` label AND its submit requirement, but the
submit requirement carries an **`applicableIf = is:false`** line:

```
[submit-requirement "Verified"]
	description = CI (build/test/lint/typecheck via GitHub Actions) must pass (MAX).
	applicableIf = is:false          # <-- INACTIVE: requirement never applies yet
	submittableIf = label:Verified=MAX
	canOverrideInChildProjects = false
```

While `applicableIf = is:false` is present, CI **records** `Verified` votes on changes
but they **do not block submit** — the gate stays single-vote (LLM-Review only). This is
the deliberate rollout safety: the config is deployable before the CI voter is proven,
with no window where the gate is enforced but no voter exists (which would freeze all
submits). Activation and back-out are just presence/absence of that one line.

---

## A. Back out to single-vote gating (the tested rollback)

**When:** g2p/CI is broken (dispatcher down, Actions failing, the SSH vote can't land)
and the `Verified` requirement is **active**, so changes are stuck waiting on a CI vote
that will never arrive. Restore single-vote (LLM-Review-only) gating so `main` is not
frozen while you fix CI. (If the requirement is still INACTIVE, there is nothing to back
out — CI failures already don't block submit.)

**How:** re-add the `applicableIf = is:false` line to the `Verified` submit requirement
and re-push refs/meta/config. The declarative push tool overwrites the live config.

1. Edit `infra/gerrit/project.config`, restoring the inactive line under
   `[submit-requirement "Verified"]`:
   ```
   [submit-requirement "Verified"]
   	description = CI (build/test/lint/typecheck via GitHub Actions) must pass (MAX).
   	applicableIf = is:false
   	submittableIf = label:Verified=MAX
   	canOverrideInChildProjects = false
   ```
2. Dry-run the diff against the live config, then push:
   ```bash
   DRY_RUN=1 bash infra/gerrit/setup-project.sh   # review the staged diff
   bash infra/gerrit/setup-project.sh             # push refs/meta/config
   ```
   (Needs the Gerrit admin SSH key; see the script header for env vars.)
3. Confirm on any open change that the `Verified` requirement no longer applies —
   `LLM-Review=MAX AND -has:unresolved` is submittable again. The `Verified` label still
   records votes (harmless), it just no longer blocks.

**This is the tested back-out.** It touches only one line, is fully declarative, and is
symmetric with activation (§B). It does NOT decommission the CI account/credentials — for
a permanent retire, follow `g2p-ci-credentials.md` §4 after backing out here.

> **Related kill-switches.** To stop the CI *dispatch* without changing the gate, disable
> the `hooks` plugin's g2p exec (or the g2p PAT) — CI stops firing, votes stop arriving,
> and if the requirement is active, back it out per §A. Replication and the review-bot
> have their own kill-switches in `review-bot-ops.md`.

---

## A.2. Back out the feature-branch flow (epic 88ab / ADR-0025)

**When:** the feature-branch pattern is being retired (or a merge-change incident forces a
return to the single-change-only flow) and you need to remove its Gerrit-side surface. This is
independent of the `Verified` back-out (§A) — the two-vote gate itself is unchanged.

All edits are in `infra/gerrit/project.config`; preview and push with the same declarative tool
(`DRY_RUN=1 bash infra/gerrit/setup-project.sh`, then without `DRY_RUN`).

1. **Revoke the feature-branch ACLs** — restores the single-change-only write path by removing
   the three permission types bound to `feature-branch-drivers`:
   - `[access "refs/heads/feature/*"]` — the `create` / `delete = group feature-branch-drivers`
     grants (branch lifecycle).
   - `[access "refs/for/refs/heads/main"]` — the `exclusiveGroupPermissions = pushMerge` +
     `pushMerge = group feature-branch-drivers` block (merge-back push).
   - `[access "refs/for/refs/heads/feature/*"]` — the same `exclusiveGroupPermissions =
     pushMerge` + `pushMerge` block (catch-up merges into a feature branch).
   With these gone, merge-commit pushes fall back to the inherited behaviour and `feature/*`
   create/delete is no longer group-restricted.

2. **LLM-Review copyCondition is inert to leave.** The `OR changekind:MERGE_FIRST_PARENT_UPDATE`
   token on `[label "LLM-Review"]` cannot match a non-merge patchset, so absent merge changes it
   is a no-op — **leave it or revert it**, either is safe (ADR-0025 back-out note).

3. **Revert the submit type** — delete the `[submit]` block (`action = merge if necessary` +
   `mergeContent = true`) to restore `INHERIT`. **VERIFY** the inherited value still matches the
   S1-recorded prior state — **MERGE_IF_NECESSARY + content-merge `true`** — via a read-only
   `GET /a/projects/rebar/config` before and after, so removing the pin does not silently change
   submit semantics. If inheritance has drifted, re-pin rather than remove.

4. **Bot rollback (if a merge-review image is implicated).** Rolling the review-bot back to the
   prior image is a separate path — see `infra/runbooks/review-bot-ops.md` ("Bot-code rollback =
   redeploy the prior image", ~lines 167–181): `docker tag compose-review-bot:prev …` / the
   `:prev` auto-rollback under continuous auto-deploy.

5. **Apply:** `DRY_RUN=1 bash infra/gerrit/setup-project.sh` to preview the staged diff, then
   `bash infra/gerrit/setup-project.sh` to push `refs/meta/config`. (The full RETIRE path — also
   emptying/deleting the `feature-branch-drivers` group — is ADR-0025 "Group RETIRE".)

---

## B. Activate the `Verified` gate (OPERATOR handoff — needs live creds)

> **DO NOT activate until the CI voter is proven end-to-end (§C).** This step is a live
> operator action, not an in-repo change that lands via a PR — it is deliberately kept
> out of the committed rollout so the gate is never enforced before the voter works.

**Prerequisite:** the CI credentials are installed (`g2p-ci-credentials.md`): the PAT in
SSM + materialised, the CI SSH key in SSM + as the `GERRIT_SSH_PRIVKEY` GitHub secret, the
four `GERRIT_*` repo variables set, and the CI service account in Service Users.

**How:** DELETE the `applicableIf = is:false` line from the `Verified` submit requirement
(leaving only `submittableIf`) and re-push:

1. In `infra/gerrit/project.config`, remove the `applicableIf = is:false` line under
   `[submit-requirement "Verified"]` so it reads:
   ```
   [submit-requirement "Verified"]
   	description = CI (build/test/lint/typecheck via GitHub Actions) must pass (MAX).
   	submittableIf = label:Verified=MAX
   	canOverrideInChildProjects = false
   ```
2. `DRY_RUN=1 bash infra/gerrit/setup-project.sh` to review, then
   `bash infra/gerrit/setup-project.sh` to push.
3. Confirm a change now needs **both** `LLM-Review=MAX` **and** `Verified=MAX` to submit.

To reverse, do §A.

---

## C. E2E verification (OPERATOR handoff — the activation gate)

Prove the whole CI loop on a **throwaway change** while the requirement is still INACTIVE
(so a failure can't freeze `main`). Only after this passes do you activate (§B).

1. **Dispatch fires.** Push a trivial change for review
   (`git push origin HEAD:refs/for/main`). Within a minute a `gerrit-verify` run should
   appear in GitHub Actions (Gerrit → g2p → workflow_dispatch). If not, check
   `journalctl CONTAINER_NAME=compose-gerrit-1 | grep -i gerrit_to_platform` and the g2p
   dispatch alarm (`rebar-gerrit-g2p-dispatch-errors`, monitoring_1fa8.tf).
2. **CI runs the real suite** against the exact patchset (same checks as `test.yml`).
3. **Vote-back lands.** On completion the run SSHes in and casts `Verified` — confirm a
   `Verified +1` (green) or `-1` (red, fail-closed) from the CI service account on the
   change, with the run URL in the message.
4. **`recheck` re-runs.** Comment `recheck` on the change; confirm a fresh run dispatches
   and re-votes (the g2p `comment-added` → `verify` mapping).
5. **New patchset resets Verified.** Amend + re-push; confirm the prior `Verified` is
   dropped (strict `copyCondition = changekind:NO_CODE_CHANGE`) and a fresh run casts a
   new one — no stale CI vote carries onto new code (GerriScary-safe, CVE-2025-1568).
6. **Only now activate (§B).** After activation, do one more full loop and confirm submit
   requires BOTH votes. Abandon the throwaway change.

---

## C.1 — E2E EXECUTED (proof record, 2026-07-02)

The §C loop was run live on the production Gerrit host (`rebar.solutions.navateam.com`) and
the gate was activated per §B. Recorded here so the epic's "live E2E / coexistence" acceptance
criteria are verifiable from the repo, not just an operator handoff. Throwaway change:
`https://rebar.solutions.navateam.com/c/rebar/+/162`.

- **Coexistence (both gates fire on one event).** Pushing patchset 1 of change 162 fired BOTH
  legs on the same `patchset-created` event: the review-bot webhook cast `LLM-Review` (by
  `rebar-review-bot`), AND g2p `workflow_dispatch`ed `gerrit-verify.yaml`
  (GitHub Actions run `28612639871`), which cast **`Verified +1`** back over SSH as
  `rebar-ci-bot`. (g2p on Gerrit 3.14.1 required the git-pinned build — the released g2p
  crashes on the compact `project~number` change-id; see the compat fix in `Dockerfile.gerrit`.)
- **Vote-back carries the run URL.** The CI votes linked their run, e.g. a red run cast
  `Verified -1  FAILURE: https://github.com/navapbc/rebar/actions/runs/28615370753` (the
  `gerrit-review-action` message is `<STATUS>: <server>/<repo>/actions/runs/<run_id>`).
- **`recheck` re-runs.** A `recheck` comment on 162 re-dispatched `gerrit-verify` and re-voted
  (the g2p `comment-added` → `verify` mapping).
- **New patchset resets `Verified`.** Amending + re-pushing 162 dropped the prior votes —
  Gerrit reported *"approvals got outdated and were removed: … Verified+1 … (copy condition:
  changekind:NO_CODE_CHANGE)"* — and a fresh run cast a new one. No stale CI vote carried onto
  new code (GerriScary-safe, CVE-2025-1568).
- **Activated + both votes required to submit.** After deleting `applicableIf = is:false` (§B,
  pushed to `refs/meta/config`), change 168 (the activation change) showed
  `submit_requirements: Verified → UNSATISFIED, LLM-Review → UNSATISFIED` (vs `Verified →
  NOT_APPLICABLE` on pre-activation change 165). It then earned **both** `LLM-Review +1` AND
  `Verified +1`, became submittable, was **submitted → merged → replicated to GitHub `main`**
  (`origin/main` `1d7caf129`). Red CI (`Verified -1`, e.g. run `28615370753`) leaves a change
  unsubmittable once the requirement is active.
- **Credentials provisioned (S4).** `rebar-ci-bot` (Gerrit account id `1000008`) is a member of
  **Service Users** (so it may cast `Verified`); SSM holds `/rebar/prod/g2p-github-pat` +
  `/rebar/prod/ci-gerrit-ssh-key`; the GitHub repo carries vars `GERRIT_SERVER` /
  `GERRIT_SSH_USER=rebar-ci-bot` / `GERRIT_KNOWN_HOSTS` / `GERRIT_URL` and secret
  `GERRIT_SSH_PRIVKEY`. NOTE: because g2p runs **in-container** (ADR-0022), the g2p GitHub PAT
  is materialized by `infra/gerrit/materialize-g2p-config.sh` into `gerrit_to_platform.ini`
  (0600) at boot — NOT via `infra/scripts/fetch-secrets.sh` (a deliberate deviation from the
  original story text, which predated the in-container decision).

---

## Design notes

Rationale for the CI-gate design choices, for future maintainers:

- **Cost.** `navapbc/rebar` is a **public** repo, so GitHub-hosted Actions minutes
  (including **macOS** runners) are **free**. The CI matrix (Linux py3.11/3.12 + macOS
  py3.12) therefore adds no runner cost. The only marginal cost is the operator's SSM
  parameters (negligible) and the CloudWatch alarm.
- **Latency.** A full CI run is ~**10–16 min** (the matrix + the gates + the two pytest
  tiers, matching `test.yml`). That is the added wall-clock before a change becomes
  submittable — acceptable for a gate, and it runs in parallel with the LLM-Review vote
  (the two legs are independent), so it does not serialize behind it.
- **Flaky-test handling.** A transient CI failure casts `Verified -1` (fail-closed). Two
  recovery paths, no diff change needed:
  - Comment **`recheck`** — g2p re-dispatches `gerrit-verify.yaml` (the `comment-added`
    → `verify` mapping) and re-votes on the same patchset.
  - Push a **new patchset** — the workflow's `concurrency` group
    (`gerrit-verify-<change-id>`, `cancel-in-progress: true`) cancels any in-flight run
    for that change so only the newest patchset's run survives (no wasted/stale runs).
- **`copyCondition` re-run behavior.** `Verified` carries a vote ONLY across a true no-op
  re-upload (`changekind:NO_CODE_CHANGE`), NOT `TRIVIAL_REBASE`. So a commit-message-only
  amend keeps the vote (no needless re-run), but any real code change — or a rebase onto a
  moved base — **drops** `Verified` and forces a fresh CI run. This is the safe default:
  a CI vote never certifies a tree CI did not actually run against.

---

## See also
- `docs/adr/0020-two-vote-ci-gate.md` — the two-vote gate design + staged rollout.
- `docs/adr/0022-g2p-in-container.md` / `docs/adr/0023-inbound-github-gerrit-ssh-vote.md`.
- `infra/runbooks/g2p-ci-credentials.md` — credential setup / rotation / retire.
- `infra/gerrit/project.config` + `infra/gerrit/setup-project.sh` — the config + push tool.
- `CONTRIBUTING.md` — the contributor-facing two-vote flow + `recheck`.
