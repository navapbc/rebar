# ADR 0093 — S3 ticket-store backend via `git-remote-s3` (optional, below the git layer)

**Status:** Accepted
**Date:** 2026-08-20
**Relates to:** ADR 0020 (two-vote CI gate — unaffected), the `sync.remote` split-residency model in [config.md](../config.md)

## Context

rebar's ticket event log is an orphan `tickets` branch that `sync.remote` pushes to a git
remote — in practice GitHub. Some teams have a hard requirement to keep ticket history **out of
any git host** and inside their own AWS account (data-residency, an existing S3/KMS security
posture, no third-party tracker). rebar already treats the tickets branch as an ordinary git
remote, so the question is narrow: **can we point `sync.remote` at S3 without changing rebar's
event/data model, and without adding an AWS dependency to core rebar?**

Constraints:

- **R1 — core stays lean.** A default install must not pull in `boto3`/AWS anything. The S3
  backend is opt-in only.
- **R2 — event format unchanged.** The store on S3 must be the same git objects, so reconcile,
  gates, `fsck`, and the schema are untouched.
- **R3 — no data loss under concurrency.** The backend must survive concurrent pushes without
  losing ticket events.
- **R4 — encryption at rest with a customer key.** Ticket history must be SSE-KMS under a CMK.

## Decision

**Adopt [`awslabs/git-remote-s3`][grs3] (Apache-2.0, actively maintained) as an optional
git-remote helper *below* rebar's shell-out git layer.** rebar shells out to `git push`/`git
fetch` exactly as today; git resolves an `s3://bucket/prefix` remote through the helper binary,
which stores the branch as `git bundle` objects under a key prefix. Because the integration sits
under the git CLI, **rebar's event/data format is unchanged** (R2), and the helper ships behind
the `[s3]` extra so **core rebar never imports it** (R1).

**SSE-KMS (server-side), not client-side encryption.** Encryption is the bucket's default
SSE-KMS with a customer CMK (R4). The helper writes plain `put_object` calls without a per-object
`ServerSideEncryption` argument, so bucket-default encryption is load-bearing; the operator guide
([s3-backend.md](../s3-backend.md)) makes the SSE-KMS bucket default a required setup step.
Client-side encryption was rejected: it would fork the object format, break the "same git
objects" property (R2), and duplicate key management that KMS already provides.

**Multiple-bundles risk handled by a rebar merge-based auto-doctor, not the upstream doctor.**
The helper stores one bundle per push and refuses ref operations when concurrent pushes leave
more than one bundle for a ref. Upstream ships an **interactive** `git-remote-s3 doctor` that
prompts and can **discard** a head — unacceptable for an autonomous, lossless ticket store (R3).
rebar instead detects the multi-bundle signature on push and runs a **non-interactive
auto-doctor** ([`src/rebar/_store/s3_doctor.py`](../../src/rebar/_store/s3_doctor.py)) that folds
every divergent bundle head into one by iterated lossless 2-parent merges — never an octopus
merge, never discarding a head — writing the merged bundle before deleting the originals
(crash-idempotent), and aborting to `rebar fsck-recover` on a genuine content conflict rather
than dropping a parent.

**All `git-remote-s3` private-API access is confined to a single thin adapter.** `git-remote-s3`
exposes no stable public API for the operations the doctor needs (bundle enumeration, the per-ref
repair lock, the raw object writes/deletes to collapse bundles). Rather than scatter private-API
calls across rebar, the doctor routes **every** touch of the helper's internals — its `S3Remote`,
`parse_git_url`, `.s3` boto3 client, `acquire_lock`/`release_lock`, `get_bundles_for_ref`,
`init_remote_head` — through one adapter function in `s3_doctor.py`. That gives the private-API
dependency exactly one swappable seam, so an upstream API change (or a switch to a different
helper) is a single-site edit, and no rebar-owned boto3/credential/key-layout code exists beyond
the one bundle-key string the helper defines.

**Lowering the push-lock TTL was rejected.** The helper self-heals a stale per-ref push lock by
clearing and re-acquiring it during the next push's lock acquisition once it exceeds
`DEFAULT_LOCK_TTL_SECONDS = 60` (`git-remote-s3` v0.3.2 `remote.py`: `acquire_lock` lines
352–400; default at line 45). The TTL is overridable via `GIT_REMOTE_S3_LOCK_TTL_SECONDS` —
the name the code reads (`remote.py:95`, verified at tag `v0.3.2` = commit
[`9f3290e`](https://github.com/awslabs/git-remote-s3/blob/9f3290e1f19090a9c11ae5b8c01ec8abe6184ab9/git_remote_s3/remote.py#L95));
upstream's README still prints the older `GIT_REMOTE_S3_LOCK_TTL`, a string that appears only in
the README and in no `.py` file of the release, so the code ignores it.
Lowering the TTL to shorten the rare post-crash delay was
considered and rejected: a shorter window risks clearing a lock from a slow-but-live push,
trading a bounded, rare delay for a correctness hazard. The 60s default stays.

[grs3]: https://github.com/awslabs/git-remote-s3

## Consequences / accepted costs

- **Opt-in only.** The backend is invisible to a default install; only operators who run
  `pip install 'nava-rebar[s3]'` and set `sync.remote` to an `s3://` remote get it.
- **A private-API dependency exists, but is contained.** rebar depends on `git-remote-s3`
  internals for the doctor; the single-adapter seam (above) bounds the blast radius of an
  upstream change and is the maintenance cost we accept for a lossless autonomous heal.
- **Bundle-per-push storage** means the multi-bundle state is normal under concurrency; it is
  handled automatically by the auto-doctor and is not an operator-visible failure.
- **Back-out.** Set `sync.remote` in the project `rebar.toml` to the name of the Git remote that will hold the ticket store. This example uses `origin`.

  ```toml
  [sync]
  remote = "origin"
  ```

  The event format is unchanged. Moving the tickets branch back to a Git host is an ordinary push to the other remote.
