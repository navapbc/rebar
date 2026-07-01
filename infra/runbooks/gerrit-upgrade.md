# Runbook — Gerrit upgrade (pinned-tag, snapshot-first, rollback-ready)

Upgrade Gerrit on the rebar box by moving to a **new pinned image tag**, taking a
**snapshot first**, and keeping a one-command rollback. Data lives on the
persistent data volume, so an image swap never touches review history.

## Invariants that MUST persist across the upgrade

These are config/state on the **persistent data volume** (`/var/gerrit`), not in
the image — an upgrade must not regenerate or clobber them:

- **`gerrit.config`** at `site/etc/gerrit.config` — the whole server config.
  Key fields that must NOT change across an upgrade:
  - **`gerrit.basePath`** — the repo storage path (relative to the site). Repos +
    NoteDb resolve from here; changing it orphans every repo.
  - **`gerrit.serverId`** — the UUID NoteDb stamps into review metadata. A new
    serverId makes existing NoteDb review data unreadable. Never let a fresh init
    mint a new one.
  - **`container.javaHome` / JAVA_HOME** — the JRE the image expects. Pin to the
    JRE that ships in the new image's tag; a mismatch fails startup.
- The **SSH host key** (`/rebar/prod/gerrit-ssh-host-ed25519-key`, SSM) — so
  clients don't see a host-key-changed warning after restart.

> Before upgrading, snapshot (below) captures all of this. If anything regenerates,
> roll back and restore.

## Procedure

1. **Pick the target tag.** Pin a specific patch tag — `gerritcodereview/gerrit:<patch>`
   (e.g. `gerritcodereview/gerrit:3.10.2`), **never** `:latest`. Prefer a patch bump
   within the current minor for a low-risk upgrade; a minor/major bump may require
   an explicit reindex/migration step — read the Gerrit release notes first.
2. **Snapshot first** (the rollback safety net). Take an on-demand snapshot of the
   data volume and wait for completion:
   ```bash
   SNAP=$(aws ec2 create-snapshot --region us-east-1 --volume-id vol-06fa2e77a9dd97527 \
     --description "pre-gerrit-upgrade $(date -u +%FT%TZ)" --query SnapshotId --output text)
   aws ec2 wait snapshot-completed --region us-east-1 --snapshot-ids $SNAP
   echo "rollback snapshot: $SNAP"
   ```
3. **Update the pinned tag.** Edit the Gerrit image tag in the compose file
   (`infra/compose/`) to the new patch tag. Commit the change (PR to `main`).
4. **Pull + restart** (on the box, over SSM Session Manager):
   ```bash
   cd /path/to/compose            # where docker-compose.yml lives on the box
   docker compose pull gerrit     # fetch the new pinned image
   docker compose up -d gerrit    # recreate only the gerrit service
   ```
   (Or re-run `infra/scripts/compose-up.sh` if it pins/pulls the stack.)
5. **Verify health.** Wait for Gerrit to come up, then:
   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' \
     https://rebar.solutions.navateam.com/config/server/version   # expect 200
   ```
   The 5-minute observability probe publishes `Rebar/Gate:GerritReachable` and
   `rebar/host:gerrit_healthy`; confirm the `rebar-gerrit-gate-down` alarm returns
   to OK (it flips to ALARM during the restart blip if it exceeds 2 periods).
6. **Reindex if needed.** A minor/major upgrade (or a release note that says so)
   may require an offline reindex. Stop Gerrit, run the bundled reindex, restart:
   ```bash
   docker compose stop gerrit
   docker compose run --rm gerrit gerrit reindex   # uses the persistent site
   docker compose up -d gerrit
   ```
   A patch bump usually needs no reindex. If the change search/UI looks stale or
   queries error after start, reindex.

## Rollback (revert the tag — data is on the volume)

The data volume is untouched by the image swap, so rollback is just reverting the
pinned tag and restarting:

1. Revert the compose tag to the previous pinned patch (git revert the upgrade
   commit, or edit the tag back).
2. On the box: `docker compose pull gerrit && docker compose up -d gerrit`.
3. Re-verify health (the `/config/server/version` 200 check above).

If the upgrade somehow mutated the persistent site (e.g. an irreversible NoteDb
migration, or a regenerated `gerrit.config`/`serverId`), the tag revert alone is
not enough — restore the data volume from the **pre-upgrade snapshot** `$SNAP`
using the restore procedure in `provision-restore.md`, then restart on the old
tag. This is why step 2 (snapshot first) is mandatory.
