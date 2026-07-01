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
Put the pair under the Gerrit secret prefix (mirrors the S5 deploy-key pattern), then add the
params to `infra/terraform/ssm.tf`:
```
aws ssm put-parameter --name "${SSM_PREFIX}/github_oauth_client_id"     --type SecureString --value "<client id>"
aws ssm put-parameter --name "${SSM_PREFIX}/github_oauth_client_secret" --type SecureString --value "<client secret>"
```
`fetch-secrets.sh` materializes these into the container `.env`; `compose-up.sh` writes the
`client-id` into `site/etc/gerrit.config` and the `client-secret` into `site/etc/secure.config`
(secrets never land in gerrit.config). Like `materialize-deploy-key.sh`, the boot must FAIL LOUDLY
if either param is absent once `auth.type = OAUTH`.

## Step 3 — Install the plugin
Install the pinned `gerrit-oauth-provider` jar (provenance in `infra/gerrit/plugins/README.md`)
into `$GERRIT_SITE/plugins/oauth.jar` (baked into the image or fetched by `install-plugins.sh`),
matching the Gerrit **3.14** line. Gerrit loads it on restart.

## Step 4 — Switch the config
In the live `site/etc/gerrit.config`:
```ini
[auth]
    type = OAUTH
    gitBasicAuthPolicy = HTTP
[plugin "gerrit-oauth-provider-github-oauth"]
    root-url = "https://github.com/"
    client-id = "<from SSM>"
```
`site/etc/secure.config`:
```ini
[plugin "gerrit-oauth-provider-github-oauth"]
    client-secret = "<from SSM>"
```
**Remove every `DEVELOPMENT_*` auth** (confirm no included file re-adds it). Keep
`gitBasicAuthPolicy = HTTP` so git-over-HTTP + bots keep working.

## Step 5 — Provision bot + service credentials BEFORE finalizing
Dev-mode required no credentials, so service accounts have none. Before restarting into OAUTH:
- Give **`rebar-review-bot`** an HTTP password (Settings → HTTP credentials, or REST
  `PUT /a/accounts/rebar-review-bot/password.http`) and/or an SSH key; store it where the
  receiver reads `GERRIT_BOT_TOKEN` (SSM). Human OAuth login does NOT apply to bots — they keep
  using HTTP passwords (Gerrit authenticates DB-only service users by HTTP password even under
  `auth.type = OAUTH`).
- The **Gerrit→GitHub replication** identity is outbound (the S5 deploy key in `replication.config`)
  and is unaffected by the inbound auth change.

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

## Rollback
If OAuth login is broken after the switch: restore `auth.type = DEVELOPMENT_BECOME_ANY_ACCOUNT`
(or `HTTP`) in `site/etc/gerrit.config` and restart. Because `gitBasicAuthPolicy = HTTP` is
unchanged, bot/git access is unaffected during the rollback. Do NOT proceed to WS7 (the Gerrit-only
GitHub cutover) until Step 6 verification passes — otherwise a broken login + a Gerrit-only `main`
would freeze the repo.
