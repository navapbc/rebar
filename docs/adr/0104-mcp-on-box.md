# ADR 0104 — MCP on the AWS box: a self-contained `rebar-mcp` HTTP server for copilot/codex/claude

- **Status:** Accepted
- **Ticket:** `deft-evolutive-mosasaur` (a45f-bd45-513a-49cb)
- **Epic:** `jira-reb-3527` — Enable MCP on AWS
- **Date:** 2026-08-24
- **Relates to:** ADR 0049 (op-certs: two kinds, environment-attributed), ADR 0050 (RS-only
  auth, `static` verifier, deferred AWS recipe), ADR 0079 (continuous auto-deploy on-box timer),
  and the local `origin/main`-tracking updater precedent (`sticky-genetic-narwhal`).

## Context

ADR 0050 gave `rebar-mcp` a remote-capable HTTP transport (OAuth 2.1 Resource-Server model,
five verifier modes, fail-closed transport hardening) but **explicitly deferred the AWS
recipe** — "how you actually stand one up on the box" — to follow-on work. The operator intent
now is to **dogfood**: run a self-contained rebar MCP server on the existing AWS box (the same
host that runs Gerrit + the review-bot + the opcert job-service) so that Copilot, Codex, and
Claude clients can drive **all** rebar operations through it, exactly as a user would against
their own MCP install. This is analogous to a user-facing rebar MCP install — not a bespoke
one-off — so its setup and configuration must match the user-facing docs (`docs/mcp-auth.md`),
and where those docs are incomplete the implementing slices update them.

Three decisions were deferred and are settled here (with the operator, 2026-08-24): how the
server is exposed and authenticated; how it is auto-deployed without killing in-flight
long-running ops; and how op-certs minted through MCP become enforceable without changing
ADR 0049's single-environment enforcement model. This ADR is the decision record only; the
infra / autodeploy / secrets / client-config / guidance / trusted-env / live-verification
slices under the epic implement it.

## Decision

### 1. Exposure + auth — nginx `/mcp/` TLS edge + `static` verifier + per-client bearer PATs

`rebar-mcp` runs as a **loopback-bound HTTP server** (`transport=http`,
`http_tls_at_edge=true`, allowed hosts/origins set, `auth_resource_server_url` set to the
external `https://…/mcp/` URL) behind an **nginx `/mcp/` TLS-terminating location** — the
documented `docs/mcp-auth.md` §5 reverse-proxy recipe, mirroring how the opcert job-service is
already fronted on this box. The verifier mode is **`static`**: each client (Copilot, Codex,
Claude) presents its own bearer PAT in the `Authorization: Bearer <token>` header, and the
server holds only the SHA-256 digests (ADR 0050 §4).

**Rationale + prior art.** Research confirmed that all three target clients support a static
`Authorization: Bearer` header for a **remote HTTP** MCP server, and **none require OAuth/DCR**
for this. `static` is therefore the minimal verifier mode that satisfies all three clients
without standing up an Authorization Server. This is a **standard self-contained rebar MCP
server** (operator intent: analogous to a user MCP install), **not** the opcert
SigV4 / API-Gateway `proxy` pattern. The ADR-0050-required acknowledgements for a
non-loopback-reachable deployment — explicit host/origin allowlists **and** the TLS-at-edge
acknowledgement — are recorded and set by the infra slice (nginx terminates TLS; the app binds
loopback only).

Concrete config keys the infra slice sets (named here so the slice has no ambiguity):
`transport=http`, `http_tls_at_edge=true`, the host allowlist, the origin allowlist,
`auth_resource_server_url`, and `auth_strategies` = **`static`** (one static verifier instance,
token digests supplied via env-var **names**, never literals — ADR 0050 §"Consequences").

### 2. Autodeploy — blue-green immutable release modeled on the LOCAL main-tracking updater, NOT the review-bot stop-and-drain

The MCP autodeploy target does **not** copy the review-bot's stop-and-health-drain
(`docker compose up -d` + `/health` in-flight gate, ADR 0067). It follows the **local
`origin/main`-tracking updater** (`~/.local/bin/rebar-dev-update.sh`, ticket
`sticky-genetic-narwhal`): **immutable release + atomic pointer swap + retire-when-idle**.
Concretely on the box (docker):

1. Build the new image.
2. Start a **new** `rebar-mcp` container alongside the old one.
3. Health-check the new container.
4. **Atomically flip the nginx `/mcp/` upstream** to the new container (the `current`-pointer
   rename analog).
5. **Retire the old container off the critical path**, gated by a flock-style "still serving?"
   check (its active connections / its own `in_flight` gauge), with bounded retention, a hard
   retention cap, and an SNS alert when a busy old container cannot be reclaimed.

Overlap is capped **one-in / one-out** (never N parallel) and guarded by a memory-pressure
alarm, so the transient 2× `rebar-mcp` never forces the `t4g.large` (8 GiB) up to `t4g.xlarge`.

**Positive rationale (why blue-green over the review-bot pattern).** It eliminates the
review-bot's unsolved saturation gotcha (bug `7b4a`): a **shared** MCP server driven by N agents
pipelines long, billable LLM ops, so an `in_flight` gauge may **never** reach 0; a gauge-gated
drain bound would then be exhausted and **kill an in-flight certified op**, and merely enlarging
the bound only defers the same kill. Blue-green **decouples retirement from the deploy**, so a
never-idle server never blocks a deploy and never forces a kill. **In-place reload is rejected
outright**: rebar lazy-imports late (e.g. `commit_impact` during `close`, after the minutes-long
completion verifier), so swapping code under a running process risks an `ImportError` mid-op
(two real local incidents). A killed MCP op is client-visible (dropped HTTP connection → the
agent retries) and rebar's event-sourced writes are idempotent (an unpersisted op-cert is simply
re-requested), so retirement is safe. The local updater is cited as **external local prior art**
for the immutable-release + retire-when-idle shape; it is not an in-repo module this box target
depends on.

The `in_flight` gauge counts only **long-running / certified / LLM** ops (`review_plan`,
`verify_completion`, `review_code`, `scan_spec` — 30 s to minutes); trivial sub-second reads are
not counted (killing them is harmless — the client retries). The gauge drives the **retire**
check, not the deploy gate. The paired ADR 0079 amendment records this `mcp` target.

### 3. Certified-op routing — MCP signs under the box's EXISTING opcert environment; code review stays the Gerrit vote

For the two op-cert kinds minted via MCP (plan-review at claim, completion-verifier at close —
ADR 0049) to be **enforceable**, the MCP server signs them under the box's **EXISTING** opcert
signing environment: the same `REBAR_OPCERT_ENV_ID` + signing key already pinned in
`.rebar/trusted_environments.yaml` on `main` and already named by the **single-valued**
`verify.require_environment` (ADR 0049). There is **no new `env_id`** and **no change to the
single-valued enforcement gate**: the box is not a distinct third environment, so a cert minted
via MCP verifies against the same environment the opcert job-service already uses.

> **Operator ruling (2026-08-24, Option A).** A distinct MCP `env_id` (Option B) was rejected
> because it would require making `verify.require_environment` **multi-valued** — a behavioral
> change to ADR 0049's single-environment enforcement model, out of scope for this epic. The
> `env_id` is the **UUID** pinned in `.rebar/trusted_environments.yaml`; the human-readable
> `nava-opcert-prod` label is only the SSH-key comment, and `verify.require_environment` matches
> by UUID, not by label.

rebar issues exactly **two** op-cert kinds (plan-review, completion-verifier — ADR 0049); there
is **no code-review op-cert**. "Certified code review" is the existing Gerrit **`LLM-Review`**
bot vote already running on the box — **no new op-cert kind is introduced**. The opcert
job-service and the `remote-cert` client continue to coexist (this is **additive**, not a
replacement). The epic's item-7 wording ("code review … certified through the MCP server") is
corrected accordingly in the guidance slice.

**Enforcement is opt-in and grandfathered, not retroactive.** `verify-opcert` enforces only the
**completion-verifier** op-cert kind, and only when **both** `verify.require_environment` (which
environment must sign) **and** `verify.opcert_enforce_since` (the grandfather boundary ref) are
set: a closed ticket whose close-anchor commit is a **descendant** of the boundary is enforced;
anything predating it is grandfathered (advisory). The committed `rebar.toml [verify]` today
sets neither, so environment-binding is **advisory**. The trusted-env / gate slices flip
`require_environment` **together with** `opcert_enforce_since` at a deploy-time anchor, so only
post-deploy closes are enforced and unsetting `require_environment` fully reverts — this ADR
does not itself enable enforcement. Plan-review certs are minted under the **same** environment
but are gated separately (they are not the `require_environment` lane).

### 4. Ticket store — a DEDICATED read-write clone at `/var/gerrit/site/mcp-tickets`

The server needs a ticket store to read, and three questions had to be settled before one could
be provisioned: does it serve the shared `tickets` branch at all, does it get its own volume or
share the review-bot's, and is it a reader or a writer. Recorded here because the answers are
load-bearing for anyone reasoning about blast radius, and they were previously only in the
bug's comments (bug `kilted-nuclear-bronco`).

**FULL READ-WRITE, serving the shared `tickets` branch.** Of read-only + certified ops,
full read-write, and certified-ops-only-with-no-store, the operator ruling is full read-write:
the server serves the shared branch and is a WRITER. `REBAR_MCP_READONLY` stays unset. This is
what makes the MCP surface the primary way agents drive the tracker rather than a read mirror.

**A DEDICATED volume, `/var/gerrit/site/mcp-tickets`** (`gerrit_mcp_tickets`), not the
review-bot's store. Sharing would put two independent writers behind one directory. rebar's
write lock would serialize them, but the review-bot's store was provisioned for a single writer
and nothing in its design anticipates a second. A dedicated clone costs one directory and keeps
the blast radius of an mcp-side store fault off the review-bot — which matters, because that
store has its own history of divergence faults.

**Credentials fall back to the review-bot PAT.** `MCP_TICKETS_PAT` is materialized from
`/rebar/prod/mcp-tickets-pat`, but `fetch-secrets.sh` falls back to
`/rebar/prod/reviewbot-tickets-pat` when the dedicated slot is blank. Both credentials do the
same thing against the same target — clone and push `tickets` of `navapbc/rebar` — so requiring
a duplicate secret to exist first would have left the store unprovisioned and the endpoint
reporting "not initialized" purely for want of a copy. The dedicated slot remains PREFERRED and
wins when populated.

**Consequence worth stating plainly:** a writer sharing the busiest branch in the repo means
every MCP write auto-commits and pushes, and contends with every other writer. That is accepted,
not overlooked — see the store-provisioning bugs discovered from this decision
(`fathomable-yester-thrip`, `unfit-beneficial-whimbrel`, `mobile-groovy-badger`).

## Consequences

- ADR 0050's deferred AWS recipe is now filled for this box: a self-contained, TLS-fronted,
  `static`-authenticated `rebar-mcp` reachable by all three clients, configured to match the
  user-facing `docs/mcp-auth.md` (incomplete/inaccurate sections are corrected by the guidance
  slice as it wires the concrete keys).
- The deploy path never kills an in-flight certified op, because retirement is decoupled from
  the deploy (Decision 2); the cost is a custom blue-green loop (recorded as the ADR 0079 `mcp`
  target) instead of reusing the review-bot's stop-and-drain.
- Op-certs minted via MCP are enforceable **without** touching ADR 0049's single-environment
  model, because the box reuses the existing environment rather than registering a distinct one
  (Decision 3). No `verify.require_environment` widening is needed or performed.
- Enforcement remains advisory until the trusted-env/gate slice flips `require_environment` +
  `opcert_enforce_since` together; historical closes are grandfathered, so enabling the gate
  never retroactively breaks the merge-gate.

## Back-out

This ADR records decisions only; the reversible surface lives in its implementing slices —
tear down the nginx `/mcp/` location + the `rebar-mcp` container(s) (Decision 1/2), disable the
`mcp` autodeploy target (ADR 0079 back-out shape: `systemctl disable`), and leave
`require_environment` unset to keep op-cert environment-binding advisory (Decision 3). None of
these touch the `gerrit`/review-bot/opcert services, which are unchanged.
