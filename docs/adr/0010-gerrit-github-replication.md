# ADR 0010 — One-way Gerrit -> GitHub replication (NON-force, mirror-off, deploy-key identity)

**Status:** Accepted (epic d251 / story S5)
**Date:** 2026-06-29

## Context

rebar's canonical history lives in Gerrit (the code-review server), and `main` is
gated there by an automated review + submit requirement (ADR-0013). We need that
published history to also appear on `github.com/navapbc/rebar` — for visibility,
external tooling, and as an off-box copy — WITHOUT making GitHub a second writer.
GitHub must be a strictly downstream mirror of the Gerrit-published branch and
tags, never a source that can feed changes back or diverge.

Gerrit's bundled **`replication`** plugin (Apache-2.0, WAR-bundled in the
`gerritcodereview/gerrit:3.14.1` image, already enabled) does push replication by
shelling out to `git push` over SSH per configured remote.

## Decision

Replicate **one-way, Gerrit -> GitHub**, configured in
`infra/gerrit/replication.config` (applied to the site `etc/replication.config`
by `infra/gerrit/setup-replication.sh`). The contract:

1. **Gerrit is the SOLE writer; GitHub is append-only / fast-forward only.** The
   push refspecs (`refs/heads/main:refs/heads/main`, `refs/tags/*:refs/tags/*`)
   have **no leading `+`**, so they are **NON-force**: any non-fast-forward update
   (rewritten or diverged GitHub history) is **rejected**, never clobbered. This
   is the one-way door — the only safe way GitHub state changes is a
   fast-forward of the Gerrit-published branch.

2. **`mirror = false` (non-destructive).** A mirror push would `--prune` refs on
   GitHub that are absent in Gerrit; we never want replication to delete refs on
   the GitHub side. Only the configured refspecs are pushed.

3. **Scoped to one project (`projects = rebar`).** The remote `url` hardcodes the
   single GitHub repo with **no `${name}` placeholder**, so without an explicit
   `projects` filter the remote would match every Gerrit project and try to push
   them all to the one GitHub repo. `projects = rebar` scopes it correctly.

4. **`replicatePermissions = false` — `refs/meta/config` is NEVER pushed.** Gerrit
   ACLs, group files, and the webhook URL token embedded in `refs/meta/config`
   stay on-box. **ADR-0014 explicitly DEPENDS on this**: the inbound-webhook
   token's "not replicated off-box" property holds only while this stays `false`.
   Flipping it to `true` would mirror `refs/meta/config` (and the token) to
   GitHub and break ADR-0014 — do not change it without revisiting ADR-0014.

5. **Deploy-key identity (least privilege).** Gerrit authenticates to GitHub as a
   per-repo **deploy key** (an ed25519 keypair), not a user PAT. The private key
   is stored in SSM `/rebar/prod/github-replication-deploy-key` (SecureString) and
   materialised into the gerrit user's `~/.ssh` (`/var/gerrit/.ssh` in the
   container) by `infra/gerrit/materialize-deploy-key.sh`, which runs
   **fail-closed before Gerrit starts** (a missing key aborts boot rather than
   silently disabling replication). The public half is registered as a
   write-enabled deploy key on `navapbc/rebar` by `infra/gerrit/register-deploy-
   key.sh` (operator-run `gh`, deliberately kept out of terraform state).

   *Param-name note:* an earlier ticket draft named the SSM param
   `/rebar/prod/github-deploy-key`; the **actual provisioned** name is the more
   specific `/rebar/prod/github-replication-deploy-key`, which all S5 files use.

6. **Finite retries + crash-safe queue.** `replicationMaxRetries = 3` and
   `timeout = 60` bound a failing push (it gives up rather than retrying forever),
   and `replication.eventsDirectory = data/replication` (set in
   `replication.config` — the plugin **ignores** this key in `gerrit.config`)
   persists the push queue so queued replications survive a Gerrit restart.

## Licensing note (BSL)

GerritForge's **2025-09-30 BSL re-licensing** applies to the **pull-replication**
and **multi-site** plugins, **NOT** to this Apache-2.0 core **`replication`** push
plugin. We use only the push plugin. This is verifiable from the running image:
the plugin is WAR-bundled in the Apache-2.0 `gerritcodereview/gerrit:3.14.1`
image, and the plugin's `LICENSE` can be inspected inside the image to confirm it
is Apache-2.0. So S5 carries no BSL obligation.

## Operations

- **Kill-switch.** Rename/remove the site `replication.config` and restart Gerrit
  (no config -> no remotes -> no pushes). See `setup-replication.sh`. Restore by
  moving the file back and restarting.
- **Deploy-key rotation lifecycle.** Generate a new keypair -> add the new public
  key as a deploy key on `navapbc/rebar` -> overwrite the SSM param with the new
  private key -> re-run `setup-replication.sh` (re-materialise + reload/restart)
  -> once healthy on the new key, remove the OLD deploy key from GitHub. (Detailed
  in `setup-replication.sh`.)
- **Loading the config.** Remote plugin admin is **disabled** on this instance, so
  a brand-new `replication.config` is loaded at Gerrit **startup** — expect a
  restart on first apply. `autoReload = true` then re-reads the file on subsequent
  changes once the plugin is loaded.

## Consequences

- **GitHub can never diverge silently.** A forced/rewritten GitHub history is
  rejected by the NON-force refspecs; the rejection surfaces in `replication_log`
  and trips the CloudWatch alarm (`infra/terraform/monitoring_s5.tf`, watching the
  `rebar/host:replication_errors` metric the host probe publishes from
  `replication_log` ERROR / REJECTED_NONFASTFORWARD / max-retry lines).
- **No secret leakage off-box.** `replicatePermissions = false` keeps
  `refs/meta/config` (ACLs + webhook token) on the box, satisfying ADR-0014's
  dependency. Only `main` and tags reach GitHub — never `refs/changes/*` or
  `refs/meta/*`.
- **Tight blast radius on the GitHub side.** The deploy key has write access to
  one repo only and is rotatable via a single scripted lifecycle.
- **Coupling to ADR-0014.** This ADR and ADR-0014 are joined at
  `replicatePermissions = false`; changing it requires re-evaluating both.
