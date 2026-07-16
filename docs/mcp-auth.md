# Authenticating the rebar MCP server (optional OAuth 2.1 Resource-Server model)

The rebar MCP server (`rebar-mcp`) is **stdio-only and unauthenticated by default** —
nothing in this document changes that. When you expose it over **HTTP** (for remote
clients), rebar can additionally act as an **OAuth 2.1 Resource Server (RS)**: it
validates a bearer token (or a trusted-proxy identity) on every request and rejects
anything it cannot verify. This is opt-in and additive.

> **Security model in one line:** rebar is *only* a Resource Server. It does **not**
> issue tokens, run an Authorization Server, or do Dynamic Client Registration (DCR).
> It verifies tokens minted elsewhere and enforces that every accepted token names
> **this** server as its audience.

## 1. Enabling the HTTP transport

The transport is selected by config; stdio remains the default and is byte-identical
to today when the new keys are unset.

```toml
[tool.rebar.mcp]
transport = "http"        # default "stdio"
http_host = "127.0.0.1"   # loopback by default
http_port = 8000
http_path = "/mcp"
```

Every key has a `REBAR_MCP_*` env-var form (e.g. `REBAR_MCP_TRANSPORT=http`,
`REBAR_MCP_HTTP_PORT=8000`).

**Transport hardening (always on for HTTP):**
- **DNS-rebinding / Origin protection is ON** with explicit loopback allowlists by
  default (`127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>` and the matching
  `http://` origins). A disallowed `Host` → **421**, a disallowed `Origin` → **403**.
- **Loopback bind by default.** Binding a **non-loopback** host is fail-closed: it
  requires **both** `http_allowed_hosts` **and** `http_allowed_origins` to be non-empty
  **and** an explicit TLS-at-edge acknowledgement `http_tls_at_edge = true`
  (`REBAR_MCP_HTTP_TLS_AT_EDGE=1`) — bearer tokens and proxy secrets must never cross a
  network in cleartext.
- **Unauthenticated HTTP is refused** unless you acknowledge it: starting `transport =
  "http"` with **auth disabled** refuses to boot unless
  `allow_unauthenticated_http = true` (`REBAR_MCP_ALLOW_UNAUTHENTICATED_HTTP=1`).

TLS is terminated at an edge proxy (see §5); rebar binds HTTP behind it and never
advertises or serves bearer auth in cleartext.

## 2. Enabling authentication

```toml
[tool.rebar.mcp]
transport = "http"
auth_enabled = true
auth_strategies = "static"                       # closed vocabulary — see below
auth_issuer_url = "https://issuer.example.com"   # advertised in the PRM document
auth_resource_server_url = "https://mcp.example.com"   # THE audience for the whole server
auth_required_scopes = ""                        # comma-separated; empty = no scope requirement
```

`auth_strategies` is a **closed set** — an unknown entry is a hard startup error, and
`auth_enabled = true` producing an empty composite is a hard startup error:

| strategy       | verifies                                             |
|----------------|------------------------------------------------------|
| `static`       | static bearer tokens from a secrets file (PATs/CI)   |
| `jwt`          | OIDC JWTs against a JWKS (Cognito, Entra, Auth0, …)   |
| `introspection`| opaque tokens via an RFC 7662 `/introspect` endpoint |
| `proxy`        | an identity asserted by a trusted edge proxy         |
| `custom`       | an operator-supplied `TokenVerifier` (import string) |

You can combine strategies (e.g. `auth_strategies = "static,jwt"` to accept PATs from
CI **and** OIDC JWTs from humans — the GitHub model). The **flat schema holds one
instance per strategy type**; for multiple issuers of the same kind, use the `custom`
strategy.

### The composite is the single audience / fail-closed choke point

Every configured verifier is placed in one `CompositeTokenVerifier`. Regardless of what
a sub-verifier returns, the composite **independently re-checks** that the token's
`resource` equals `auth_resource_server_url` (RFC 8707) before accepting it — the SDK's
own bearer backend never inspects `.resource`, so this is the only place audience is
enforced. A verifier that raises is treated as **non-acceptance** (never
swallow-to-accept). If no verifier yields a valid, audience-matching token the request
is denied **401**; a principal lacking a `required_scope` is **403**. Tokens and secrets
are never logged.

## 3. The five verifier modes

### `static` — static bearer tokens (dev/internal-grade PATs)
```toml
auth_strategies = "static"
auth_static_tokens_file = "/etc/rebar/tokens.json"
```
The secrets file holds per-token records; rebar stores **only SHA-256 digests** and
compares in constant time. Each record has a `name`, `client_id`, `scopes`, and
**exactly one** of `token_sha256` (a hex digest) or `token_env` (the NAME of an env var
holding the token). A plaintext `token` literal is rejected.
```json
{"tokens": [
  {"name": "ci", "client_id": "ci-bot", "scopes": ["rebar.use"], "token_env": "REBAR_CI_TOKEN"}
]}
```
Static tokens are **non-expiring** and dev/internal-grade — generate them with **≥128
bits of entropy** and prefer `token_env` over a committed digest. Generate one and its
digest:
```sh
tok=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')   # 256-bit
printf '%s' "$tok" | python3 -c 'import sys,hashlib; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
```

### `jwt` — OIDC / JWKS JWT validation
```toml
auth_strategies = "jwt"
auth_jwt_jwks_uri = "https://issuer.example.com/.well-known/jwks.json"
auth_jwt_issuer = "https://issuer.example.com"
auth_jwt_algorithms = "RS256,ES256"      # asymmetric only
auth_jwt_leeway = 60
auth_jwt_jwks_refetch_cooldown = 30
auth_jwt_jwks_timeout = 10
auth_jwt_expected_typ = ""               # SHOULD be "at+JWT" for RFC 9068 IdPs
auth_jwt_allow_private_jwks_host = false
```
Validates the signature against the JWKS (pinned algorithms — `alg:none` and any
**symmetric** algorithm on a JWKS source are refused, blocking RS256↔HS256 confusion),
plus `aud == auth_resource_server_url`, `iss`, and `exp`/`nbf` with leeway. When
`auth_jwt_expected_typ` is set the JWT header `typ` must match it (RFC 8725 §3.11) so an
ID token cannot be replayed as an access token — **operators of RFC 9068-compliant IdPs
SHOULD set `auth_jwt_expected_typ = "at+JWT"`**. The JWKS fetch is **HTTPS-only**,
rejects private/link-local hosts by default, and caps unknown-`kid` refetches to one per
cooldown window.

### `introspection` — RFC 7662 opaque-token introspection
```toml
auth_strategies = "introspection"
auth_introspection_endpoint = "https://issuer.example.com/introspect"
auth_introspection_client_id = "rebar-rs"
auth_introspection_client_secret_env = "REBAR_INTROSPECTION_SECRET"   # NAME of an env var
auth_introspection_allow_private_host = false
auth_introspection_allow_missing_aud = false
```
POSTs the token with `client_secret_basic` (the secret comes from the **named env var**,
which must be non-empty at startup), accepts only `active:true`, and re-introspects on
**every** request (instant revocation; no caching in v1). Audience follows RFC 7662 §2.2
(string or array). A response **omitting `aud` is rejected by default**; set
`auth_introspection_allow_missing_aud = true` only if your AS scopes tokens to this
resource by other means (documented risk). A non-200, a timeout, or a malformed response
is fail-closed (deny).

### `proxy` — trusted edge-proxy identity passthrough
```toml
auth_strategies = "proxy"
auth_proxy_secret_env = "REBAR_PROXY_SECRET"     # NAME of an env var holding the shared secret
auth_proxy_secret_header = "x-proxy-auth"
auth_proxy_identity_header = "x-forwarded-user"
auth_proxy_scopes = ""                           # scopes granted to proxy principals; empty by default
```
For deployments that terminate auth at an edge proxy (oauth2-proxy, API Gateway, ALB).
A header-guard middleware **strips the entire `X-Forwarded-*` family** (and the
identity/secret headers, normalizing case + underscore/dash to defeat smuggling) on
every request; it trusts the identity header **only** when the shared secret validates
(constant-time). The proxy identity flows through the **same composite** and carries
`resource = auth_resource_server_url`, so it is subject to the audience check like any
token. The secret proves "from the proxy," not identity authenticity — so **your
fronting proxy MUST overwrite/strip all client-supplied forwarded/identity headers
before adding its own**. `auth_proxy_scopes` defaults to empty, so a proxy principal
holds no scopes and is 403'd by any non-empty `auth_required_scopes` until you grant them.

### `custom` — a pluggable operator verifier
```toml
auth_strategies = "custom"
auth_custom_import = "my_pkg.rebar_auth:make_verifier"   # module:factory
```
Loads a `module:factory` import string (like uvicorn/Starlette app factories) resolving
to an object implementing the SDK's `mcp.server.auth.provider.TokenVerifier` protocol.
**`auth_custom_import` is a trusted operator config value — importing it executes code at
startup.** It is read only from config (never a request); the current working directory
is **not** added to `sys.path` (no cwd hijack); an unresolvable import or an object
lacking `verify_token` is a fail-closed startup error. The composite's audience re-check
still applies, so a buggy/hostile custom verifier cannot mint a wrong-audience token.

## 4. The interactive-client connection paths (RS-only is not "can't connect")

Because rebar is RS-only (no AS/DCR), a client obtains its token elsewhere and presents
it. Three paths:
1. **Static bearer** — Claude Code `--header "Authorization: Bearer <token>"` / Claude.ai
   `static_headers`; pairs with the `static` verifier.
2. **Automatic OAuth** when the client and your org IdP support DCR + client-id metadata
   discovery (CIMD); pairs with `jwt`/`introspection`.
3. **Pre-registered client** — `--client-id`/`--client-secret` against an IdP that does
   not offer DCR, discovered via the served PRM.

## 5. Behind a proxy (TLS at the edge)

TLS terminates at an edge proxy (nginx/ALB); rebar binds **loopback** behind it. The
external host must be in `http_allowed_hosts` and the external origin in
`http_allowed_origins`, and **`auth_resource_server_url` MUST be the external `https://`
URL** — it drives the served RFC 9728 PRM `resource` and the
`WWW-Authenticate: resource_metadata` URL, not the request scheme.

```nginx
# nginx terminating TLS in front of a loopback-bound rebar-mcp
location /mcp {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host mcp.example.com;                 # external host (in http_allowed_hosts)
    proxy_set_header X-Forwarded-Proto https;
    # For the `proxy` strategy: strip client-supplied identity, then assert your own:
    proxy_set_header X-Forwarded-User $authenticated_user;
    proxy_set_header X-Proxy-Auth "$rebar_proxy_secret";   # matches auth_proxy_secret_env
}
```
```toml
[tool.rebar.mcp]
transport = "http"
http_host = "127.0.0.1"
http_allowed_hosts = "mcp.example.com:443"
http_allowed_origins = "https://mcp.example.com"
http_tls_at_edge = true
auth_enabled = true
auth_resource_server_url = "https://mcp.example.com"
```

## 6. Server-level and per-request controls (unchanged)

Auth is orthogonal to the existing gates: `REBAR_MCP_READONLY` (hide write tools),
`REBAR_MCP_ALLOW_JIRA_SYNC` (live Jira reconcile), `REBAR_MCP_ALLOW_LLM` (billable LLM
tools). These are server-level capability gates; auth decides *who may call the server*
at all.

## 7. Explicitly out of scope (v1)

Deliberately deferred: running an Authorization Server or Dynamic Client Registration;
per-tool / per-resource authorization; in-process rate-limiting / brute-force protection
(front with your proxy); a positive-introspection cache; and AWS-specific deployment
recipes. See [ADR 0050](adr/0050-mcp-optional-auth-resource-server.md) for the decision
record and rationale.
