# SSO auth host — operations runbook (`*.solutions.navateam.com`)

Google-OAuth SSO gate for `*.solutions.navateam.com`. **Option B**: a central auth
host does the Google code flow once and mints a domain-wide, HMAC-signed session
cookie; each protected CloudFront distribution runs a viewer-request Lambda@Edge
(the `sso-gate` module) that verifies the cookie and otherwise bounces the visitor
to the auth host. One sign-in covers every subdomain (true SSO).

> **Ownership.** This stack was re-homed from the decommissioned snap project into
> rebar's terraform (ADR 0048). It lives in **`infra/terraform/`** (state
> `rebar-tfstate-896586841071`). Physical resource names are still `snap-demo-*`
> (preserved to avoid a destroy/recreate — see `local.legacy_name_prefix` in
> `auth_sso.tf`); a rename is a separate future migration. rebar owns it; the tags
> read `Project=rebar`.

## Architecture

```
  ┌──────────┐  no/!valid cookie  ┌────────────────────────────────────────────┐
  │ Viewer   │ ─────────────────► │ auth.solutions.navateam.com (auth host)      │
  │ browser  │   302 /authorize   │ CloudFront → API Gateway (HTTP API) →        │
  └────┬─────┘                    │ regional Lambda (us-east-1):                 │
       │  GET a rebar subdomain   │  /authorize → 302 Google consent (signed     │
       ▼                          │               state carries return_to)       │
  ┌──────────────────────────┐    │  /_callback → code exchange, enforce         │
  │ a protected CloudFront    │    │               hd+email=navapbc.com, set      │
  │ distribution (future):    │    │               __Secure-sso cookie, 302 back  │
  │ viewer-request Lambda@Edge│    │  /_logout   → clear cookie                   │
  │  = sso-gate module        │    └──────────────────────────────────────────────┘
  │ valid cookie → real origin│            ▲ Google OAuth (Internal consent)
  └──────────────────────────┘            ▼ redirect_uri .../\_callback
                                     accounts.google.com
```

- **Why API Gateway, not a Lambda Function URL:** this account blocks invocation of
  Lambda Function URLs by anything but in-account IAM principals (an org guardrail).
  API Gateway invokes via standard `lambda:InvokeFunction`; the HTTP API uses payload
  format 2.0 (same event shape a Function URL would deliver).
- **Origin secret:** the `execute-api` endpoint is public, so CloudFront injects a
  secret `X-Origin-Auth` custom origin header (which it overrides, so viewers can't
  spoof it) and the Lambda 403s any request lacking it — keeping the raw API Gateway
  URL un-usable by outsiders.
- **One** Google OAuth client (Internal consent, single redirect URI
  `https://auth.solutions.navateam.com/_callback`) covers every subdomain (Google
  forbids wildcard redirect URIs, so the central host is what makes one client serve
  all of `*.solutions.navateam.com`).
- The session cookie is `__Secure-sso`, `Domain=.solutions.navateam.com`,
  `HttpOnly; Secure; SameSite=Lax`, 12h lifetime — scoped to the parent domain so one
  sign-in is honored by every subdomain gate.
- `return_to` is restricted to `https://*.solutions.navateam.com` at both `/authorize`
  and `/_callback` (signed into `state`) — no open redirect. Both `hd` (Workspace org)
  and a verified `@navapbc.com` email are required; `aud`/`iss` are checked.

## Protecting a new rebar subdomain

No Google change is needed (the client already covers the whole domain). rebar adopted
the reusable **`infra/terraform/modules/sso-gate`** module (edge-gate source under
`infra/terraform/auth/edge-gate/`, sharing `auth/lib/cookie.js` with the auth host so
sign/verify can never drift). To protect a distribution, add one module block + one
`lambda_function_association` on its `default_cache_behavior` — see
`infra/terraform/modules/sso-gate/README.md` for the copy-paste shape — and
`terraform apply` from `infra/terraform`. The new gate reuses the same cookie, so a
user already signed in elsewhere reaches it without re-auth. (rebar currently runs no
gate instance — the auth host + module are staged for the first rebar subdomain.)

## Secrets

| Secret | Where | Managed by | Consumed by |
|---|---|---|---|
| Google `client_secret` | SSM SecureString `/auth-solutions/GOOGLE_CLIENT_SECRET` | provisioned out-of-band (Nava IT → CLI) | auth-host Lambda, **read at runtime** (never baked) |
| Cookie-signing key (HMAC) | SSM SecureString `/auth-solutions/COOKIE_SIGNING_SECRET` | Terraform owns existence+type (`aws_ssm_parameter.cookie_signing_secret`, `ignore_changes=[value]`); **value set out-of-band** | auth-host Lambda (runtime) **and** every edge-gate bundle (**baked at deploy** — Lambda@Edge can't read SSM) |
| Origin secret (`X-Origin-Auth`) | Terraform state only (`random_password.auth_origin_secret`) | Terraform | injected by CloudFront as a custom origin header; verified by the auth-host Lambda (env var) |

The Google **client_id** is public (`auth_sso.tf` locals) — not a secret.

## Rotation

### Cookie-signing key (the SSO session key) — also the revoke lever

Rotating this key **invalidates every live session**. Unlike snap (which regenerated
it via `random_password`), rebar manages the SSM value with `ignore_changes=[value]`,
so you rotate it **out-of-band** and then force consumers to pick it up:

1. Write a fresh key and force the auth host to redeploy so warm containers drop the
   old cached key:
   ```
   aws ssm put-parameter --name /auth-solutions/COOKIE_SIGNING_SECRET \
     --type SecureString --value "$(openssl rand -base64 36 | tr -d '/+=' | head -c 48)" \
     --overwrite --region us-east-1
   cd infra/terraform && terraform apply -replace=aws_lambda_function.auth_host
   ```
   The auth host reads the key from SSM at runtime and caches it for the life of each
   warm container (no TTL — see `getSecrets()` in `auth/auth-host/index.js`), so the
   `-replace` forces new containers that read the new key on cold start.
2. **If any edge gate is deployed**, its bundle *bakes* the key (`templatefile` reads
   `data.aws_ssm_parameter.cookie_signing_secret`), so `terraform apply` re-bakes and
   republishes the gate. Allow a few minutes for Lambda@Edge propagation.
3. Verify: an existing session cookie now fails → user is bounced to Google; a fresh
   sign-in works end-to-end.

### Google `client_secret`

Rotate in the Google Cloud console (new secret on the same OAuth client), then
overwrite the SSM SecureString — **no Terraform value change** (Terraform references
only the ARN):
```
aws ssm put-parameter --name /auth-solutions/GOOGLE_CLIENT_SECRET \
  --type SecureString --value 'NEW_SECRET' --overwrite --region us-east-1
```
Both old and new secrets are valid during the overlap, so sign-in keeps working; to
force immediate pickup, `cd infra/terraform && terraform apply -replace=aws_lambda_function.auth_host`.
Then delete the old secret in the Google console.

## Incident response — revoke all access now

Sessions are stateless signed cookies (no per-user store). To force every user to
re-authenticate immediately, rotate the cookie-signing key (above). To cut off Google
entirely, additionally disable/delete the OAuth client in the Google console — new
sign-ins then fail at `/authorize`.

## Deploy & verify

Deploy from `infra/terraform` (`terraform apply`; provider is us-east-1, a Lambda@Edge
requirement). CloudFront + Lambda@Edge propagate asynchronously (minutes).

**Prerequisite:** the Google OAuth client must keep
`https://auth.solutions.navateam.com/_callback` as an Authorized redirect URI, or
`/_callback` fails with `redirect_uri_mismatch`.

**Headless smoke test** (no login needed) — the auth host builds the correct Google
consent redirect and Google accepts the `redirect_uri`:
```
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  "https://auth.solutions.navateam.com/authorize?return_to=https://rebar.solutions.navateam.com"
# expect: 302  https://accounts.google.com/o/oauth2/v2/auth?...redirect_uri=...%2F_callback...
curl -sS -o /dev/null -w '%{http_code}\n' \
  "https://q8mv4bhkmh.execute-api.us-east-1.amazonaws.com/authorize?return_to=https://rebar.solutions.navateam.com"
# expect: 403  (direct execute-api hit lacks the CloudFront origin secret)
```

**Manual checklist** (needs a real `@navapbc.com` human): unauth → Google (no content);
non-navapbc → 403; navapbc → allowed; SSO across subdomains with no re-login; expiry
forces re-auth; `/_logout` clears the cookie.
