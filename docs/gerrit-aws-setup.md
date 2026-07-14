# Self-hosted Gerrit + rebar review-bot to gate a GitHub repo

> **OPTIONAL / ADVANCED.** This is **not** part of rebar's standard workflow and is
> not required to use rebar (the CLI, library, MCP server, or Jira sync). It is an
> advanced deployment pattern for teams that want *every* commit to `main`
> automatically code-reviewed by an LLM before it can land — with GitHub demoted to
> a read-only mirror. If you just want rebar's ticket system, ignore this doc.

This walkthrough teaches you to replicate the setup we built (epic `d251`): a
self-hosted **Gerrit** code-review server on AWS, fronted by a **rebar review-bot**
that imports the `rebar.llm` review kernel (the same library the `rebar-mcp` server
exposes) and casts a deterministic `LLM-Review` vote. Gerrit becomes the **sole
writer** of `main`; **GitHub becomes a read-only mirror** that only advances via a
one-way Gerrit → GitHub replication after the gate passes.

The committed artifacts under `infra/` and the ADRs under `docs/adr/` are the
**source of truth**. This doc is the ordered map through them — it points you at the
real scripts and configs rather than duplicating large files inline. Read each
referenced ADR; they justify every decision below.

---

## 1. Overview + topology

```
   developer
      │  git push HEAD:refs/for/main   (a change for review)
      ▼
 ┌─────────────────────── AWS EC2 (single box, IMDSv2, SSM-only admin) ─────────────┐
 │                                                                                   │
 │   ┌───────────┐  patchset-created webhook   ┌──────────────────────────────┐     │
 │   │  Gerrit   │ ───────────────────────────▶│  rebar review-bot (FastAPI)  │     │
 │   │ (3.14.1)  │   (internal compose net)    │  imports rebar.llm review     │     │
 │   │           │ ◀───────────────────────────│  kernel; POSTs LLM-Review vote│     │
 │   └─────┬─────┘   REST: cast LLM-Review      └──────────────────────────────┘     │
 │         │                                                                         │
 │         │ submit-requirement: LLM-Review=MAX AND -has:unresolved                  │
 │         │ (a change is submittable ONLY when the bot votes +1)                    │
 │         ▼                                                                         │
 │   submit → Gerrit `replication` plugin ── one-way, NON-force, deploy-key ──┐      │
 └────────────────────────────────────────────────────────────────────────── │ ─────┘
                                                                               ▼
                                                          github.com/<org>/<repo>  (READ-ONLY mirror;
                                                          repo ruleset: only the deploy key may write main)
```

The integrity property: **submit is fail-closed.** A change is submittable only when
the deterministic `LLM-Review` vote is at its MAX value and there are no unresolved
comments (ADR-0013). A missed webhook, a dropped vote, an LLM outage, or a crashed
receiver can only ever **delay** review (the change stays unsubmittable) — none of
them can let an unreviewed change merge. GitHub can never diverge because replication
is **non-force** and the repo is locked to the replication deploy key (ADR-0010,
ADR-0011).

This is a **proof-of-concept-grade** deployment: single EC2 host, default VPC, the
Gerrit dev-login auth mode. A production hardening pass (private subnets + load
balancer, KMS-CMK state encryption, tighter ingress) is explicitly out of scope
(ADR-0012).

---

## 2. Prerequisites

- **An AWS account + credentials** with permission to create EC2, EBS, EIP, IAM, SSM,
  Route53, DLM, and CloudWatch resources, plus an S3 bucket for Terraform state.
- **A GitHub repo** you control, and a **fine-grained PAT scoped to that one repo**
  with **`Administration: write`** (needed to lock the repo in Step 7). Never commit
  it — inject it as a CI secret / environment variable only (ADR-0011).
- **A DNS zone you control** (e.g. a Route53 hosted zone) and a **subdomain** for the
  box (we used `rebar.solutions.navateam.com`).
- **Terraform `>= 1.10`** — required for S3-native state locking (`use_lockfile`);
  older CLIs *silently* run unlocked (ADR-0012).
- **Docker** + the compose v2 plugin (installed on the box by `compose-up.sh`; you
  need Docker locally only if you want to build/test images).
- An **Anthropic API key** (the review-bot makes live, billable LLM calls).
- The **`nava-rebar[agents]`** extra installed in the review-bot image — this pulls
  the `rebar.llm` review kernel (`pip install .[agents,reviewbot]`, baked by
  `infra/compose/Dockerfile.reviewbot`).
- A workstation with `gh` (GitHub CLI), `git`, the AWS CLI, and an SSH key you'll
  register as the Gerrit admin.

---

## 3. Secret-handling model (read this first)

Internalize this before you touch anything else: **no secret is ever committed,
baked into an image, or left on disk as a static credential.**

**The SSM contract (ADR-0012).** All runtime secrets live as **SSM Parameter Store
SecureString** parameters under the `/rebar/prod/*` namespace, encrypted with the
AWS-managed SSM KMS key. Terraform (`infra/terraform/ssm.tf`) creates exactly seven
parameters as **placeholders** with value `"CHANGEME"` and
`lifecycle { ignore_changes = [value] }`, so Terraform owns the parameter's
*existence and type*, never its *value* — you populate the real values out-of-band
and Terraform never reverts them. The EC2 instance role grants scoped read of
`/rebar/prod/*` + `kms:Decrypt`, so the box reads its own secrets **with no static
AWS keys anywhere on disk**.

The seven parameters you must populate (placeholders shown — substitute your real
values out-of-band, e.g. `aws ssm put-parameter --overwrite --type SecureString`):

| SSM parameter | Holds | Consumed by |
|---|---|---|
| `/rebar/prod/anthropic-api-key` | `CHANGEME` → your Anthropic key | review-bot LLM |
| `/rebar/prod/mcp-hmac-signing-key` | `CHANGEME` → an HMAC signing key | verdict signing |
| `/rebar/prod/gerrit-admin-password` | `CHANGEME` → admin password | admin bootstrap |
| `/rebar/prod/gerrit-bot-token` | `CHANGEME` → bot HTTP token | bot votes + webhook URL token |
| `/rebar/prod/gerrit-ssh-host-ed25519-key` | `CHANGEME` → SSH host key | Gerrit SSH identity |
| `/rebar/prod/github-replication-deploy-key` | `CHANGEME` → ed25519 **private** key | Gerrit → GitHub push (Step 6) |
| `/rebar/prod/alert-endpoint` | `CHANGEME` → alert destination | monitoring |

**Boot-time materialization (ADR-0008).** docker-compose consumes runtime config from
an `env_file`. That `.env` is generated **fresh on each boot** from SSM by
`infra/scripts/fetch-secrets.sh`, which:

- reads only the **subset** of `/rebar/prod/*` the containers actually need
  (`anthropic-api-key`, `mcp-hmac-signing-key`, `gerrit-admin-password`,
  `gerrit-bot-token`) — least exposure;
- authenticates purely via the **instance role** (region discovered via IMDSv2, no
  access keys);
- writes `infra/compose/.env` with **mode 0600**, **git-ignored** and excluded from
  the Docker build context, so the secret never reaches the repo or an image layer;
- is **fail-fast**: if any read fails, or returns empty / `None` / the unpopulated
  `CHANGEME` placeholder, it aborts with exit 1 **before** touching the existing
  `.env` — a partial or stale secrets file is never left in place (all params are
  fetched into shell vars first, then the `.env` is written atomically).

So SSM is the **single source of truth**; the `.env` is a disposable cache. Rotating
a secret in SSM is picked up on the next boot with no redeploy. **Never commit a
real secret** — this doc and every committed artifact use placeholders only
(`<your-domain>`, `<aws-account-id>`, `CHANGEME`). The materialize step for the
replication deploy key (`materialize-deploy-key.sh`) is the same model: SSM
SecureString → on-box `~/.ssh`, fail-closed, never echoed.

---

## 4. Step 1 — Provision the AWS base

> Owns: Terraform state backend, network, EC2 instance role + DLM policy, the seven
> SSM slots, the instance + data volume + EIP + DNS. See ADR-0012; files under
> `infra/bootstrap/` and `infra/terraform/`.

There is a chicken-and-egg with the S3 state bucket, so it is a **two-step apply**:

```bash
# (a) ONE-TIME bootstrap — local state, creates only the state bucket
cd infra/bootstrap
terraform init
terraform plan
terraform apply        # creates rebar-tfstate-<aws-account-id> (versioned, encrypted, prevent_destroy)

# (b) Main stack — uses the S3 backend the bootstrap created
cd ../terraform
terraform init         # backend "s3" points at rebar-tfstate-<aws-account-id>
terraform plan
terraform apply        # network, instance role, DLM, the 7 SSM placeholders,
                       # EC2 instance + EBS data volume + EIP + DNS record
```

What it creates and the guards (ADR-0012):

- A `t4g.large` (Graviton/arm64) AL2023 instance, **IMDSv2-required**, administered
  **only via SSM Session Manager** (no inbound SSH on port 22; the security group
  opens 443 + Gerrit SSH 29418 only).
- A **separate gp3 EBS data volume** mounted at `/var/gerrit` (resolved by NVMe
  volume-id, formatted + mounted by UUID in `user_data.sh`), an **Elastic IP**, and
  a Route53 record for your subdomain (`infra/terraform/dns.tf`).
- **`prevent_destroy`** on the three irreplaceable resources — the state bucket, the
  Gerrit data volume, and the EIP — so a `terraform destroy` or instance replacement
  never silently takes your data, state, or public address. `ignore_changes = [ami]`
  keeps a newly published AMI from force-replacing the running host.
- The **seven `/rebar/prod/*` SSM placeholders** (Section 3). **Populate them with
  real values before this apply completes booting** — `user_data.sh` fails fast on
  any `CHANGEME` value, so the box never boots half-configured.
- A single-owner instance role + DLM lifecycle policy; downstream stories *attach*
  scoped inline policies to the existing role rather than redeclaring it.

---

## 5. Step 2 — Deploy Gerrit + the review-bot

> Owns: the docker-compose stack, the nginx + TLS front, the receiver skeleton,
> admin bootstrap. See ADR-0007 (receiver) + ADR-0008 (secrets); files under
> `infra/compose/`, `infra/nginx/`, `infra/scripts/`.

The compose stack (`infra/compose/docker-compose.yml`) runs **two containers** —
Gerrit (`gerritcodereview/gerrit:3.14.1`, publishes arm64) and the rebar review-bot
(`Dockerfile.reviewbot`, `pip install .[agents,reviewbot]`). **nginx is NOT in the
stack** — it runs as a host package so host certbot can manage the cert and reload it
(ADR-0007).

```bash
# On the box (run as root; uses the instance role for SSM):
sudo infra/scripts/compose-up.sh          # installs docker + compose plugin,
                                           # seeds the persistent site subdirs,
                                           # runs fetch-secrets.sh (SSM -> .env 0600),
                                           # docker compose up -d --build

sudo infra/scripts/install-certbot-timer.sh   # host nginx + Let's Encrypt (HTTP-01
                                               # webroot) + a systemd renew timer
# DOMAIN / EMAIL default to our values; override:
#   sudo DOMAIN=<your-domain> EMAIL=you@example.com infra/scripts/install-certbot-timer.sh
```

Key properties:

- **Persistence model.** The official image runs from the baked site `/var/gerrit`
  and ignores a `GERRIT_SITE` override, so the stack persists **only the stateful
  subdirs** (`git`, `index`, `cache`, `db`, `etc`, `logs`, `plugins`) as `external`
  bind volumes onto the EBS-backed `/var/gerrit/site/*`. `external: true` means
  `docker compose down -v` cannot destroy them, and they ride the daily DLM
  snapshots.
- **The %2F-safe proxy.** nginx (`infra/nginx/rebar.conf.template`) terminates TLS,
  serves Gerrit, and routes `/review/` to the receiver (stripping the prefix, so the
  app sees `/health` and `/webhook`). Gerrit URLs contain encoded slashes (`%2F`), so
  the proxy is configured to preserve them. Gerrit's HTTP port is bound to host
  **loopback** (`127.0.0.1:8080`); only nginx (443) and Gerrit SSH (29418) are
  internet-facing.
- **Admin bootstrap (PoC).** Gerrit runs `auth.type=DEVELOPMENT_BECOME_ANY_ACCOUNT`.
  `infra/scripts/bootstrap-gerrit-admin.sh` registers your admin SSH **public** key
  headlessly by committing it into the `All-Users` NoteDb user branch (REST
  mutations are refused under cookie-auth on a clean slate), then reloads Gerrit:
  ```bash
  sudo ADMIN_PUBKEY="$(cat ~/.ssh/gerrit_admin.pub)" infra/scripts/bootstrap-gerrit-admin.sh
  ```
- **Observability** is installed by `infra/scripts/install-observability.sh` (a
  systemd timer running `observability.sh`) — see Step 8.

---

## 6. Step 3 — Gerrit project + `LLM-Review` label + submit requirement

> Owns: the `rebar` project, the two-vote gate, and the feature-branch flow (§6a). See
> ADR-0013, ADR-0020, ADR-0025; files `infra/gerrit/project.config`,
> `infra/gerrit/setup-project.sh`.

```bash
# From a workstation holding the Gerrit admin SSH key:
GERRIT_HOST=<your-domain> GERRIT_ADMIN_SSH_KEY=~/.ssh/gerrit_admin \
  infra/gerrit/setup-project.sh
# DRY_RUN=1 prints the project.config diff vs live without pushing.
```

This creates the project if absent and pushes the fully declarative
`project.config` to `refs/meta/config`. The **gate design** (ADR-0013, extended to
two votes by ADR-0020 / epic 1fa8):

- **Two independent votes gate submit:** `LLM-Review` (`-1..+1`, the review bot) AND
  `Verified` (`-1..+1`, CI via gerrit-to-platform → GitHub Actions, epic 1fa8). Each
  label has exactly one authorized (bot/CI) voter; there is no human `Code-Review` vote.
- The label is **advisory** (`function = NoBlock` — Gerrit 3.14.1 *rejects* a
  blocking label function); **a submit requirement does the blocking**:
  `submittableIf = label:LLM-Review=MAX AND -has:unresolved`. The inherited
  `Code-Review` submit requirement is disabled on this project (`applicableIf =
  is:false`) so `LLM-Review` is the sole gate.
- **Only `Service Users` + `Administrators` may cast `LLM-Review`** — a developer can
  push a change but cannot self-approve the gate.
- **`copyCondition = changekind:NO_CODE_CHANGE`** — the vote carries across a true
  no-op re-upload (commit-message-only amend) but **not** a `TRIVIAL_REBASE` (whose
  diff is byte-identical against a *moved* base). A real rebase drops the vote and
  forces a fresh review — the safe default for a correctness gate.
- **`change.submitWholeTopic = true`** is set server-level in `gerrit.config`
  `[change]` (a global key, ignored in project.config) so a reviewed multi-change
  feature lands atomically; adopt a `<feature>` topic naming convention.
- **CI `Verified` rollout (epic 1fa8, ADR-0020):** the `Verified` label + its
  submit-requirement are authored in `project.config`, but the requirement ships
  **inactive** (`applicableIf = is:false`) so the label records CI votes without
  blocking submit until the voter exists. Story S6 **activates** it (deletes the
  `applicableIf` line) once the gerrit-to-platform CI voter is proven end-to-end; the
  effective gate then becomes
  `label:LLM-Review=MAX AND label:Verified=MAX AND -has:unresolved`. `Verified` uses
  the same strict `copyCondition = changekind:NO_CODE_CHANGE` reset as `LLM-Review`.

### 6a. Feature-branch flow (epic 88ab / S1 — ADR-0025)

For multi-story features that accumulate off `main` and land via one reviewed merge
change, `setup-project.sh` also provisions the feature-branch machinery (declarative +
idempotent, same script):

- **Merge-carry copyCondition (LLM-Review only):**
  `copyCondition = changekind:NO_CODE_CHANGE OR changekind:MERGE_FIRST_PARENT_UPDATE`.
  A re-merge after `main` advances is a `MERGE_FIRST_PARENT_UPDATE` (first parent moves,
  feature tip unchanged) — the reviewed auto-merge delta is identical, so the LLM vote
  **carries**. **`Verified` is deliberately NOT given this token** — a re-merge is a new
  merge tree, so CI must **re-run** (GerriScary-safe). Net: on a re-merge **LLM-Review
  carries, Verified re-runs**.
- **Submit type pinned:** `[submit] action = merge if necessary` + `mergeContent = true`
  pins the current effective inherited behaviour (`MERGE_IF_NECESSARY` + content merge)
  so the atomic `--no-ff` merge-back can't be broken by an All-Projects default change.
- **`feature-branch-drivers` group + three ACL permission types:** Create Reference +
  Delete Reference on `refs/heads/feature/*`, and Push Merge Commit on both
  `refs/for/refs/heads/main` and `refs/for/refs/heads/feature/*`. Ordinary (non-merge)
  story pushes for review are already allowed to Registered Users by the
  `refs/for/refs/heads/*` grant; only the **merge-commit** push is group-restricted.
  Membership = Administrators (subgroup) + named operating agents
  (`FEATURE_BRANCH_DRIVER_MEMBERS`, space-separated usernames); the script **creates** the
  group if absent and converges membership on every run.
- **`Contributors` group + Submit ACL (landing authorization):** an explicit, exclusive
  `submit` grant on `refs/heads/*` (`exclusiveGroupPermissions = submit`; `submit = group
  Contributors` / `Administrators`) restricts *who may land a change* to authorized
  contributors + admins. Anyone may still push to `refs/for/*` to **propose**; only a
  Contributor/admin can **Submit** — even with both gate votes at MAX. This closes the gap
  where `submit` was inherited from All-Projects (and thus available to any Registered
  User). Membership = Administrators (subgroup) + the accounts in `CONTRIBUTOR_MEMBERS`
  (space-separated usernames; **default `RebarBotNava`**, the landing bot); the script
  **creates** the group (owned by Administrators) if absent and converges membership on
  every run (additive — offboarding is a manual `gerrit set-members Contributors
  --remove <user>`). This is the documented Gerrit Contributor/Developer split (the Go
  model), orthogonal to the two gate labels above.
- **Enforcement + signals:** ACL refusals (non-member merge push / `feature/*` creation)
  are refused natively by Gerrit and recorded in Gerrit's sshd/httpd audit log — the
  review-bot is not in that path (see `infra/runbooks/review-bot-ops.md` "signals to
  watch").
- **Back-out:** delete the `[submit]` block to restore INHERIT; revoke the three ACL
  grants + delete/empty the group to retire the flow (the copyCondition token is inert
  absent merge changes and may be left or reverted). See ADR-0025. To retire the
  **landing-authorization** gate specifically, delete the `submit` ACL lines from
  `[access "refs/heads/*"]` (restores inherited submit to all Registered Users) and
  optionally remove the `Contributors` group.

---

## 7. Step 4 — Review-bot identity + webhooks + events-log

> Owns: the bot Service-User + token, the internal-delivery webhook, the events-log
> plugin. See ADR-0014; files `infra/gerrit/service-user.sh`,
> `infra/gerrit/install-plugins.sh`, `infra/gerrit/webhooks.config`.

```bash
# From the workstation with the Gerrit admin key:
GERRIT_HOST=<your-domain> infra/gerrit/service-user.sh
#   creates/rotates the rebar-review-bot Service User + HTTP token,
#   overwrites SSM /rebar/prod/gerrit-bot-token, renders webhooks.config
#   (substituting the token for __BOT_TOKEN__) and pushes it to refs/meta/config.

# On the box: install events-log (NOT bundled; webhooks IS bundled+enabled):
sudo infra/gerrit/install-plugins.sh      # downloads the pinned events-log jar,
                                           # verifies its sha256, drops it in plugins/
# then reload/restart Gerrit to load it.
```

Design (ADR-0014):

- The bot is a Gerrit **Service User** (`rebar-review-bot`) in the `Service Users`
  group, with a single HTTP token. That **one token doubles** as the bot's REST
  identity *and* the inbound webhook URL token — one secret to rotate, stored once at
  `/rebar/prod/gerrit-bot-token`.
- The `webhooks` plugin has **no HMAC** request signing, so the inbound auth is a
  **two-layer, network-first** control:
  - **PRIMARY — internal-only delivery.** The webhook URL targets the receiver
    **directly over the private docker compose network**
    (`http://review-bot:8000/webhook?token=…`), so a webhook **never traverses the
    public internet or nginx**. The receiver's host port is loopback-bound and port
    8000 is not open in the security group — the network boundary is the real gate.
  - **SECONDARY — URL-embedded token** (bounded exposure): it lives only in
    `refs/meta/config` (Gerrit-access-controlled), is **not replicated off-box**
    (Step 6 keeps `replicatePermissions=false`), and is rotatable via
    `service-user.sh`.
- The **`events-log`** plugin is the backfill source the reconciler reads (Step 5).
  Its REST endpoint is `GET /a/plugins/events-log/events/` — **the trailing slash is
  required**.

Verify the plumbing: `infra/gerrit/smoke-check.sh` asserts the bot token
authenticates both the Gerrit REST `/a/` namespace and a git-over-HTTPS clone of the
repo, and that the events-log endpoint returns events.

---

## 8. Step 5 — The review → vote pipe

> Owns: the receiver that reviews and votes. See ADR-0007 + ADR-0009; files under
> `src/rebar/review_bot/` (`app.py`, `adapter.py`, `voter.py`, `reconcile.py`,
> `gerrit_client.py`, `dedup.py`, `config.py`).

The receiver is a **thin FastAPI app** that **imports the `rebar.llm` review kernel
as a library** — it is *not* the stdio `rebar-mcp` server over HTTP, because Gerrit
speaks plain webhook JSON, not MCP JSON-RPC (ADR-0007). The flow on a
`patchset-created` event (ADR-0009):

1. **`POST /webhook`** validates the inbound `?token=` (constant-time compare),
   enqueues the event, and **ACKs 202 immediately** — an LLM review takes 30s–minutes
   and would blow Gerrit's ~5s webhook socket timeout if processed inline.
2. A background worker takes a per-`(change_id, revision)` **single-flight lock**,
   short-circuits if the vote is already recorded (dedup) or already present on
   Gerrit (the authoritative check), then **clones the change ref** into a temp tree
   and fetches the diff.
3. **The `code_review_decision` seam** (`adapter.py`) is the contract between "what
   the LLM thinks" and "what value the label gets":
   `code_review_decision(diff_text, repo_root, ref) -> {decision, message,
   findings}`. The proven pipe implements it over `rebar.llm.review_code(...,
   source="local", ...)` (`source="local"` because the patchset's ref lives only in
   the clone, not on origin) and maps findings to **PASS/BLOCK by a configured
   blocking-severity threshold** (default `{critical, high}`). The signature is kept
   deliberately small so a richer reviewer can be dropped in with **no receiver
   change**.
4. The decision maps to the vote: **PASS → MAX (+1)** makes the change submittable;
   **BLOCK → BLOCK value (-1)** keeps it unsubmittable. The vote is cast via Gerrit
   REST with a `robot_comments` payload under `/PATCHSET_LEVEL`.
5. **Fail-closed:** a MAX is cast **only on an explicit PASS**. Every error path
   (adapter exception, missing/bad result, clone/diff failure, vote-POST failure)
   leaves the change unsubmittable; the dedup row is written **only on a
   confirmed-successful vote** (write-on-success), and a `VOTER_ERROR` JSON line is
   logged.

Recovery paths:

- **`POST /rerun`** (same `?token=` auth) is the operator escape hatch for a stuck
  fail-closed `-1` (e.g. a transient LLM outage): it looks up the change's current
  revision and re-reviews from scratch, **bypassing both short-circuits**, but is
  **still fail-closed** — a rerun can only request a fresh review, never cast a PASS
  the reviewer did not produce.
- **The backfill reconciler** (`reconcile.py`) runs on startup and every
  `RECONCILE_INTERVAL_SECONDS` (default 300): it queries `events-log` (with a
  persisted, resumable cursor) for patchsets whose current revision has no
  `LLM-Review` vote and re-invokes the same `review_and_vote` (sharing the lock +
  dedup), recovering a dropped webhook. If events-log is unavailable it logs a
  greppable `RECONCILE_DEGRADED` marker, advances no cursor, and casts no vote —
  relying on the live webhook path (degraded but still fail-closed).

No new runtime dependency: the Gerrit client is stdlib `urllib`/`subprocess`, so the
`reviewbot` extra stays `fastapi` + `uvicorn` (the LLM call is the `[agents]` extra).

---

## 9. Step 6 — Gerrit → GitHub replication

> Owns: one-way push to GitHub. See ADR-0010; files
> `infra/gerrit/replication.config`, `infra/gerrit/materialize-deploy-key.sh`,
> `infra/gerrit/register-deploy-key.sh`, `infra/gerrit/setup-replication.sh`.

Gerrit's bundled, Apache-2.0 **`replication`** push plugin replicates **one-way,
Gerrit → GitHub** (ADR-0010). Set up the deploy-key identity, then apply the config:

```bash
# (a) Generate an ed25519 deploy keypair; store the PRIVATE half in SSM:
ssh-keygen -t ed25519 -f ./rebar-replication -N ''
aws ssm put-parameter --overwrite --type SecureString \
  --name /rebar/prod/github-replication-deploy-key --value "$(cat ./rebar-replication)"

# (b) Register the PUBLIC half as a write-enabled deploy key on your repo:
REPO=<your-org>/<your-repo> PUBKEY_FILE=./rebar-replication.pub \
  infra/gerrit/register-deploy-key.sh        # uses your gh auth; kept out of TF state

# (c) On the box — materialize the key + apply the config (then restart Gerrit):
sudo infra/gerrit/setup-replication.sh       # runs materialize-deploy-key.sh first
```

The one-way-door contract (ADR-0010):

- **Gerrit is the SOLE writer; GitHub is fast-forward only.** The push refspecs
  (`refs/heads/main:refs/heads/main`, `refs/tags/*:refs/tags/*`,
  `refs/changes/*:refs/changes/*`) have **no leading `+`** → **NON-force**: a
  rewritten/diverged GitHub history is **rejected**, never clobbered.
- **`refs/changes/*` mirrored for CI (epic 1fa8, ADR-0021).** Per-patchset change
  refs are replicated so the gerrit-to-platform GitHub Actions run can fetch the
  change under test (`checkout-gerrit-change-action`, with a Gerrit-direct fallback on
  replication lag). Scoped to `refs/changes/*` (NOT wildcard `refs/*`) so
  `refs/meta/config` is never included — the webhook-token protection below is intact.
- **`mirror = false`** — non-destructive (no `--prune` of GitHub refs).
- **`projects = rebar`** scopes the remote to one project (the `url` hardcodes one
  repo with no `${name}` placeholder).
- **`replicatePermissions = false`** — `refs/meta/config` is **never** pushed, so
  ACLs and the embedded webhook token stay on-box. **ADR-0014 depends on this** —
  flipping it to `true` would mirror the token to GitHub. Do not change it.
- **Deploy-key identity** — Gerrit authenticates to GitHub as a per-repo ed25519
  **deploy key** (not a user PAT). The private key is materialized from SSM into the
  gerrit user's `~/.ssh` by `materialize-deploy-key.sh`, which is **fail-closed
  before Gerrit starts** (a missing key aborts boot rather than silently disabling
  replication).
- **Finite retries + crash-safe queue** (`replicationMaxRetries = 3`, `timeout = 60`,
  a persisted `eventsDirectory`).

**Kill-switch:** rename/remove the site `replication.config` and restart Gerrit (no
config → no remotes → no pushes); restore by moving it back. Deploy-key rotation is a
scripted lifecycle (generate new keypair → add as a new GitHub deploy key → overwrite
SSM → re-run `setup-replication.sh` → remove the old key).

---

## 10. Step 7 — Lock the GitHub repo

> Owns: the deploy-key-only ruleset. See ADR-0011; files under
> `infra/terraform-github/` + `infra/github/`, runbook
> `infra/runbooks/github-mirror-lock.md`.

This makes Gerrit the *sole* writer by locking the GitHub repo so **only the
replication deploy key** can update `main` and tags. **Always `plan`/dry-run before
applying.** Two equivalent paths:

```bash
# Path A — Terraform (provider integrations/github >= 6.8.0, native DeployKey bypass):
export GITHUB_TOKEN=…            # the Administration:write fine-grained PAT; NEVER committed
cd infra/terraform-github
terraform init
terraform plan                   # review the two rulesets (main + tags)
terraform apply                  # creates gerrit-mirror-lock-main + -tags

# Path B — gh api (no provider-version dependency): infra/github/apply-mirror-lock.sh
# Rollback: infra/github/rollback-mirror-lock.sh
```

How the lock works (ADR-0011 + the runbook):

- A repo ruleset's **`update` rule** means "restrict updates": only the ruleset's
  `bypass_actors` may update the matched refs. **A PR merge into `main` is itself an
  update to `main`** by a non-bypass actor, so the `update` rule rejects **both
  direct pushes and PR merges** — there is no merge path left. It is paired with
  `deletion` + `non_fast_forward` to block deletes/force-pushes, and a second ruleset
  locks all tags.
- The **single bypass actor is the replication deploy key** (`actor_type:
  "DeployKey"`, `bypass_mode: "always"`, no `actor_id`). The GitHub UI omits the
  DeployKey actor from the bypass picker, hence the `gh api` path; the native
  Terraform path works on provider `>= 6.8.0`.
- This **replaces** the prior human-PR protection model. The pre-existing ruleset is
  snapshotted at `infra/github/main-protection.snapshot.json` for **< 15-min
  rollback**: set the mirror-lock ruleset `enforcement=disabled` (or delete it),
  recreate the old protection from the snapshot, re-enable PRs/Issues/Actions.
- **Accepted PoC risk:** on a GitHub **free org**, only *repo-level* rulesets are
  available, so an active deploy-key-only ruleset rejects even an admin push, but a
  **trusted repo admin can deliberately edit/disable the rule**. This is documented,
  not hidden — a paid org's admin-proof org-level rulesets would close it.

After this step, `main` advances **only** via Gerrit replication; the team's
contribution workflow moves to Gerrit.

---

## 11. Step 8 — Verify + operate

**End-to-end verification.** Run from an operator box after a deploy (these make
*live* calls — do not run in CI by default):

```bash
infra/gerrit/smoke-check.sh        # S4a plumbing: bot token works for REST + git clone;
                                   # events-log backfill endpoint responds
infra/gerrit/reviewbot-e2e.sh      # the proven pipe end-to-end:
#   1. push a real throwaway change to refs/for/main
#   2. poll the LLM-Review label until rebar-review-bot casts a non-zero vote
#   3. assert the LLM-Review submit requirement is satisfied (the vote gates submit)
#   4. call /rerun and assert NO duplicate vote (dedup / single-flight holds)
```

**Operate:**

- **Manual recovery** — `POST https://<your-domain>/review/rerun?token=…` re-reviews a
  stuck change (still fail-closed).
- **Kill-switch** — disable replication (Step 6 kill-switch) and/or the review-bot
  container; with the GitHub lock in place and replication off, `main` simply stops
  advancing (safe).
- **CloudWatch alarms** (the host probe publishes metrics from journald/logs via the
  instance role — no AWS creds in the containers):
  - `rebar-gerrit-voter-errors` watches `rebar/host:voter_errors` (fail-closed vote
    failures; `infra/terraform/monitoring_s4b.tf`),
  - replication failures via `rebar/host:replication_errors` from the
    `replication_log` (non-fast-forward rejections, max-retry;
    `infra/terraform/monitoring_s5.tf`),
  - Gerrit/review-bot health + data-volume disk usage (`observability.sh`).
- **Freeze-and-restore posture** — the EBS data volume + EIP + state bucket are
  `prevent_destroy`; the data volume rides daily DLM snapshots, so the box is
  rebuildable from IaC + a snapshot without losing Gerrit state or the public address.

---

## 12. Adapting to your repo / domain

Everything is parameterized. To run this against **your** org/repo/domain, change the
placeholders — none of the committed artifacts carry a real secret:

| Placeholder | Where to change |
|---|---|
| `<your-domain>` (e.g. our `rebar.solutions.navateam.com`) | `DOMAIN`/`GERRIT_HOST`/`EMAIL` env vars on the scripts; Route53 record + `EMAIL` in Terraform variables; nginx server_name |
| `<aws-account-id>` | the state bucket name `rebar-tfstate-<aws-account-id>` and the region in `infra/terraform/` / `infra/bootstrap/` (Terraform variables) |
| `<your-org>/<your-repo>` | `replication.config` remote `url`; `register-deploy-key.sh REPO=…`; `infra/terraform-github/` variables; the GitHub PAT scope |
| `/rebar/prod/*` SSM paths | the `SSM_PREFIX` / per-script `SSM_*` env overrides if you want a different namespace |
| The seven secret **values** | populate the SSM SecureString params out-of-band — never in a committed file |
| Blocking-severity threshold, vote values | review-bot `config.py` env (`BLOCKING_SEVERITIES`, `LLM_REVIEW_MAX_VALUE`, `LLM_REVIEW_BLOCK_VALUE`) |

Read the ADRs (`docs/adr/0007`–`0014`) for the rationale behind each decision before
you adapt them — several are load-bearing (e.g. ADR-0010's
`replicatePermissions = false` is what keeps ADR-0014's webhook token off GitHub).
