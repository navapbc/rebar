# S3 ticket-store backend (optional)

rebar's ticket event log normally lives on an orphan `tickets` branch that `sync.remote`
pushes to a git remote — typically GitHub. For teams that must keep ticket history **entirely
out of GitHub** (or any git host) and inside their own AWS account, rebar can instead sync the
`tickets` branch to an **S3 bucket**, served by the optional [`git-remote-s3`][grs3] helper.

This backend is a **purely optional add-in**. It adds **no** dependency to core rebar: the
`git-remote-s3` helper (and its transitive `boto3`) install only under the `[s3]` extra, and
nothing in core rebar imports them. A default install neither pulls in nor requires anything
S3-specific.

The event/data format is unchanged — the S3 remote stores the very same git objects a normal
git remote would, as `git bundle` files under a key prefix. Everything else about rebar (the
CLI, the event schema, reconcile, gates) works identically.

[grs3]: https://github.com/awslabs/git-remote-s3

## 1. Install the optional dependency

The helper ships behind rebar's `[s3]` extra:

```sh
pip install 'nava-rebar[s3]'
```

This installs [`git-remote-s3`][grs3] (Apache-2.0) with a minimum of **`git-remote-s3>=0.3.2`**
— the first release carrying the `IfNoneMatch` per-ref push lock that prevents the
multiple-bundles corruption the auto-doctor (below) heals. Confirm the git remote helper is on
your `PATH`:

```sh
git-remote-s3 --help   # git resolves `s3://…` remotes through this binary
```

If the binary is absent, git cannot talk to an `s3://` remote and rebar's sync will fail with a
transport error naming the missing helper.

## 2. Create the bucket (SSE-KMS) and the IAM policy

Create a dedicated bucket with **default encryption set to SSE-KMS using a customer-managed
key (CMK)**, versioning enabled, and public access fully blocked:

```sh
aws s3api create-bucket --bucket my-org-rebar-tickets --region us-east-1
aws s3api put-bucket-versioning --bucket my-org-rebar-tickets \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket my-org-rebar-tickets \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket my-org-rebar-tickets \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "aws:kms",
        "KMSMasterKeyID": "arn:aws:kms:us-east-1:111122223333:key/<cmk-id>"
      },
      "BucketKeyEnabled": true
    }]
  }'
```

> **Why bucket-level default encryption is required.** The `git-remote-s3` helper writes
> objects with a plain `put_object` and does **not** request `ServerSideEncryption` per object.
> At-rest encryption therefore comes from the **bucket's default encryption**, so the SSE-KMS
> default above is what actually encrypts your ticket history — it is not optional hardening.

Scope an IAM policy to exactly this bucket **and** the CMK (the helper needs `kms:Decrypt` +
`kms:GenerateDataKey` to read/write SSE-KMS objects):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RebarTicketStoreObjects",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::my-org-rebar-tickets",
        "arn:aws:s3:::my-org-rebar-tickets/*"
      ]
    },
    {
      "Sid": "RebarTicketStoreKms",
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
      "Resource": "arn:aws:kms:us-east-1:111122223333:key/<cmk-id>"
    }
  ]
}
```

## 3. Configure the remote and `sync.remote`

Add a git remote whose URL is `s3://bucket/prefix`.

```sh
git remote add s3tickets s3://my-org-rebar-tickets/tickets
```

Set the remote name in the project's `rebar.toml`.

```toml
[sync]
remote = "s3tickets"
```

The URL may name an AWS profile with `s3://profile@bucket/prefix` (the helper passes it to
`boto3.Session(profile_name=…)`); with no profile it uses the default boto3 session. rebar
pushes/fetches/reconciles the `tickets` branch against this remote exactly as it would against
a GitHub `origin`. (Code review and the code mirror still live on their own remotes — see
[config.md](config.md); only the **ticket store** moves to S3.)

## 4. Trust boundary

- **Credentials never touch rebar.** AWS credentials come from the standard boto3 chain
  (environment, `~/.aws/credentials`, SSO, an instance/task role, …), resolved by the helper —
  they are **never** stored in rebar config, and **never** written into ticket events. rebar
  passes only the `s3://…` URL.
- **Data at rest is SSE-KMS in your bucket.** Ticket history is encrypted with your CMK via the
  bucket's default encryption (see §2); the key stays in your account under your key policy.
- **Access is IAM/SigV4 only.** All S3 access is authenticated (SigV4) and authorized by the
  scoped IAM policy above. Block public access at the bucket level (as in §2) so there is no
  anonymous read path to ticket history.

## 5. Operational behavior — self-healing

Two conditions unique to the bundle-per-push storage model are handled automatically, with **no
operator action and no data loss**:

- **Multiple-bundles auto-doctor.** When concurrent pushes leave more than one bundle for the
  `tickets` ref, the helper refuses further ref operations until the count returns to one.
  rebar detects this on push and runs a **non-interactive auto-doctor** that fetches every
  divergent bundle head and folds them into one by iterated lossless 2-parent merges (never an
  octopus merge, never discarding a head), writes the merged bundle **before** deleting the
  originals, and retries the push. This is strictly lossless: a real content conflict aborts the
  heal, deletes nothing, and surfaces `rebar fsck-recover` rather than dropping a parent. It
  contrasts deliberately with upstream's interactive `git-remote-s3 doctor`, which prompts and
  can discard a head.
- **Stale push-lock auto-clear.** The helper serializes pushes with a per-ref S3 lock object. If
  a client dies mid-push, the lock self-heals **during the next push's lock acquisition**: when
  `acquire_lock` finds the existing lock older than the TTL it deletes it and re-acquires in the
  same call (`git-remote-s3` v0.3.2 `remote.py`, `acquire_lock` at lines 352–400) — no separate
  step and no cross-client "steal" of a live lock. The TTL is
  `DEFAULT_LOCK_TTL_SECONDS = 60` (`remote.py:45`), overridable via the
  `GIT_REMOTE_S3_LOCK_TTL_SECONDS` environment variable — the name the code actually reads
  (`remote.py:95`, verified at tag `v0.3.2`). Note that upstream's README documents the older
  `GIT_REMOTE_S3_LOCK_TTL`, which the code does **not** honor, so set the `_SECONDS` form. If a
  stale lock ever persists, the helper's own contention message points operators at the manual
  `git-remote-s3 doctor --lock-ttl <seconds>` cleanup path (`remote.py:236–237`). The resulting
  one-time delay of up to ~60s is a rare, bounded cost; rebar intentionally does **not** lower
  it, because a shorter TTL risks clearing a lock from a slow-but-live push.

  > **Env-var name — verify against your installed version.** The code reads
  > `GIT_REMOTE_S3_LOCK_TTL_SECONDS` (v0.3.2 tag = commit `9f3290e`,
  > [`git_remote_s3/remote.py#L95`](https://github.com/awslabs/git-remote-s3/blob/9f3290e1f19090a9c11ae5b8c01ec8abe6184ab9/git_remote_s3/remote.py#L95)).
  > Upstream's README documents `GIT_REMOTE_S3_LOCK_TTL` (no `_SECONDS`), but that string appears
  > **only in the README** — it is present in **no `.py` file** of the release, so the code does
  > not honor it. Set the `_SECONDS` form; if you override the TTL, confirm the exact name in
  > your installed version's `git_remote_s3/remote.py` before relying on it.
