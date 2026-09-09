# ADR 0014: Inbound webhook auth — network ACL primary, token public-only

- **Status:** Accepted
- **Context:** Epic *stand up AWS-hosted Gerrit + rebar review-bot (PoC)* (`d251`),
  story *S4a — review-bot identity + event plumbing* (this story owns the bot
  service account, its HTTP token, the `events-log` plugin, and the `webhooks`
  plugin remote config that POSTs Gerrit events to the receiver).

## Context

Gerrit's bundled **`webhooks`** plugin delivers events to the review-bot receiver
(`POST` to `/review/`, the S2 endpoint) when a patchset is created. We must
authenticate those inbound POSTs so that only Gerrit — not an arbitrary internet
client — can trigger a bot review.

The constraint that drives the design is a property of the plugin:

- The `webhooks` plugin has **NO HMAC** request signing. It cannot sign the
  request body with a shared secret the receiver could verify.
- The Gerrit 3.14 `webhooks` plugin deployed here does **not** read arbitrary
  `header =` keys from `webhooks.config`; it sends only its built-in event
  headers such as `X-Origin-Url`.
- The plugin reads its remote config **only** from each project's
  `refs/meta/config` (`webhooks.config`) — not from the site dir, not from the
  working tree, not from an env var.

## Decision

Authenticate inbound webhooks with a **two-layer control**, network-first:

1. **PRIMARY — internal-only delivery (network ACL).** The webhook destination URL
   targets the receiver **directly over the private docker compose network**
   (`http://review-bot:8000/webhook`), so a webhook **never traverses the
   public internet or nginx at all** — it is a container-to-container POST
   (Gerrit container → review-bot container). Verified live: the receiver logs the
   delivery from the Gerrit container's compose-network IP (`172.21.0.3 → POST
   /webhook … 202`). Defence in depth: the receiver's host port is loopback-bound
   (`127.0.0.1:8000`) and port 8000 is **not** open in the security group, so the
   receiver is unreachable from the public internet (verified: an external
   `curl http://<eip>:8000/` times out); the only public surface is nginx `/review/`,
   which S4b's receiver gates on the token. The network boundary is the real gate.

2. **SECONDARY — Gerrit-origin assertion for the internal path.** For the
   container-to-container webhook, the receiver accepts tokenless requests only when
   Gerrit's built-in `X-Origin-Url` header matches the configured canonical Gerrit URL,
   the TCP peer is the address currently resolved for the configured internal Gerrit
   service host, and the request did not pass through public nginx (`X-Forwarded-For`
   absent). This uses a header the plugin actually sends instead of a `header =` config
   key it ignores, without trusting that client-controlled header by itself.

3. **Public receiver token.** Public callers to `/review/*` (notably `/rerun`)
   must still supply the SSM-sourced bot token in the `X-Rebar-Token` header. The
   legacy `?token=` query form remains accepted only for backward compatibility and
   is redacted from access logs; do not configure Gerrit to put secrets in URLs.

## Consequences

- **No reliance on a non-existent feature.** We do not pretend the `webhooks`
  plugin can HMAC-sign; the design is honest about the plugin's capability and
  puts the real weight on the network boundary.
- **No secret in `refs/meta/config`.** Gerrit's webhook URL stays tokenless, so
  `refs/meta/config` and Gerrit/webhooks error logs no longer contain the bot token.
- **Single public token rotation path.** The bot's Gerrit HTTP identity and public
  receiver token remain one SSM value; rotating it (`service-user.sh`) updates SSM
  and the review-bot environment, but webhook acceptance no longer depends on
  Gerrit reloading a secret it cannot actually send as a header.
- **Bounded blast radius.** If the token did leak, the worst case is spurious
  public `/review/*` calls (which S4b's token validation still filters), and
  rotation is a single scripted step.
