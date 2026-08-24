# Runbook for active two-vote gate rollback and restoration

The rebar submit gate requires two independent votes. `LLM-Review` records the review-bot decision. `Verified` records CI from GitHub Actions through gerrit-to-platform. A change is submittable only when both votes reach MAX and no unresolved comments remain.

```text
LLM-Review requirement = label:LLM-Review=MAX AND -has:unresolved
Verified requirement = label:Verified=MAX
```

The enforced source is [`infra/gerrit/project.config`](../gerrit/project.config). [ADR 0047](../../docs/adr/0047-retire-autolander-rebase-if-necessary.md) defines the Rebase If Necessary submit action and the `Verified` carry across `TRIVIAL_REBASE`. [ADR 0091](../../docs/adr/0091-llm-review-carry-trivial-rebase.md) records the `LLM-Review` carry across `TRIVIAL_REBASE`.

---

## Current enforcement

Both label functions use `NoBlock`. Separate submit requirements enforce the votes. The `Verified` requirement is active because its block has no `applicableIf = is:false` line.

```
[submit-requirement "Verified"]
	description = CI (build/test/lint/typecheck via GitHub Actions) must pass (MAX).
	submittableIf = label:Verified=MAX
	canOverrideInChildProjects = false
```

The current vote carry conditions are defined in the same file.

| Label | Carries across | Drops across |
|---|---|---|
| `LLM-Review` | `NO_CODE_CHANGE`, `MERGE_FIRST_PARENT_UPDATE`, `TRIVIAL_REBASE` | `REWORK` |
| `Verified` | `NO_CODE_CHANGE`, `TRIVIAL_REBASE` | `MERGE_FIRST_PARENT_UPDATE`, `REWORK` |

---

## A. Back out to single-vote gating

Use this rollback when the CI bridge or GitHub Actions cannot produce a `Verified` vote and the active requirement prevents submission. Add `applicableIf = is:false` to the `Verified` submit requirement, then push `refs/meta/config` through the declarative setup tool.

1. Edit `infra/gerrit/project.config` and add the inactive line under `[submit-requirement "Verified"]`.
   ```
   [submit-requirement "Verified"]
   	description = CI (build/test/lint/typecheck via GitHub Actions) must pass (MAX).
   	applicableIf = is:false
   	submittableIf = label:Verified=MAX
   	canOverrideInChildProjects = false
   ```
2. Preview the configuration diff, then push it.
   ```bash
   DRY_RUN=1 bash infra/gerrit/setup-project.sh   # review the staged diff
   bash infra/gerrit/setup-project.sh             # push refs/meta/config
   ```
   The command requires the Gerrit administrator SSH key. See the script header for environment variables.
3. Confirm on an open change that the `Verified` requirement is not applicable. A change with `LLM-Review=MAX` and no unresolved comments can then become submittable. The `Verified` label continues to record votes but does not block.

This rollback changes one applicability line. It does not remove the label, voter identity, credentials, or GitHub Actions workflow. For retirement, follow Section 4 of `g2p-ci-credentials.md` after applying this rollback.

To stop dispatch without changing the gate, disable the hooks plugin g2p execution or its GitHub credential. If the requirement remains active, submission will stop until `Verified` returns or this rollback is applied. Replication and the review bot have separate controls in `review-bot-ops.md`.

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

3. **Preserve the project submit action.** Do not delete the `[submit]` block when retiring the feature-branch flow. `action = rebase if necessary` is the project-wide integration decision from ADR 0047. A return to Fast Forward Only is a separate rollback that must also restore the strict `Verified` copy condition. It is not part of feature-branch retirement.

4. **Bot rollback (if a merge-review image is implicated).** Rolling the review-bot back to the
   prior image is a separate path — see `infra/runbooks/review-bot-ops.md` ("Bot-code rollback =
   redeploy the prior image", ~lines 167–181): `docker tag compose-review-bot:prev …` / the
   `:prev` auto-rollback under continuous auto-deploy.

5. **Apply:** `DRY_RUN=1 bash infra/gerrit/setup-project.sh` to preview the staged diff, then
   `bash infra/gerrit/setup-project.sh` to push `refs/meta/config`. (The full RETIRE path — also
   emptying/deleting the `feature-branch-drivers` group — is ADR-0025 "Group RETIRE".)

---

## B. Restore two-vote gating after a rollback

Restore the active gate only after the CI voter passes the checks in Section C. Confirm that the CI credentials described by `g2p-ci-credentials.md` are installed. The CI service account must remain in `Service Users`.

1. Remove `applicableIf = is:false` from `[submit-requirement "Verified"]` so the block reads as follows.
   ```
   [submit-requirement "Verified"]
   	description = CI (build/test/lint/typecheck via GitHub Actions) must pass (MAX).
   	submittableIf = label:Verified=MAX
   	canOverrideInChildProjects = false
   ```
2. Run `DRY_RUN=1 bash infra/gerrit/setup-project.sh` to review the diff. Then run `bash infra/gerrit/setup-project.sh` to push it.
3. Confirm that an open change requires `LLM-Review=MAX`, `Verified=MAX`, and no unresolved comments before Submit becomes available.

Section A reverses this restoration.

---

## C. End-to-end verification before restoration

Verify the CI loop on a throwaway change while the `Verified` requirement is not applicable. Restore the requirement through Section B only after these checks pass.

1. **Confirm dispatch.** Push a throwaway change with `git push gerrit HEAD:refs/for/main`. Confirm that a `gerrit-verify` run appears in GitHub Actions. If it does not, inspect `journalctl CONTAINER_NAME=compose-gerrit-1 | grep -i gerrit_to_platform` and the `rebar-gerrit-g2p-dispatch-errors` alarm.
2. **Confirm the CI scope.** Verify that the workflow checks the proposed Gerrit patch set with the configured build, test, lint, and type-check jobs.
3. **Confirm vote return.** Verify that the CI service account casts `Verified +1` after success or `Verified -1` after failure. The message must include the GitHub Actions run URL.
4. **Confirm environmental recovery when needed.** Use a `recheck` comment only for a proven environmental failure. Confirm that gerrit-to-platform dispatches a new run and returns a replacement vote on the same patch set.
5. **Confirm changed code drops prior votes.** Push a patch set classified as `REWORK`. Confirm that both prior votes are removed and fresh checks run. `NO_CODE_CHANGE` and `TRIVIAL_REBASE` retain `Verified` under the current configuration. `MERGE_FIRST_PARENT_UPDATE` does not.
6. **Restore the gate.** Follow Section B. Run one more full check and confirm that both votes and the unresolved-comment condition control Submit. Abandon the throwaway change.

---

## C.1 — E2E EXECUTED (proof record, 2026-07-02)

This section is a historical proof record for initial activation. The current enforcement and carry conditions are defined above and in `infra/gerrit/project.config`.

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
- **CI failure handling.** A failed test casts `Verified -1` and requires diagnosis. Use `recheck` only for a proven environmental failure such as a dispatch outage or runner failure. A code correction requires a new patch set. The workflow concurrency group cancels an older in-progress run for the same change.
- **Copy conditions.** `LLM-Review` carries across `NO_CODE_CHANGE`, `MERGE_FIRST_PARENT_UPDATE`, and `TRIVIAL_REBASE`. `Verified` carries across `NO_CODE_CHANGE` and `TRIVIAL_REBASE`. A patch set classified as `REWORK` drops both votes. A feature-branch re-merge classified as `MERGE_FIRST_PARENT_UPDATE` drops `Verified` and requires a new CI result.

---

## See also
- `docs/adr/0020-two-vote-ci-gate.md` — the two-vote gate design + staged rollout.
- `docs/adr/0022-g2p-in-container.md` / `docs/adr/0023-inbound-github-gerrit-ssh-vote.md`.
- `infra/runbooks/g2p-ci-credentials.md` — credential setup / rotation / retire.
- `infra/gerrit/project.config` + `infra/gerrit/setup-project.sh` — the config + push tool.
- `CONTRIBUTING.md` — the contributor-facing two-vote flow + `recheck`.
