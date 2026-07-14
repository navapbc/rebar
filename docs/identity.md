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

- `rebar.create_placeholder(provider, external_id, display_name)` mints the ghost
  (a keyless, partial-data identity); it is a thin idempotent alias over
  `rebar.ensure_identity_for(...)`, which resolves the mapping first, so re-running
  yields **one** identity, not two.
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
SSHSIG) and wrap an **in-toto Statement**. `sign_event_authorship(event, key_path,
principal)` builds a Statement whose single `subject` binds the event as
`{name: <event_uuid>, digest: {sha256: <content_hash>}}` — where `content_hash` is
the SHA-256 of the event's canonical bytes with `author_sig` EXCLUDED — sets
`predicateType = application/vnd.rebar.authorship.v1+json`, and signs it in a DSSE
envelope. `verify_event_authorship(event, envelope, identity_id)` parses the
Statement, checks the subject binds *this* event's uuid and content hash, and only
then verifies the DSSE signature. Because the binding lives in the signed Statement
subject, a signature cannot be replayed onto a different event.

The **trust root is the identity entity's own in-band public keys** (never the
envelope), and the scheme is pinned by source policy (`rebar.attest.registry`),
closing the alg-confusion class. An event signed by identity A does not verify
against identity B, and any tampering of the event body or the signature fails
verification. (The low-level `sign_authorship(payload, …)` primitive is retained for
signing key-rotation operations, which sign an op-payload rather than an event.)

## Key lifecycle: TOFU genesis, signed rotation, commit-ancestry validity

Keys have a lifecycle with cryptographic continuity:

- **Genesis (TOFU).** The first key on an identity is trust-on-first-use — accepted
  with no prior signature (there is nothing to chain to).
- **Signed rotation.** Every subsequent `identity key add|revoke` must be signed by a
  currently-valid key of that identity (filling git-bug's unimplemented `IsProtected`).
  An unsigned non-genesis change is refused.
- **Commit-ancestry validity.** A key record binds `{public_key, added_at, revoked_at}`
  where `added_at`/`revoked_at` are the **positions** of the KEY_ADD/KEY_REVOKE events;
  a resolver maps each position to the **tickets-branch commit that introduced it**
  (`added_at_commit` / `revoked_at_commit`). An event is authored by a key iff the
  event's introducing commit **descends `added_at_commit` AND does NOT descend
  `revoked_at_commit`** (decided with `git merge-base --is-ancestor`). When a key
  operation shares an event's commit (rebar batches N events per commit), the tie is
  broken by intra-commit position order.

  **Why the DAG, not wall-clock/HLC?** The immutable commit graph is a tamper-proof
  fact: you cannot make a new event an ancestor of an existing revocation commit
  without forging SHAs. The earlier HLC-timestamp ordering was partly
  author-assignable — a malicious clone holding a since-revoked key could write an
  event with a backdated timestamp that sorted *before* the revocation and verified as
  valid. Commit ancestry closes that hole: the backdated event's real introducing
  commit still descends the revocation, so it fails. Validity survives compaction —
  the keyring folds into the ticket's SNAPSHOT, and a retired KEY event's introducing
  commit is still resolvable from git history.

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
  in-scope mutating event's signature against the author identity's commit-anchored
  keyring, emitting one of five verdicts per event:
  - `verified` — the in-toto Statement binds the event and the signature verifies
    against a key valid at the event's commit;
  - `key_not_valid_at_era` — the signature is cryptographically valid by a key the
    identity holds, but that key was NOT valid at the event's commit (e.g. a
    since-revoked key) — distinct from a forgery;
  - `bad-signature` — malformed, content-binding mismatch, or verifies against no key
    the identity ever held (forged);
  - `unknown-author` — no `author_id` / not an identity ticket;
  - `unsigned` — no `author_sig`.

  The user-facing display groups these as **verified** / **unverified**
  (`bad-signature` | `key_not_valid_at_era` | `unknown-author`) / **unsigned**. (This
  is the `verify-authorship` gate's own vocabulary; it is distinct from the separate
  `verify-signature` HMAC-attestation verdict set.) The gate exits non-zero (blocking
  the change) only when `require_authenticated` is on and any in-scope event is not
  verified; advisory (exit 0) when off. CI runs it as the merge gate. Gate-exempt
  types (`identity`/`session_log`/`code_review`) are skipped.
- **The local write-gate is UX-only.** With `require_authenticated` on, a write that
  cannot be signed (no resolvable identity, or no `identity.signing_key`) fails fast
  as a convenience — but it is *not* the security boundary (a determined user can
  bypass it locally). The merge-gate is what actually enforces history.
- **Replay never rejects.** A bad-signature or unsigned event still folds into
  compiled state — no clone can be bricked by an unverifiable event. The reducer
  records only presence counts (`authorship: {signed, unsigned}`), surfaced by `show`
  and `fsck`; the cryptographic verdict is exclusively the merge-gate's job.
- **Snapshots carry signatures.** Compaction writes an `authorship_ledger` into the
  SNAPSHOT — one self-contained record `{event_uuid, content_hash, signature,
  signer_pubkey, position}` per signed folded event, where `position` is
  `{commit_sha, position}` (the introducing commit + intra-commit order) and
  `signer_pubkey` is the actual key that verified the signature. A compacted event
  re-verifies from the ledger alone (no keyring re-scan needed), so signed events
  still verify after their raw event files are retired.

To sign your own writes, configure `identity.signing_key` (the path to your
identity's SSH private key) and set your self-identity; `append_event` then stamps an
in-toto Statement `author_sig` on every event you write.

## Setting up signing in a local dev / agent clone (story 472f)

Every human or agent clone writes non-exempt tickets, so each needs its **own** identity
(never the shared bot). One-time setup per machine:

1. **Own an identity ticket.** Create one (or reuse yours):
   `rebar identity create --name "<your name>" --email <your-git-email> --key "<your ssh public key line>" --self`.
   `--self` records it as this clone's current identity via the **git-ignored**
   `.rebar/current_identity` pointer. (Equivalently, `rebar identity use <id>` later.)
2. **Point `identity.signing_key` at your SSH PRIVATE key.** Either set
   `[verify]`-adjacent `identity.signing_key = "~/.ssh/id_ed25519"` in your **local**
   config (`.rebar/config.conf` or user config — NOT the shared `rebar.toml`), or export
   `REBAR_IDENTITY_SIGNING_KEY=~/.ssh/id_ed25519`. This key is **per-machine and is never
   committed** — only your PUBLIC key lives in the store (on the identity ticket).
3. **Git-email fallback.** If no `.rebar/current_identity` pointer is set, the resolver
   falls back to a **case-insensitive match of the store repo's `git config user.email`**
   against identity tickets (`resolve_current_identity`). So if your git email already
   matches your identity ticket's email, signing "just works" without an explicit pointer.

**Verify a signed write:** after setup, make any write (e.g. `rebar comment <id> "hi"`) and
run `rebar show <id>` — the new event shows `authorship: {signed: ≥1}`; `rebar
verify-authorship` emits a `verified` verdict for it. (Note: this is the SSH-authorship flow —
distinct from `rebar verify-signature`, which certifies HMAC *manifest* attestations from the
`rebar sign` command, not event authorship.)

## The CI merge-gate: `verify-identity.yaml` (story cc0b)

The authenticated-authorship control runs in CI as `.github/workflows/verify-identity.yaml`:

- **It mounts the tickets store.** The store lives on the `tickets` orphan branch, which a
  plain code checkout does not contain (a bare `rebar verify-identity` would exit 2, "store
  not found"). The workflow's "Mount tickets branch as a worktree" step fetches
  `+tickets:refs/remotes/origin/tickets` and `git worktree add -B tickets .tickets-tracker
  origin/tickets` (full history, so commit-ancestry scoping works), so the gate scans real
  events.
- **Posture is config-driven, not hard-forced.** The gating step passes **no**
  `--require-authenticated` / `--since` flags; enforcement follows
  `identity.require_authenticated` in `rebar.toml` (**currently `false` → advisory, exit 0**:
  it re-verifies and reports every in-scope event's authorship but does not block landing).
  It becomes blocking only when that flag is flipped on (a separate, deliberate step).
- **Grandfathering boundary — CI vs local.** The boundary that exempts pre-enforcement events
  is `identity.enforce_since` in `rebar.toml`, overridable in CI by the **environment
  variable `REBAR_IDENTITY_ENFORCE_SINCE`** (set in the workflow's `env:` block) — there is no
  `vars.ENFORCE_SINCE` GitHub Actions repo variable. Before enforcement is flipped on, this
  boundary must be set to an appropriate ref (e.g. the earliest tickets-store event, or the
  enforcement-cutover commit) so historical unsigned events are grandfathered and the gate is
  not spuriously red.
