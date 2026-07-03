# Runbook — review-bot operations

Operator guide for the rebar **review-bot** (the LLM-Review voter). It reviews each
Gerrit patchset and casts the deterministic `LLM-Review` vote that submit requires
(ADR-0009, ADR-0013). The gate is **fail-closed**: any failure leaves a change
unsubmittable (a `-1`), never silently submittable.

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
push a new patchset).

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
