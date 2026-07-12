# Authenticated identity — the model end-to-end

rebar can attribute and cryptographically certify *who* authored each change, map
contributors to Jira accounts by opaque id, and (opt-in) enforce authenticated
authorship at the merge gate. This guide is the operator/agent reference for that
system. The load-bearing design decisions are recorded in
[adr/0045-authenticated-identity.md](adr/0045-authenticated-identity.md).

## The `identity` entity

An **identity** is a first-class, event-sourced ticket-type (like `session_log`):
gate-, graph-, lifecycle-, and Jira-sync-exempt, so it never pollutes the
dependency graph, the ready/next-batch scheduler, or the Jira reconciler. It holds:

- `name` (its title) and `email`
- `mappings` — a list of `{provider, external_id}` keyed on the **provider's opaque
  id** (a Jira `accountId`, a GitHub node id, …), **never** an email
- `keys` — OpenSSH authorized-keys public-key lines (`ssh-ed25519 AAAA…`)

Create one and set it as your self-identity:

```sh
rebar identity create --name "Ada Lovelace" --email ada@example.com \
    --mapping jira:557058:0a1b… --key "ssh-ed25519 AAAA…" --self
rebar identity use <identity-id>   # writes the git-ignored .rebar/current_identity pointer
```

`resolve_current_identity()` reads that pointer, falling back to a case-insensitive
`git config user.email` match; every miss (no pointer, no match, ambiguous match,
`git` unavailable) returns `None` — identity is **opt-in**, so unauthenticated is a
valid state that never raises.

## Tiers: real identities vs. placeholder ghosts

Not every contributor is known locally. When rebar pulls an inbound Jira ticket, its
assignee may be a Jira user with no local mapping. rebar then mints a **placeholder
(ghost) identity** storing the raw `(provider, external_id, display_name)`, tagged
`placeholder`:

- `rebar.ensure_identity_for(provider, external_id, display_name)` is idempotent: it
  resolves the mapping first, so re-running yields **one** identity, not two.
- When a real name/mapping later appears, the placeholder is **upgraded in place**
  (keyed on the stable `external_id`) — a human's real identity is never overwritten
  and never forked.
- `rebar.is_placeholder(id)` distinguishes a ghost from an enriched identity.

## Event attribution

Every locally-written mutating event carries denormalized `author` (name) and
`author_email` in its envelope (git-style), plus an optional `author_id` referencing
an identity when one resolves. Old events without these fields replay to the same
state — attribution is additive and back-compatible. The reducer surfaces the
author's identity at the ticket's top level and per comment/revert/signature entry.

## Authorship attestation (`rebar.authorship.v1`)

Authorship signatures ride the foundation asymmetric-attest substrate (DSSE +
SSHSIG). A signature is produced with `sign_authorship(payload, key_path, principal)`
and verified with `verify_authorship(envelope, identity_id)`; the **trust root is the
identity entity's own in-band public keys** (never the envelope), and the scheme is
pinned by source policy (`rebar.attest.registry`), closing the alg-confusion class.
An event signed by identity A does not verify against identity B, and any tampering
of the payload fails verification.

## Key lifecycle: TOFU genesis, signed rotation, era-scoped validity

Keys have a lifecycle with cryptographic continuity:

- **Genesis (TOFU).** The first key on an identity is trust-on-first-use — accepted
  with no prior signature (there is nothing to chain to).
- **Signed rotation.** Every subsequent `identity key add|revoke` must be signed by a
  currently-valid key of that identity (filling git-bug's unimplemented `IsProtected`).
  An unsigned non-genesis change is refused.
- **Era-scoped validity.** A key is valid for a **merged-log epoch** window
  `[added_epoch, revoked_epoch)`. The epoch is the key event's position in the
  reducer's canonical (HLC-filename-ordered) replay — the authoritative merged-log
  position, **not** an attacker-controllable event timestamp. So an event signed by a
  key revoked at an earlier epoch fails; one from within the key's valid epoch passes.
  This survives compaction: the keyring epoch counter is frozen into and restored from
  the ticket's SNAPSHOT.

## Provider-neutral Jira mapping (outbound)

The reconciler sets the Jira **assignee/reporter by accountId**, resolved through the
identity's `{provider:"jira"}` mapping behind a provider seam
(`resolve_mapping(provider, external_id)`, `jira_account_id(local_assignee)`), instead
of by fuzzy display-name/email search:

- The assignee fast-path submits the resolved `accountId` directly, skipping Jira's
  `/user/assignable/search`. A best-effort, transient `/user/search?query=<email>`
  bootstrap resolves an accountId when only an email is known (exact-match only;
  zero-or-many matches degrade).
- **Reporter** is applied as a dedicated REST sub-call and is set **only where the
  project grants Modify-Reporter**. Where it does not (the common case), or when the
  reporter can't be resolved, the reconciler records a soft-fail
  (`outbound-reporter-not-permitted` / `-unresolved`) and continues — it never hard-
  fails a sync.
- Anything unresolvable degrades to unassigned / project-default without failing.

## Opt-in enforcement — the merge-gate is the control

Enforcement is **project-opt-in** via `identity.require_authenticated` (default off):

- **The merge-gate is the real control.** `rebar verify-authorship` re-verifies every
  in-scope mutating event's signature against the author identity's epoch-scoped
  keyring, classifying each `verified` / `unsigned` / `unknown-author` /
  `bad-signature`. It exits non-zero (blocking the change) only when
  `require_authenticated` is on and any in-scope event is not verified; advisory
  (exit 0) when off. CI runs it as the merge gate. Gate-exempt types
  (`identity`/`session_log`/`code_review`) are skipped — an identity's own bootstrap
  CREATE is unsigned by construction.
- **The local write-gate is UX-only.** With `require_authenticated` on, a write that
  cannot be signed (no resolvable identity, or no `identity.signing_key`) fails fast
  as a convenience — but it is *not* the security boundary (a determined user can
  bypass it locally). The merge-gate is what actually enforces history.
- **Replay never rejects.** A bad-signature or unsigned event still folds into
  compiled state — no clone can be bricked by an unverifiable event. The reducer
  records only presence counts (`authorship: {signed, unsigned}`), surfaced by `show`
  and `fsck`; the cryptographic verdict is exclusively the merge-gate's job.
- **Snapshots carry signatures.** Compaction writes an `authorship_ledger`
  (`{event_uuid, author_id, author_sig, epoch}` per signed folded event) into the
  SNAPSHOT, so compacted signed events still verify after their raw event files are
  retired.

To sign your own writes, configure `identity.signing_key` (the path to your
identity's SSH private key) and set your self-identity; `append_event` then stamps an
`author_sig` on every event you write.
