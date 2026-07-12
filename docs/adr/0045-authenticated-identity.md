# ADR 0045 — Authenticated identity: git-bug entity, opaque-id keying, in-band TOFU

## Status

Accepted (epic gnu-whale-ichor).

## Context

rebar needed to attribute and cryptographically certify authorship of ticket events,
map contributors to external providers (Jira) reliably, and let a project *enforce*
authenticated authorship — without bricking the always-readable, replicated,
event-sourced store, and without standing up external PKI. Three cross-cutting
decisions shaped the whole design. See [../identity.md](../identity.md) for the
resulting model, and ADR 0044 for the asymmetric-attestation substrate this builds
on.

## Decision

### 1. Identity is a first-class, event-sourced entity (git-bug style)

An identity is modeled as a **gate-/graph-/sync-exempt ticket-type** on the existing
event machinery — the same substrate `session_log`/`code_review` use — rather than a
side table, a config file, or an external directory.

- **Why:** it inherits event-sourcing, replication, offline availability, and
  `show`/`search` for free; it round-trips through every clone with no new storage or
  sync path; and the exemption keeps it out of the dependency graph, the scheduler,
  and the Jira reconciler so it never distorts the work-tracking hot paths.
- **Alternatives rejected:** a bespoke `.rebar/identities.json` (no replication, no
  history, a second concurrency model) and an external IdP (network dependency,
  breaks the offline/append-only guarantee).

### 2. Mappings key on the provider's opaque id, never email

Every `{provider, external_id}` mapping is keyed on the provider's **stable opaque
id** (a Jira `accountId`, a Gitea `OriginalAuthorID`, …), and email is stored only as
denormalized display data.

- **Why:** emails are mutable, reused, privacy-hidden (Jira suppresses them), and
  ambiguous; the opaque id is stable and authoritative. Keying on it makes inbound
  ghost minting **idempotent** and provider-neutral (a placeholder upgrades in place
  instead of forking), and makes outbound assignee/reporter resolution deterministic
  (submit the accountId directly instead of a fuzzy name/email search that can
  mis-assign).
- **Alternatives rejected:** email-keyed mappings (churn + collisions on every
  rename/privacy change) and display-name matching (Jira's substring search
  mis-resolves agent identities onto unrelated accounts).

### 3. In-band trust-on-first-use with a signed rotation chain and era-scoped validity

Public keys live **in-band on the identity entity**. The first key is trusted on
first use (TOFU); every subsequent add/revoke must be signed by a currently-valid
key (a signed rotation chain, filling git-bug's unimplemented `IsProtected`); and a
key's validity is scoped to a **merged-log epoch** window, not wall-clock.

- **Why:** in-band keys mean verification needs no external key server — the trust
  root travels with the identity. The signed chain gives cryptographic continuity: an
  attacker who appends a KEY event can't inject a key without a valid signer. Anchoring
  validity to merged-log position (the reducer's canonical HLC-ordered replay index,
  frozen into snapshots) rather than the event's self-reported timestamp stops an
  attacker from backdating a forged event into a revoked key's window.
- **Consequences:** enforcement is opt-in and lives at the **merge-gate** (CI), never
  at replay — replay never rejects, so the store stays always-readable; the local
  write-gate is UX-only. A pre-existing identity's static `keys` bootstrap an epoch-0
  genesis keyring so it works without an explicit KEY event.
- **Alternatives rejected:** an external key directory (network/PKI dependency);
  wall-clock key validity (attacker-controllable, non-deterministic across clones);
  and enforcing at replay (would let one unverifiable event brick every clone).

## Consequences

- The identity model is fully replicated, offline-capable, and history-preserving.
- Provider integration (Jira assignee/reporter, inbound ghosts) is deterministic and
  degrades gracefully rather than hard-failing.
- Authenticated-authorship enforcement is available but opt-in, with the security
  boundary at the merge-gate and the store always readable.
