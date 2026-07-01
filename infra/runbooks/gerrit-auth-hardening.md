# Runbook — Gerrit auth hardening (replace `DEVELOPMENT_BECOME_ANY_ACCOUNT` with GitHub OAuth)

**Ticket:** b744 / WS8. **Blocks:** WS7 (the Gerrit-only cutover must NOT precede real auth).

The PoC Gerrit boots with `auth.type = DEVELOPMENT_BECOME_ANY_ACCOUNT` (`infra/compose/gerrit.config`)
— anyone can impersonate any account. This runbook switches it to **GitHub OAuth** via the
[`gerrit-oauth-provider`](https://github.com/davido/gerrit-oauth-provider) plugin (GitHub backend),
the confirmed strategy (native Gerrit OAuth, matches the team's GitHub identity; access curated via
a short account roster + Gerrit ACLs — no reverse-proxy org gate). Precedent: GerritHub.

> **Why not just flip `type = OAUTH` in the committed config?** Gerrit refuses to boot with
> `auth.type = OAUTH` unless the provider plugin is installed AND its client-id/secret are present.
> So the flip, the plugin jar, and the SSM-sourced secrets must land together (this runbook), or a
> fresh boot fails. That's why the committed `gerrit.config` still shows the PoC value with a
> pointer here.

## Prerequisites
- **GitHub org admin** on `navapbc` (to create the OAuth App — a web-UI action).
- **AWS access** to the Gerrit host (the box is reached via AWS SSM; `infra/scripts/*` +
  `fetch-secrets.sh` pull from SSM `${SSM_PREFIX}/*`).
- The Gerrit canonical URL is `https://rebar.solutions.navateam.com` (TLS already terminated).

## Step 1 — Create the GitHub OAuth App (org admin, web UI)
1. GitHub → `navapbc` org → Settings → Developer settings → **OAuth Apps** → *New OAuth App*.
2. **Homepage URL:** `https://rebar.solutions.navateam.com`
3. **Authorization callback URL (exact):** `https://rebar.solutions.navateam.com/oauth`
   (the `gerrit-oauth-provider` GitHub backend requires the `/oauth` suffix on the canonical URL.)
4. Generate a client secret. Record the **Client ID** (non-secret) + **Client Secret** (secret).
   > Note: the GitHub backend does not restrict login to org members — anyone with a GitHub account
   > can authenticate. Authorization is curated via the Gerrit account roster + project ACLs. If you
   > later need hard org-gating, front Gerrit with `oauth2-proxy --github-org=navapbc` (`auth.type =
   > HTTP`) — a separate change.

## Step 2 — Store the credentials in SSM
Put the pair under the Gerrit secret prefix `/rebar/prod` (mirrors the S5 deploy-key pattern). The
two param slots are declared in `infra/terraform/ssm.tf` (and mirrored in the `user_data.sh` PARAMS
map), so terraform owns their existence; an operator populates the values out-of-band:
```
aws ssm put-parameter --region us-east-1 --type SecureString --overwrite \
  --name /rebar/prod/github-oauth-client-id     --value "<client id>"
aws ssm put-parameter --region us-east-1 --type SecureString --overwrite \
  --name /rebar/prod/github-oauth-client-secret --value "<client secret>"
```
> **Hyphens, not underscores** — the leaf names are `github-oauth-client-{id,secret}`, matching the
> repo convention (`github-replication-deploy-key`, etc.).

**Materialization is automated** (no manual config edit on the box):
- `fetch-secrets.sh` fetches both params into the container `.env` as
  `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` (fail-fast on empty/`None`/`CHANGEME`).
- `compose-up.sh` then seeds `gerrit.config`, substitutes the non-secret client-id into it (the
  committed config carries the `__GITHUB_OAUTH_CLIENT_ID__` placeholder), and writes the secret
  client-secret into `site/etc/secure.config` (mode 0600). **Secrets never land in gerrit.config.**
  When `gerrit.config` selects `auth.type = OAUTH`, `compose-up.sh` FAILS LOUD if `plugins/oauth.jar`
  is absent or either credential is empty — a half-configured OAUTH Gerrit never boots.

> **Ordering to avoid a mid-apply error:** prefer to let terraform `apply` **create** the two slots
> as `CHANGEME` first, *then* `aws ssm put-parameter --overwrite` the real values (as shown above).
> `ignore_changes = [value]` means a later `apply` never reverts your value to `CHANGEME`. If you
> instead `put-parameter` *before* the first `apply`, terraform will fail with `ParameterAlreadyExists`
> — recover by importing:
> `terraform import 'aws_ssm_parameter.rebar_secrets["/rebar/prod/github-oauth-client-id"]' /rebar/prod/github-oauth-client-id`.

## Step 3 — Install the plugin
Run `infra/gerrit/install-plugins.sh` (operator step — `GERRIT_SITE=/var/gerrit/site bash
infra/gerrit/install-plugins.sh`). It fetches the pinned `gerrit-oauth-provider` jar (URL + sha256
in `infra/gerrit/plugins/README.md`) into `$GERRIT_SITE/plugins/oauth.jar`, verifying the sha256
(fail-on-mismatch), matching the Gerrit **3.14** line. It is idempotent (skips if the jar is already
present + verified). Gerrit loads the plugin on restart. **Run this BEFORE `compose-up.sh`** — the
plugin download is deliberately NOT wired into `compose-up` so a whole-stack boot is not coupled to
GerritForge CI reachability; `compose-up` only *verifies* `oauth.jar` is present (fail-loud) before
booting into OAUTH.

## Step 4 — Switch the config
The committed `infra/compose/gerrit.config` already selects OAUTH (this is the WS8 flip):
```ini
[auth]
    type = OAUTH
    gitBasicAuthPolicy = HTTP
[plugin "gerrit-oauth-provider-github-oauth"]
    root-url = https://github.com/
    client-id = __GITHUB_OAUTH_CLIENT_ID__   # substituted from SSM by compose-up.sh
```
`compose-up.sh` writes `site/etc/secure.config` (0600) with the client-secret:
```ini
[plugin "gerrit-oauth-provider-github-oauth"]
    client-secret = <from SSM>
```
**Every `DEVELOPMENT_*` auth is removed** from the committed config; confirm no included file
re-adds it. `gitBasicAuthPolicy = HTTP` is kept so git-over-HTTP + bots keep working. On the live
box the switch happens on the next `compose-up.sh` (re-seeds gerrit.config from the repo).

## Step 5 — Provision bot + service credentials BEFORE finalizing
Dev-mode required no credentials, so service accounts have none. Before restarting into OAUTH:
- Give **`rebar-review-bot`** an HTTP password (Settings → HTTP credentials, or REST
  `PUT /a/accounts/rebar-review-bot/password.http`) and/or an SSH key; store it where the
  receiver reads `GERRIT_BOT_TOKEN` (SSM). Human OAuth login does NOT apply to bots — they keep
  using HTTP passwords (Gerrit authenticates DB-only service users by HTTP password even under
  `auth.type = OAUTH`).
- The **Gerrit→GitHub replication** identity is outbound (the S5 deploy key in `replication.config`)
  and is unaffected by the inbound auth change.

> **`bootstrap-gerrit-admin.sh` no longer works under OAUTH.** That script creates the admin account
> via the dev-login endpoint (`GET /login/...?account_id=1000000`), which only exists under
> `DEVELOPMENT_BECOME_ANY_ACCOUNT`. It is a **one-time bootstrap** — run it (to create the admin +
> the `rebar-review-bot` service user and their SSH keys / HTTP passwords) while the box is STILL in
> dev-mode, i.e. **before** this cutover. After the flip, the admin logs in via GitHub OAuth and new
> service users are created with `gerrit create-account` over SSH by the admin. Do NOT expect
> `bootstrap-gerrit-admin.sh` to succeed post-flip; provision all service credentials in Step 5
> first.

## Step 6 — Restart + verify
1. `docker compose restart gerrit` (or the S2 compose-up path). Watch logs for a clean boot (no
   "cannot start: OAUTH provider not configured").
2. `curl -s https://rebar.solutions.navateam.com/config/server/info | sed "s/)]}'//" | jq .auth.auth_type`
   → must be `OAUTH` (NOT `DEVELOPMENT_BECOME_ANY_ACCOUNT`).
3. In a browser, "Sign in" → redirected to GitHub → back to Gerrit as your GitHub identity. Confirm
   the *Become* link is gone.
4. Bot check: with the bot HTTP password, `curl -u rebar-review-bot:<pw> https://rebar.solutions.navateam.com/a/accounts/self`
   returns the bot account.

## Account migration
Dev-mode accounts were self-asserted usernames; OAuth identities are keyed by the GitHub
`external-id`. For the handful of real users, either recreate accounts on first OAuth login or
reconcile `refs/meta/external-ids` so the GitHub identity maps to the existing account. Do this for
real users before flipping (Wikimedia's T147864 shows account migration is the fiddly part at
scale; at our size it is a short manual step).

> **Make the migration idempotent.** Whatever reconciles `refs/meta/external-ids` must be safe to
> re-run after a partial failure: **skip any account that already carries a `github:` external-id**
> (add-if-absent, never double-apply). Then a mid-run failure (say after 3 of 10 accounts) is fixed
> by simply re-running — no account is left half-migrated or duplicated.

## Rollback
If OAuth login is broken after the switch: restore `auth.type = DEVELOPMENT_BECOME_ANY_ACCOUNT`
(or `HTTP`) in `site/etc/gerrit.config` and restart. Because `gitBasicAuthPolicy = HTTP` is
unchanged, bot/git access is unaffected during the rollback. Do NOT proceed to WS7 (the Gerrit-only
GitHub cutover) until Step 6 verification passes — otherwise a broken login + a Gerrit-only `main`
would freeze the repo.

## Break-glass — GitHub OAuth outage
Under `auth.type = OAUTH`, **all human logins depend on GitHub being reachable.** If GitHub OAuth is
down (GitHub outage, OAuth App disabled/secret rotated, or the plugin failing), humans cannot sign
in. Mitigations, in order of preference:
- **Bots/git keep working regardless** — service users authenticate by HTTP password
  (`gitBasicAuthPolicy = HTTP`), which does not touch GitHub. So the review-bot, replication, and
  git-over-HTTP survive a GitHub-auth outage; only the human web UI login is affected.
- **Pre-provisioned admin HTTP password.** Keep the admin's HTTP password (set in Step 5, stored in
  SSM `/rebar/prod/gerrit-admin-password`) usable so an operator can still drive Gerrit via REST/SSH
  during an outage without the web login.
- **Last resort — temporary dev-mode.** On the isolated box (admin via SSM only, TLS-fronted),
  temporarily restore `auth.type = DEVELOPMENT_BECOME_ANY_ACCOUNT` and restart to regain UI access,
  then flip back once GitHub recovers. Treat this as an incident: it re-opens impersonation, so do it
  only under SSM-restricted access and revert promptly.
