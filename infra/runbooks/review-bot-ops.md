# Runbook — review-bot operations

Operator guide for the rebar **review-bot** (the LLM-Review voter). It reviews each
Gerrit patchset and casts the deterministic `LLM-Review` vote that submit requires
(ADR-0009, ADR-0013). The gate is **fail-closed**: any failure leaves a change
unsubmittable (a `-1`), never silently submittable.

---

## The box (where prod runs)

Everything below runs on the single prod EC2 host. **You need its instance id and
region for every SSM / AWS-CLI operation** in this runbook — they are:

| | |
|---|---|
| **Instance** | `i-00880b2c7f13527c5` |
| **Region** | `us-east-1` (single-sourced as `var.aws_region` default in `infra/terraform/variables.tf`; the Terraform backend is also pinned to `us-east-1` in `infra/terraform/versions.tf`) |
| **Account** | `896586841071` |

The host is **SSM-managed** (no public SSH); connect with Session Manager:

```bash
aws ssm start-session --region us-east-1 --target i-00880b2c7f13527c5
# non-interactive (scripted) equivalent — run a command and read its output:
aws ssm send-command --region us-east-1 --instance-ids i-00880b2c7f13527c5 \
  --document-name AWS-RunShellScript --parameters 'commands=["<cmd>"]'
```

`/opt/rebar` is a *copy* of `main` (not a git checkout) that `autodeploy.sh` keeps in
sync (ADR-0026). The `autodeploy.sh` **body** auto-updates in place via that same
`BOT_PATHS` rsync, like any other source — but a commit touching *only* `autodeploy.sh`
(no `BOT_PATHS` path) triggers no deploy, so it will not rsync itself until the next
bot-path deploy; to make such a script-only change live now, hand-deploy it (update the
mirror at `/var/lib/rebar/mirror`, then install the new `infra/scripts/autodeploy.sh`
into `/opt/rebar`, preserving `502:502`). Separately, the **installer**
(`install-autodeploy.sh`) and the `rebar-autodeploy.{service,timer}` unit files *are*
deliberately excluded from in-run re-materialization (self-modification race), so those
always require a hand-deploy.

---

## Manually re-run a review (`/rerun`)

> **This is the operator escape hatch for a change stuck at a fail-closed `-1`.**
> Use it when a *transient* failure (e.g. the LLM was briefly down) cast a `-1`
> that now sticks — the automatic paths will NOT clear it on their own (see WHEN).

**WHERE.** The review-bot receiver exposes `POST /rerun`. It is reached from
outside through nginx at:

```
https://rebar.solutions.navateam.com/review/rerun      (nginx /review/* → receiver /*)
```

i.e. the public `/review/rerun` path maps to the receiver's internal `POST /rerun`.

**HOW.** Auth is the same `?token=` secret as the inbound webhook
(`/rebar/prod/gerrit-bot-token`, constant-time compared). Pass the change as an id
or number. A successful enqueue ACKs **202**:

```bash
TOKEN=$(aws ssm get-parameter --name /rebar/prod/gerrit-bot-token \
  --with-decryption --query Parameter.Value --output text)

curl -sS -X POST \
  "https://rebar.solutions.navateam.com/review/rerun?token=$TOKEN&change=<CHANGE_ID_OR_NUMBER>"
# → 202 Accepted  (the review runs asynchronously on the background worker)
```

Replace `<CHANGE_ID_OR_NUMBER>` with the Gerrit change number (e.g. `1234`) or the
full Change-Id. After the worker runs, confirm the `LLM-Review` vote flipped on the
change's current revision.

**WHEN.** Use `/rerun` to recover a change stuck at a fail-closed `-1` **without
amending the patchset** (no new patchset needed). Contrast the two automatic paths,
neither of which clears a stuck `-1`:

- **Webhook re-delivery** — the dedup ledger + the Gerrit existing-vote guard make
  a re-delivered event a **no-op skip** once any non-zero vote exists.
- **The 5-minute backfill reconciler** (`reconcile.py`) — only re-reviews **vote-LESS**
  changes (a gap a dropped webhook left). It will **NOT** retry a change that already
  carries a `-1`.

So a transient outage that produced a `-1` will sit there until you `/rerun` it (or
push a new patchset). Contributors also have a self-service path: commenting
**`recheck-review`** on the change re-triggers the review for any latest bot state
that is not a real finding (see `docs/review-policy.md`); `/rerun` remains the
operator-side equivalent and additionally works when the bot cannot read comments.
(**Activation note:** the trigger depends on the `comment-added` webhook event in
`infra/gerrit/webhooks.config`; after that file changes, an operator must re-run
`infra/gerrit/service-user.sh` to push the rendered config to `refs/meta/config`.)

**SEMANTICS (budget).** Both `/rerun` and an accepted `recheck-review` trigger also
**reset the change's retry-budget row** in the dedup store, so a change that
previously exhausted its automatic retries gets a fresh budget for the forced run.

**SEMANTICS.** `/rerun` enqueues the change's current revision with a force marker;
the worker calls `voter.review_and_vote(force=True)`, which **bypasses both
short-circuits** — the dedup row AND the Gerrit existing-vote check — and re-reviews
from scratch, overwriting the stuck vote with a fresh verdict. It is **still
fail-closed**: `force` only requests a *fresh review*; the verdict is computed the
same way, so a rerun can **never** force a PASS the reviewer did not produce. (A
normal, non-forced call against a change that already has a vote still skips.)

See **ADR-0009 §7a** ("Manual `/rerun` recovery endpoint") for the full design.

---

## Reading voter failures (`VOTER_ERROR` + the alarms)

When the voter cannot cast a vote (Gerrit 4xx/5xx, clone/diff failure, LLM
unavailable, expired bot token) it writes a structured `VOTER_ERROR` JSON line to
stderr and stays fail-closed (the change keeps its `-1`/no vote).

- The host observability probe (`infra/scripts/observability.sh`, §4) greps the
  review-bot container's journald for `VOTER_ERROR` markers and publishes
  `rebar/host:voter_errors` (a per-interval delta). The
  **`rebar-gerrit-voter-errors`** alarm (`infra/terraform/monitoring_s4b.tf`) fires
  on any new errors. There is **no break-glass** to disable the submit requirement —
  the fix is to RESTORE the voter (token / LLM reachability / receiver), then `/rerun`
  the affected changes.
- The **`rebar-gerrit-replication-errors`** alarm (`monitoring_s5.tf`) watches
  `rebar/host:replication_errors` — Gerrit→GitHub replication failures
  (`REJECTED_NONFASTFORWARD` / max-retry / `[ERROR]` in the replication_log). A
  non-fast-forward rejection means GitHub diverged from Gerrit (the one-way-door
  contract was violated) — investigate before re-enabling replication.
- The **`rebar-gerrit-gate-down`** alarm (`monitoring.tf`) fires when Gerrit itself
  is unreachable; pair it with the EC2 status-check alarms for host-down.

Inspect the raw markers:

```bash
journalctl CONTAINER_NAME=compose-review-bot-1 --no-pager -o cat | grep VOTER_ERROR | tail
journalctl CONTAINER_NAME=compose-review-bot-1 --no-pager -o cat | grep RECONCILE_DEGRADED | tail
```

## The reconciler cursor

The backfill reconciler persists a **cursor** (the newest events-log event time it
processed) at `<dedup dir>/reconcile_cursor` on the data volume, fetching only
events since that cursor each pass (ADR-0009 §7b). The cursor is purely an
optimization — idempotency is owned by the per-`(change, revision)` dedup ledger +
the authoritative Gerrit vote-existence check, so deleting/resetting the cursor (it
re-scans) can never double-vote. If `RECONCILE_DEGRADED` appears (events-log absent
or malformed), backfill is degraded: the cursor does NOT advance and no vote is
cast — the live webhook path still works, and a missed change stays vote-less =
unsubmittable (never submittable-but-unreviewed). Restore events-log to recover.

## Merge-change ops (feature-branch flow — epic 88ab / ADR-0025)

Feature-branch work (CONTRIBUTING.md §4) adds one review shape the bot must handle: the
`--no-ff` merge change that lands `refs/heads/feature/<name>` into `main`, plus the
re-merges that refresh it. Operate them as follows.

**What the bot reviews on a merge change.** A merge change's `patchset-created` event is
reviewed like any other: the bot clones the change ref and reviews the **auto-merge
delta**, then casts `LLM-Review`. There is no special merge path in the receiver — a merge
patchset is just a patchset. The asymmetry lives in Gerrit's copyCondition (ADR-0025), not
the bot: on a `MERGE_FIRST_PARENT_UPDATE` re-merge (first parent moved, feature tip
unchanged) **`LLM-Review` carries and is NOT re-requested**, while `Verified` re-runs. So
after a re-merge you should see CI run again but **no** new `LLM-Review` review fire — that
is correct, not a dropped webhook.

**409 semantics on merge changes.** The bot casts a vote via Gerrit REST and treats a
**409 Conflict** as a benign "already-voted / label-not-settable-right-now" race, staying
fail-closed. On merge changes a 409 most often means the bot's *own image is behind the
merge-review code that landed on `main`* (S2's merge-review code was on `main` while the
running bot was pre-S2, so it 409'd on merge changes — the exact condition ADR-0026's
continuous auto-deploy now prevents). **A burst of 409s specifically on merge changes ⇒
suspect a stale bot image, not a Gerrit fault.** Check the running image against `main`
(see rollback below) and let autodeploy converge (or redeploy).

**`/rerun` on merges.** `/rerun` works identically on a merge change — pass the merge
change's number/Change-Id; the worker looks up its current revision and re-reviews from
scratch, bypassing both short-circuits, still fail-closed. Use it to clear a stuck merge-change
`-1` from a transient outage **without** amending the merge commit (which would otherwise
re-run everything). Note `/rerun` only refreshes `LLM-Review`; a stuck `Verified` is CI's —
comment `recheck` on the change for that.

**S2 log signals to watch.** In the review-bot journald
(`CONTAINER_NAME=compose-review-bot-1`), the merge-review path emits:

```bash
journalctl CONTAINER_NAME=compose-review-bot-1 --no-pager -o cat \
  | grep -E 'merge_detection|merge_change_409_guard|merge_change_review|voter_voted|MERGE_CHANGE_ERROR' | tail
```

- `merge_detection` — the bot recognised the patchset as a merge (into `main` or a
  `feature/*` branch); logged for EVERY change with `parent_count` + `is_merge`.
- `merge_change_409_guard` — fires ONLY on a merge: the bot took the auto-merge-delta path
  and deliberately avoided the bare `/patch` endpoint (which 409s on a >=2-parent commit).
  Its presence confirms the 409 guard engaged; absence on a change you expected to be a merge
  means Gerrit flattened it to one parent (check `merge_detection`'s `parent_count`).
- `merge_change_review` — it ran the review on the merge's auto-merge delta.
- `voter_voted` — it successfully cast the `LLM-Review` vote (the write-on-success signal).
- `MERGE_CHANGE_ERROR` — a merge-specific failure (bad merge parent, auto-merge/diff
  failure). Fail-closed: the change keeps its `-1`/no-vote. Treat like a `VOTER_ERROR` —
  restore the bot, then `/rerun` the affected merge change.

**Feature-branch ACL signals (NOT in the bot's path).** Branch-create / merge-push refusals
for non-members of `feature-branch-drivers` (ADR-0025) are enforced **natively by Gerrit** —
the review-bot is not involved and emits no metric. The signal is **Gerrit's** sshd/httpd
audit log, not the bot's:

```bash
journalctl CONTAINER_NAME=compose-gerrit-1 --no-pager -o cat | grep -Ei 'refs/heads/feature/|merge|not permitted|not allowed' | tail
```

**Stale-branch inventory.** Gerrit does not auto-prune merged or abandoned `feature/*` refs, so
they accumulate. `infra/gerrit/feature-branch-inventory.sh` enumerates the live `feature/*`
branches and, per branch, classifies it **MERGED-BACK** (tip already reachable from `main` —
safe to delete) vs **ABANDONED** (never merged, no recent activity), and flags any inactive
beyond the **14-day** lifetime cap (CONTRIBUTING.md §4h). It is **read-only by default** — it
prints the classification plus the owner-confirmed delete commands rather than running them, so
a driver reviews before pruning:

```bash
bash infra/gerrit/feature-branch-inventory.sh   # read-only: classify + suggest deletes
```

Deleting `feature/*` refs needs the `feature-branch-drivers` Delete Reference grant (ADR-0025);
run an emitted delete only after confirming with the branch owner.

**Bot-code rollback = redeploy the prior image.** A bad bot deploy (e.g. a merge-review
regression) rolls back by restoring the previous image:

```bash
docker tag compose-review-bot:prev compose-review-bot:latest \
  && (cd infra/compose && docker compose up -d review-bot)
```

Under continuous auto-deploy (ADR-0026 — see the section below) this is usually
**automatic**: after `up -d` the health check gates success, and a failed health check
**auto-rolls-back to `:prev`** and does not advance `deployed-sha` (marker `bot-unhealthy`
→ the `rebar-autodeploy-errors` alarm). So a bad merge-review image self-heals to the
last-known-good bot; a *fix-forward* on `main` deploys promptly (a new SHA resets the
backoff). Manual rollback above is the escape hatch if you need to pin `:prev` while
investigating.

## Kill-switch: disable replication

If replication to GitHub is misbehaving (e.g. repeated non-fast-forward attempts)
and you need to stop it WITHOUT taking Gerrit down: disable the `replication`
plugin's remote so Gerrit stops pushing, leaving the review gate fully functional.
On the box (SSM Session Manager):

```bash
# Inspect the replication config (the remote is defined here).
cat /var/gerrit/site/etc/replication.config
# Disconnect the GitHub remote (set replicateOnStartup off + comment the remote's
# url, OR set its `remote.<name>.url` to empty), then reload the plugin:
docker compose exec gerrit gerrit plugin reload replication
```

Merged code is still safe on the GitHub mirror up to the last successful push;
unmerged review work continues in Gerrit. Re-enable by restoring the remote url and
reloading the plugin once the divergence is resolved.

## Kill-switch: revert the LLM path from Bedrock to direct Anthropic

The review-bot's LLM inference runs through **AWS Bedrock** (story eb6e). If Bedrock
misbehaves — throttling, a region outage, a model id that stops resolving, an IAM or
inference-profile change — this is how to put the gate back on the **direct Anthropic
API** without rebuilding the image or touching Gerrit.

> This safety net is written and landed **before** the cutover itself, by design — an
> operator must never be looking for the escape hatch after the flip. The three env vars
> below therefore appear in `docker-compose.yml` only from the cutover commit onward; if
> `docker compose exec review-bot env | grep REBAR_LLM_` shows no class-slot overrides,
> the bot is still on the direct-Anthropic path and there is nothing to revert.

### The lever: delete the three class-slot env vars, recreate the container

The lever is the **three model-class-slot env vars** in the `review-bot` service's
`environment:` block in `infra/compose/docker-compose.yml` — the resolver reads them as
`REBAR_LLM_<CLASS>_MODEL` (`src/rebar/llm/model_classes.py`, `_env_override`), one per
class slot:

```yaml
      REBAR_LLM_FRONTIER_MODEL: "bedrock:<opus inference-profile id>"
      REBAR_LLM_STANDARD_MODEL: "bedrock:<sonnet inference-profile id>"
      REBAR_LLM_TRIVIAL_MODEL: "bedrock:<haiku inference-profile id>"
```

**Delete all three lines and recreate the container.** That is the whole switch.

*Fast path (seconds, on the box — but TRANSIENT, see the caveat below):*

```bash
# SSM Session Manager onto the box, then:
cd /opt/rebar/infra/compose             # the deployed COPY of main (autodeploy's DEPLOY_REPO)
sudo -e docker-compose.yml              # delete the three REBAR_LLM_*_MODEL lines
docker compose up -d --force-recreate review-bot
# Confirm the overrides are gone from the running container:
docker compose exec review-bot env | grep REBAR_LLM_ || echo "no class-slot overrides set"
```

> **CAVEAT — the on-box edit does not survive the next deploy.** `/opt/rebar` is a *copy*
> of `main` that `autodeploy.sh` refreshes with `rsync -a --delete`, and
> `infra/compose/docker-compose.yml` is in that script's `BOT_PATHS`. So the next `main`
> advance touching any bot path silently restores the three Bedrock lines and puts the gate
> back on Bedrock. Use the fast path to stop the bleeding, then land the **durable** revert:
> a commit to `main` deleting the three lines, which autodeploy applies on its own
> (rebuild + recreate `review-bot`). Do not leave the box hand-edited.

**Why deleting them is sufficient.** With no per-class env override set, rebar falls
back to the **built-in class defaults** in `src/rebar/llm/model_classes.py`
(`_DEFAULT_MODEL_BY_CLASS`): `frontier` = `claude-opus-4-8`, `standard` =
`claude-sonnet-4-6`, `trivial` = `claude-haiku-4-5`. None of these carries a provider
qualifier, so `infer_provider` resolves each to the **`anthropic:`** provider — i.e.
deleting the overrides restores the direct-Anthropic path exactly. Nothing else in
`infra/` competes for that resolution: **`REBAR_LLM_MODEL` appears nowhere under
`infra/`**, so there is no lower-precedence infra-set value left behind to surprise you.

**Do NOT reach for `REBAR_LLM_MODEL` here.** It is a **deprecated alias** (scheduled for
removal — see `src/rebar/_deprecations.py`), and its deprecation shim
(`model_classes._deprecated_bare_model`) fans a single value out to **all three** class
slots at once. Setting it would collapse the per-pass frontier/standard split the review
kernel depends on (frontier for the review pass, standard for the verifier downgrade) —
the exact defect the class slots were introduced to fix. Set the three per-class vars, or
set none of them.

### `ANTHROPIC_API_KEY` stays in the container — deliberately

`ANTHROPIC_API_KEY` is **retained** in the review-bot container even while the bot runs
on Bedrock, precisely so this lever has a working credential the moment you pull it. A
kill-switch that needs a secret provisioned before it works is not a kill-switch.

It is also **not expressible** to "retire the key for this container", because the
secrets pipeline writes ONE shared `.env`:

- `infra/scripts/fetch-secrets.sh:81` reads `anthropic-api-key` via `get_param`, the
  **REQUIRED** (fail-fast) reader — a missing/empty/`CHANGEME` value aborts the whole
  fetch before the `.env` is rewritten.
- `infra/scripts/fetch-secrets.sh:116` writes `ANTHROPIC_API_KEY=` into that single
  `.env`.
- That one file is named by an `env_file: [- .env]` pair under **both** services in
  `infra/compose/docker-compose.yml` — at `:113-114` (`review-bot`) and `:196-197`
  (`opcert`). There is no second, per-service env file to differentiate them.
- **`opcert` consumes it** for its own LLM gate ops. So removing the key from the `.env`
  would break opcert, and there is no per-service split to remove it from `review-bot`
  alone.

Leave it in place. Its presence costs nothing while Bedrock is in use (the Bedrock path
authenticates off the ambient AWS credential chain — the instance role — and reads no
rebar-managed key) and it is what makes the revert above instant.

### Retry / timeout posture on Bedrock: botocore stock defaults ONLY

Know this before you tune anything during a Bedrock incident:

- `rebar.llm.bedrock_model.build_bedrock_provider` ends at
  `src/rebar/llm/bedrock_model.py:88` with `return BedrockProvider(region_name=region)`.
  It passes **no** `botocore.config.Config(retries=...)` and opens no custom client or
  transport. The Bedrock client therefore runs on **botocore's stock client defaults** for
  retries, retry mode, and socket/connect timeouts.
- The tenacity retry envelope (`AsyncTenacityTransport` + `RetryConfig` +
  `wait_retry_after`) and the `httpx.Timeout` wiring live **only** in
  `src/rebar/llm/anthropic_model.py` (`_build_retrying_anthropic_model`), on the
  direct-Anthropic transport.
- Consequence: **`llm_retry_max_attempts` and `timeout_s` do NOT apply to Bedrock.**
  Turning them up during a Bedrock throttling incident changes nothing. Your actual
  options are: wait out the throttle, or pull the kill-switch above and let the
  direct-Anthropic path (which *does* honour those settings) carry the gate.

This describes the posture **as it stands**, not a design intent. The gap is tracked as
bug `61d8-ff23-8ee0-4289` ("Bedrock ignores `llm_retry_max_attempts` and `timeout_s`: no
botocore retry envelope", discovered from eb6e) — check that ticket before assuming the
paragraph above is still current.

Reverting back to Bedrock is the same edit in reverse: restore the three
`REBAR_LLM_*_MODEL` lines and `docker compose up -d --force-recreate review-bot`.

## Where the logs live

All review-bot logs are structured JSON on the container's stderr/stdout, shipped to
journald by compose's journald driver under `CONTAINER_NAME=compose-review-bot-1`:

```bash
journalctl CONTAINER_NAME=compose-review-bot-1 --no-pager           # full log
journalctl CONTAINER_NAME=compose-review-bot-1 -f                   # follow live
journalctl CONTAINER_NAME=compose-review-bot-1 --since '1 hour ago' # recent window
```

Each line carries `timestamp, change_id, revision_id, vote_value, http_status,
error`. Gerrit's own logs are under `CONTAINER_NAME=compose-gerrit-1` and the
replication_log at `/var/gerrit/site/logs/replication_log`.

## Continuous auto-deploy (epic 88ab / story 8903)

The box tracks `main` automatically: `rebar-autodeploy.timer` fires
`rebar-autodeploy.service` (`infra/scripts/autodeploy.sh`) every ~2 min. On a `main`
advance it re-applies ONLY the changed components — the review-bot container
(rebuild+restart), `replication.config` / g2p config (materialise; autoReload, no Gerrit
restart). `refs/meta/config` (project.config) is **detect-only** (logs + a
`AUTODEPLOY_ERROR meta-config-manual` marker; apply it by hand).

**Watch it:**
```bash
journalctl -u rebar-autodeploy.service -f          # deploy runs (JSON "autodeploy" lines)
cat /var/lib/rebar/deployed-sha                    # what main SHA the box is at
cat /var/lib/rebar/deploy-backoff 2>/dev/null      # "<sha> <fail#> <next-epoch>" if backing off
```

**Signals to watch:** `AUTODEPLOY_ERROR` markers -> the `rebar/host:deploy_errors` metric
(observability.sh §4d) -> the `rebar-autodeploy-errors` CloudWatch alarm. Reasons:
`fetch-failed`, `config-invalid` (config-check rejected the new config — should be rare, the
CI config-gate blocks malformed config from reaching main), `materialise-failed`,
`bot-build-failed`, `bot-unhealthy` (health check failed -> **auto-rolled-back to `:prev`**),
`meta-config-manual` (project.config change needs a manual apply).

**Diagnosing `bot-unhealthy`:** the rollback replaces the failing container immediately, so
autodeploy captures a bounded tail of its output into the deploy journal first — look for the
`bot-unhealthy diagnostics — last N lines of review-bot:` line right above the marker. That
tail is what separates a **broken image** (a traceback / immediate exit) from a container that
was merely **slow** (startup lines that simply had not finished), which have opposite
remediations. The readiness deadline is `HEALTH_TIMEOUT` (default 120s), deliberately set above
the app's own worst-case cold-start budget — chiefly the 60s store write-lock budget
`run_ensures()` may spend; see the comment on the tunable in `autodeploy.sh`.

**Failure behaviour (fail-safe):** a failed deploy keeps the **last-known-good** review-bot +
config live (the gate is never frozen by a bad deploy). The loop retries with **capped
exponential backoff** (60s→15m), keyed to the target SHA — a NEW `main` tip resets the
backoff (a fix-forward deploys promptly). It never auto-disables.

**A stuck bad `main`:** if a deploy keeps failing on the same SHA, the box stays on the prior
good SHA and backs off; land a fix-forward on `main` (it deploys immediately). Inspect with
`journalctl -u rebar-autodeploy`.

**Back-out (disable auto-deploy):**
```bash
sudo systemctl disable --now rebar-autodeploy.timer     # stop auto-deploy
```
The manual deploy path (`compose-up.sh`, `setup-*.sh`, `materialize-g2p-config.sh`) is
unchanged and still works. Re-enable with `systemctl enable --now rebar-autodeploy.timer`.

**Bot-code rollback (manual):** `docker tag compose-review-bot:prev compose-review-bot:latest
&& (cd infra/compose && docker compose up -d review-bot)` restores the prior image.

## Voter errors — 409 "change is closed" (bug c943)

**Symptom.** The `rebar/host:voter_errors` metric spikes and the `rebar-gerrit-voter-errors`
alarm flaps; journald shows repeated
`VOTER_ERROR {… "http_status":409, "error":"post_vote: … HTTP 409: change is closed"}`.

**Root cause (2026-07, resolved).** The backfill reconciler re-selected MERGED/ABANDONED
changes (no open-status filter) and cast LLM-Review on them, drawing a 409; the failure
wrote no dedup row, so the same change was re-attempted every pass — amplified by the
review-bot container having no persistent state volume (its reconcile cursor reset on every
auto-deploy, forcing a full events-log re-scan). Fixed by: the `reconcile.py` open-status
filter, `voter.py` 409-terminal handling (record + no `voter_errors` increment), and a
persistent `gerrit_reviewbot` volume.

**Votes-dropped confirmation.** No legitimate `LLM-Review` vote was dropped by this bug. All
recurring 409 change_ids were confirmed **ABANDONED** via Gerrit REST
(`GET /a/changes/<id>` → `"status":"ABANDONED"`); an abandoned change is unsubmittable and
needs no vote. Open (`NEW`) changes voted normally throughout (webhook POSTs 202,
`/review/health` 200). To re-confirm after any recurrence: pull the change_ids from the
`VOTER_ERROR` markers and check each `status` via Gerrit REST — a 409 batch that is entirely
non-`NEW` means no open change was affected.

**Logging decision (review-bot logs → CloudWatch).** DECISION: do **not** ship review-bot
logs to a CloudWatch Logs group at this time. Rationale: the host is SSM-managed, so journald
is reachable read-only on demand (`aws ssm send-command … AWS-RunShellScript` →
`journalctl -u …`), which was sufficient to root-cause this incident; a CloudWatch Logs group
adds cost + a log-shipping agent for a single-host, low-volume service. Revisit if remote
diagnosis frequency grows or the host becomes multi-instance — the follow-up would be a
CloudWatch agent config + a `/rebar/reviewbot` log group in `infra/terraform`.

## Disk full — snapshot-leak recovery (incident 2731 / bug 9d7c)

**Symptom.** The `rebar-root-disk-pressure` alarm (`monitoring_autodeploy.tf` —
`rebar/host:root_disk_used_percent` > 85%) pages, and/or `rebar/host:voter_errors`
spikes with clone/checkout failures in journald. Every review clones the change into
the content-addressed snapshot store on the **ROOT** disk (`/tmp/rebar-gate-snapshots`)
and builds docker images/build-cache there too; when the root filesystem fills, a clone
or subprocess can **hang** or fail mid-review and each `LLM-Review` vote fail-closes.

**Automated defenses now in place (verify first, then recover manually only if needed).**

- **Snapshot-cache janitor.** The receiver's FastAPI lifespan starts a background
  snapshot-cache janitor (`rebar._snapshot.start_background_janitor`) that reclaims
  `/tmp/rebar-gate-snapshots` on a free-space watermark + max-age policy (tunables via
  `REBAR_GATE_*` env / the `[snapshot]` config table). Before this, the reclamation code
  existed but no production process ran it and the store grew unboundedly (694M observed).
  Confirm it started: `journalctl CONTAINER_NAME=compose-review-bot-1 --no-pager -o cat | grep -i 'snapshot' | tail`.
- **Root-disk-pressure alarm.** `rebar-root-disk-pressure` (above) now pages on sustained
  root-fs pressure (2-of-3 5-min periods > 85%) so exhaustion is caught before it silently
  fail-closes the gate.
- **Hung-review timeout.** The single background worker wraps each review in a bounded
  wall-clock timeout (`REVIEW_TIMEOUT_SECONDS`, default 1200s / 20 min — see
  `src/rebar/review_bot/app.py`). A review that hangs indefinitely (a clone/subprocess/LLM
  call blocked — as when the disk filled mid-clone) is abandoned with a countable
  `VOTER_ERROR` timeout marker and the worker moves on to the next change, so one hung
  review can no longer silently wedge the worker and back the whole queue up behind it.
  Lower it temporarily (e.g. `REVIEW_TIMEOUT_SECONDS=300`) if reviews are wedging under
  active disk pressure; restore the default once healthy.

**Manual recovery (when the box has already filled).** Reclaim root-disk space, clear the
leaked snapshot store, and restart the receiver so the janitor + worker come back clean:

```bash
# 1. Reclaim docker image / build-cache storage on the root disk.
docker builder prune -f          # add `docker image prune -af` if images dominate

# 2. Clear the leaked snapshot store (safe: content-addressed cache, rebuilt on demand).
rm -rf /tmp/rebar-gate-snapshots/*

# 3. Restart the review-bot service so the lifespan re-launches the janitor + worker.
(cd infra/compose && docker compose up -d review-bot)

# 4. Confirm recovery.
df -h /                          # root fs back under the 85% alarm threshold
curl -s localhost:8000/health    # (or /review/health via nginx) → {"status":"ok"}
```

Then `/rerun` any changes whose `LLM-Review` fail-closed during the outage (see "Manually
re-run a review" above) — the janitor keeps the store bounded thereafter, and the
`rebar-root-disk-pressure` alarm clears once `df` drops back under 85%.
## Disk-full triage (root-volume exhaustion — bug 7a41)

**Symptom.** The box's ROOT filesystem fills up. Because the docker image/build-cache store
and the review-bot's per-change clone workdir both live on root (see **Where the clone workdir
lives** below), exhaustion fail-closes the gate: `LLM-Review` votes go to `-1` — typically a
`[LLM-Review: BLOCK — coverage-gap (…)]` infra veto and/or a `VOTER_ERROR` line (clone/diff
failure when the clone cannot write), and CI/deploy steps error on "no space left on device"
(image builds and `docker compose up` fail, so autodeploy may mark `bot-build-failed`).

**Diagnose.** Confirm it is disk before anything else:

```bash
df -h /                                    # root filesystem — the one that fills
docker system df                           # images + build cache footprint (the usual culprit)
du -sh /tmp/rebar-gate-snapshots 2>/dev/null   # leaked gate snapshot dirs (see below)
du -sh /tmp/reviewbot-* 2>/dev/null        # leaked per-change clone workdirs
docker builder du 2>/dev/null || docker system df -v | sed -n '/Build cache/,$p'  # builder cache detail
```

The docker builder cache and dangling images are the most common consumers; the gate snapshot
store (`/tmp/rebar-gate-snapshots`, overridable via `REBAR_GATE_TMPDIR`) and leaked
`reviewbot-*` clone dirs are secondary.

**Recover.** Reclaim space, largest lever first:

```bash
docker builder prune -f          # drop the build cache (rebuilds are slower once, not lost)
docker image prune -f            # drop dangling (untagged) images — tagged :prev/:latest are kept
rm -rf /tmp/rebar-gate-snapshots/tmp/*   # clear in-progress/leaked gate snapshot builds
rm -rf /tmp/reviewbot-*          # clear leaked per-change clone workdirs (none should persist)
```

Never delete tagged images (`compose-review-bot:prev` is the rollback lifeline). After
reclaiming, `/rerun` any change left stuck at a disk-induced `-1` (see the `/rerun` section) —
the vote does not clear on its own.

**Where the clone workdir lives (root-volume-resident).** The voter clones each change into
`tempfile.TemporaryDirectory(prefix="reviewbot-")` (`src/rebar/review_bot/voter.py`), which
resolves to the system temp dir (`tempfile.gettempdir()`, typically `/tmp`) on the box's
**ROOT** volume — **not** the `/var/gerrit` data volume. So a burst of reviews consumes root
disk, and this workdir is one of the things the root-disk alarm covers. The dir is
context-managed (removed when the review finishes), so a lingering `reviewbot-*` under `/tmp`
means a crashed/killed review, not normal operation.

**Automated mitigations already in place.** You should rarely have to do the above by hand:

- **Autodeploy prunes on every deploy.** `prune_docker_caches` in `infra/scripts/autodeploy.sh`
  runs `docker builder prune -f --keep-storage` + `docker image prune -f` on **both** deploy
  paths (bot rebuild and the periodic converge), keeping the build cache bounded and dangling
  images swept without touching tagged images.
- **Root-disk alarm.** The `rebar-root-disk-pressure` CloudWatch alarm
  (`infra/terraform/monitoring_autodeploy.tf`) fires when `rebar/host:root_disk_used_percent`
  (published by `observability.sh`, 5-min cadence) stays above 85% (2 of 3 periods). It pages
  before exhaustion so you can prune ahead of a fail-closed gate.
