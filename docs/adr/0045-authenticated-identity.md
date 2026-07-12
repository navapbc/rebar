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

### 3. In-band TOFU, an in-toto authorship Statement, and git-commit-ancestry validity

Public keys live **in-band on the identity entity**. The first key is trusted on
first use (TOFU); every subsequent add/revoke must be signed by a currently-valid
key (a signed rotation chain, filling git-bug's unimplemented `IsProtected`).
Authorship signatures wrap an **in-toto Statement**, and a key's validity is anchored
to the **tickets-branch commit graph**, not wall-clock or replay-order.

- **In-toto Statement payload.** An authorship signature signs an in-toto Statement
  whose `subject` binds the event as `{event_uuid, content_hash}` (DSSE payloadType
  `application/vnd.rebar.authorship.v1+json`; `content_hash` = SHA-256 over the
  canonical event minus `author_sig`). **Why:** binding lives in the signed payload
  itself, so a valid signature over one event can't be replayed onto another, and a
  compacted ledger entry re-verifies from the recorded `content_hash` alone. It also
  puts authorship on a standard supply-chain attestation shape rather than an ad-hoc
  raw-bytes signature.
- **Git-commit-ancestry validity (not wall-clock, not HLC replay-order).** A key
  record binds `{public_key, added_at, revoked_at}` positions that resolve to the
  commits which introduced the KEY_ADD/KEY_REVOKE events (`added_at_commit`,
  `revoked_at_commit`). An event is valid iff its introducing commit **descends
  `added_at_commit` and does not descend `revoked_at_commit`** (`git merge-base
  --is-ancestor`), refined by intra-commit position when a key op shares the event's
  commit. **Why:** the commit DAG is tamper-proof — you cannot make a new event an
  ancestor of an existing revocation commit without forging SHAs. An earlier
  HLC-timestamp/replay-order model was partly author-assignable: a malicious clone
  holding a since-revoked key could write an event with a backdated timestamp that
  sorted before the revocation and verified as valid. Commit ancestry closes that
  hole because the backdated event's *real* introducing commit still descends the
  revocation. The reducer stays pure (it records positions, not commits); the
  git-aware resolver lives in the attest layer.
- **Self-contained snapshot ledger + a four-way verdict.** Compaction writes an
  `authorship_ledger` of `{event_uuid, content_hash, signature, signer_pubkey,
  position}` records so a compacted event re-verifies without the raw file or a
  keyring re-scan. The merge-gate distinguishes `key_not_valid_at_era` (a valid
  signature by a real-but-not-era-valid key) from `bad-signature` (forged) and
  `unknown-author` — so a revoked-key event is no longer indistinguishable from a
  forgery.
- **Consequences:** enforcement is opt-in and lives at the **merge-gate** (CI), never
  at replay — replay never rejects, so the store stays always-readable; the local
  write-gate is UX-only. A pre-existing identity's static `keys` bootstrap a genesis
  keyring (added at the CREATE commit) so it works without an explicit KEY event.
- **Alternatives rejected:** an external key directory (network/PKI dependency);
  wall-clock or HLC-replay-order key validity (attacker-controllable via backdating);
  a raw-canonical-bytes authorship signature (replayable, not self-describing); and
  enforcing at replay (would let one unverifiable event brick every clone).

## Consequences

- The identity model is fully replicated, offline-capable, and history-preserving.
- Provider integration (Jira assignee/reporter, inbound ghosts) is deterministic and
  degrades gracefully rather than hard-failing.
- Authenticated-authorship enforcement is available but opt-in, with the security
  boundary at the merge-gate, key validity anchored to the immutable commit DAG, and
  the store always readable.
