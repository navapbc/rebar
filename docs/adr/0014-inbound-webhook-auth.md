# ADR 0014: Inbound webhook auth — network ACL primary, URL token secondary

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
  request body with a shared secret the receiver could verify. The only
  per-remote authentication knob it offers is whatever can be baked into the
  destination **URL** (it sends a configurable URL with optional headers).
- The plugin reads its remote config **only** from each project's
  `refs/meta/config` (`webhooks.config`) — not from the site dir, not from the
  working tree, not from an env var. So any secret used must live in
  `refs/meta/config`.

## Decision

Authenticate inbound webhooks with a **two-layer control**, network-first:

1. **PRIMARY — internal-only delivery (network ACL).** The webhook destination URL
   targets the receiver **directly over the private docker compose network**
   (`http://review-bot:8000/webhook?token=…`), so a webhook **never traverses the
   public internet or nginx at all** — it is a container-to-container POST
   (Gerrit container → review-bot container). Verified live: the receiver logs the
   delivery from the gerrit container's compose-network IP (`172.21.0.3 → POST
   /webhook … 202`). Defence in depth: the receiver's host port is loopback-bound
   (`127.0.0.1:8000`) and port 8000 is **not** open in the security group, so the
   receiver is unreachable from the public internet (verified: an external
   `curl http://<eip>:8000/` times out); the only public surface is nginx `/review/`,
   which S4b's receiver gates on the token. The network boundary is the real gate.

2. **SECONDARY — URL-embedded token (bounded exposure).** The bot's HTTP token is
   embedded in the webhook destination URL
   (`http://review-bot:8000/webhook?token=<token>`). Because the plugin offers no HMAC, this
   token is the only in-band credential available; it is a **secondary**,
   defence-in-depth control, not the primary gate. Its exposure is bounded:
   - it resides in `refs/meta/config` (the same ref the plugin reads), which is
     **Gerrit-access-controlled** (only admins can read/write that ref);
   - it is **NOT replicated to GitHub** — S5's replication sets
     `replicatePermissions=false`, so `refs/meta/config` (and thus the token) is
     never mirrored off-box (see Consequences / the dependency note below);
   - it is **rotatable** — `service-user.sh` regenerates the token, rotates it via
     `gerrit set-account --http-password`, overwrites the SSM param, and re-pushes
     `webhooks.config`, all idempotently.

3. **Receiver-side validation (S4b).** The S4b receiver additionally **validates
   the token** (compares the `?token=` query value against the SSM-sourced bot
   token) **and the source** of the request, so a request that reaches the
   endpoint without the right token — or from an unexpected source — is rejected.
   The token in the URL is thus checked, not merely decorative.

The bot's HTTP token and the webhook URL token are **one and the same secret**
(stored once at SSM `/rebar/prod/gerrit-bot-token`), so there is a single thing to
rotate.

## Consequences

- **No reliance on a non-existent feature.** We do not pretend the `webhooks`
  plugin can HMAC-sign; the design is honest about the plugin's capability and
  puts the real weight on the network boundary.
- **Single secret, single rotation path.** One token serves both the bot's Gerrit
  HTTP identity and the inbound webhook URL; rotating it (`service-user.sh`)
  updates SSM and `refs/meta/config` together.
- **DEPENDENCY ON S5.** The token's bounded exposure depends on S5 keeping
  **`replicatePermissions=false`** for the GitHub replication. If that ever flips
  to `true`, `refs/meta/config` — and the embedded token — would be mirrored to
  GitHub, breaking the "not replicated off-box" property. S5 must keep
  `replicatePermissions=false`; revisit this ADR if that changes.
- **Bounded blast radius.** If the token did leak, the worst case is spurious
  inbound webhook POSTs (which S4b's source + token validation still filter), and
  rotation is a single scripted step.
