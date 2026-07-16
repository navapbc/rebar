# ADR 0050 — Optional authentication for the rebar MCP server (OAuth 2.1 Resource-Server model)

- **Status:** Accepted
- **Epic:** `dissimilar-sorcerous-hartebeest` (3393-83ca-5501-4e6e)

## Context

`rebar-mcp` shipped as a **stdio-only, unauthenticated** server — correct for a local
agent, but a blocker for any remote/multi-user deployment. The MCP authorization spec
places auth on the **HTTP** transport (a stdio server MUST NOT authenticate), and models
the server as an **OAuth 2.1 Resource Server (RS)**: it validates tokens minted by an
external Authorization Server (AS) and rejects anything it cannot verify. We needed a
provider-generic way to authenticate the HTTP transport across a wide variety of
environments (Cognito, Entra ID, Auth0, Okta, Keycloak, WorkOS, Google, GitHub Actions
OIDC, oauth2-proxy, bespoke internal token services) without building or operating an AS.

## Decisions

1. **Resource-Server only — no AS, no DCR.** rebar verifies tokens; it does not issue
   them, run an Authorization Server, or do Dynamic Client Registration. This is the
   smallest correct surface and matches how the MCP SDK's `simple-auth` example is
   structured. AS/DCR, per-tool authorization, and in-process rate-limiting are
   explicitly deferred.

2. **Build our own verifiers on the official SDK `TokenVerifier`/`AuthSettings` seam**,
   rather than adopting the standalone `fastmcp` auth stack. The official `mcp` SDK is
   the dependency we already ship; `fastmcp`'s auth is semver-exempt and has its own CVE
   history. We pin `mcp>=1.28.1,<2` (at/above the DNS-rebinding fix GHSA-9h52-p55h-vw2f).

3. **A composite verifier is the single audience / fail-closed choke point.** The SDK's
   `BearerAuthBackend` never inspects a token's `.resource`, so there is no backstop
   behind an individual verifier. We therefore route every verifier through one
   `CompositeTokenVerifier` that **independently re-checks** `token.resource ==
   auth_resource_server_url` (RFC 8707) for every accepted token, treats a raising
   verifier as non-acceptance (never swallow-to-accept), and denies (401) when no
   verifier yields a valid, audience-matching token. This is motivated by the
   audience-confusion precedent (PVE-2026-93393): a buggy or hostile sub-verifier (even a
   custom one returning `resource=None`) cannot bypass audience validation.

4. **Five verifier modes on one seam** (`auth_strategies`, a closed vocabulary):
   `static` (SHA-256-digest bearer tokens), `jwt` (JWKS/OIDC via PyJWT), `introspection`
   (RFC 7662), `proxy` (trusted edge-proxy identity via a header-guard middleware +
   contextvar), and `custom` (an operator-supplied import string). A single flat config
   holds one instance per type; multiple issuers of one kind use `custom`.

5. **Transport hardening is fail-closed.** DNS-rebinding/Origin protection is ON with
   explicit loopback allowlists (disallowed Host → 421, Origin → 403); loopback bind is
   the default; a non-loopback bind requires both allowlists **and** an explicit
   TLS-at-edge acknowledgement; and an unauthenticated HTTP boot is refused unless
   explicitly acknowledged. This cites GHSA-9h52-p55h-vw2f (DNS-rebinding), the
   oauth2-proxy forwarded-header class (CVE-2026-40575 / CVE-2025-64484), and the
   confused-deputy class (CVE-2026-27124).

## Consequences

- Auth is **opt-in and additive**: stdio stays the default and is byte-identical when the
  new keys are unset. No change to the existing 48 tools.
- Secrets are never in config (only env-var **names**) and never logged; static tokens
  hold only digests; JWT/introspection/proxy all carry `resource` so the composite gates
  them uniformly.
- Operators front rebar with a TLS-terminating proxy and set `auth_resource_server_url`
  to the external `https://` URL; the served PRM and `WWW-Authenticate` derive from it.
- Deferred items (AS/DCR, per-tool authz, in-process rate-limiting, positive-introspection
  cache, AWS recipes) are follow-on work, not regressions.

See [docs/mcp-auth.md](../mcp-auth.md) for the operator guide.
