# Setting up authenticated identity & signing (client guide)

This is a **generic, project-agnostic** guide for enabling rebar's authenticated-authorship
signing in your project's environments. It uses placeholders throughout — substitute your own
identities, emails, key paths, and secret names. Nothing here is specific to any particular
deployment.

For the underlying model (identities, attestations, keyrings, the merge-gate), see
[`identity.md`](identity.md). This document is the practical **"how do I turn it on"** guide.

## The mental model in one paragraph

Every mutating ticket event can carry two things: **attribution** (`author_email` +
`author_id`, derived from who is writing) and a **signature** (`author_sig`, an SSHSIG
attestation proving the author's key signed the event). Attribution is automatic once an
**identity** exists whose email matches the writer's `git config user.email`. Signing
additionally requires the writer's **SSH private key** to be configured via
`identity.signing_key` (or the `REBAR_IDENTITY_SIGNING_KEY` environment variable). A CI
merge-gate (`rebar verify-identity`) can then re-verify every event's authorship against the
identity keyring. Enforcement is opt-in — until you turn it on, unsigned writes are allowed
and the gate is advisory.

## Step 1 — Create an identity (once per author)

An **identity** is a first-class ticket holding a name, an email, and one or more registered
SSH **public** keys. Create one per human, agent, or service account that writes tickets:

```
rebar identity create \
  --name "<Display Name>" \
  --email "<author-email>" \
  --key "ssh-ed25519 AAAA...<your public key line>" \
  [--self]     # also record it as THIS clone's current identity
```

- Generate the keypair with `ssh-keygen -t ed25519 -f <path> -N ""` (a passphrase-less key is
  required for non-interactive signing). Register only the **public** key on the identity;
  the private key never goes in the store or the repo.
- `--self` writes a git-ignored `.rebar/current_identity` pointer so this clone signs as this
  identity. You can also switch later with `rebar identity use <identity-id>`.

## Step 2 — How the writer's identity is resolved

On each write, rebar resolves the "current identity" in this order:

1. the `.rebar/current_identity` pointer, if it names an existing identity; else
2. a **case-insensitive match of `git config user.email`** against identity emails.

So if a writer's git email equals its identity's email, attribution works with no pointer.
Set the git email per environment to the intended author:
```
git config user.email "<author-email>"
```

## Step 3 — Configure the signing key **per environment**

Attribution alone is not a signature. To sign, point rebar at the **private** key. The
private key must be a real file on disk (SSHSIG signs with `ssh-keygen -Y sign -f <key_path>`),
so you deliver it differently in each environment. **Never commit the private key.**

### 3a. Local dev / agent clone

Keep your private key under `~/.ssh/` (per-machine, never committed) and set either:
```
# in your LOCAL, git-ignored rebar config (not a shared/tracked config file):
[identity]
signing_key = "~/.ssh/<your-signing-key>"
```
or, equivalently, export the env override:
```
export REBAR_IDENTITY_SIGNING_KEY="$HOME/.ssh/<your-signing-key>"
```

> **Persisting it.** A signing key configured only for one shell session signs only that
> session. To make every session sign, persist the setting (e.g. in your shell profile, or a
> per-machine config). If you skip this, your writes are still **attributed** (via git email)
> but **unsigned** — which is fine while enforcement is off.

### 3b. CI (GitHub Actions and similar)

A multi-line OpenSSH PEM private key **cannot** live in a single-line `KEY=VALUE` env file or
a plain env var used directly as a path. Store it as a CI **secret**, then at runtime
**materialize it to a `0600` file** and point rebar at that file:

```yaml
jobs:
  write-tickets:
    runs-on: ubuntu-latest
    env:
      # Set the key PATH at JOB level so EVERY step that runs rebar sees it.
      # (Exporting it from one step via $GITHUB_ENV does NOT reliably reach later
      #  steps' rebar invocations — set it here instead.)
      REBAR_IDENTITY_SIGNING_KEY: ${{ runner.temp }}/signing-key
    steps:
      - name: Configure the author identity
        run: git config --global user.email "<author-email>"

      - name: Materialize the signing key
        env:
          SIGNING_KEY: ${{ secrets.<YOUR_SIGNING_KEY_SECRET> }}
        run: |
          set -euo pipefail
          if [ -z "${SIGNING_KEY:-}" ]; then
            echo "signing key secret unset — writes will be unsigned"; exit 0
          fi
          echo "::add-mask::${SIGNING_KEY}"
          install -m 600 /dev/null "${RUNNER_TEMP}/signing-key"
          printf '%s\n' "${SIGNING_KEY}" > "${RUNNER_TEMP}/signing-key"

      - name: ... your rebar ticket operations ...
        run: rebar comment <id> "..."   # signs automatically now
```

**Two lessons worth repeating** (both cost real debugging):
1. **Materialize the key to a file, not an env-file line** — multi-line PEM keys break the
   `KEY=VALUE` format.
2. **Set `REBAR_IDENTITY_SIGNING_KEY` at the JOB level**, not by exporting it from an earlier
   step. A per-step export via `$GITHUB_ENV` did not reach the later `rebar` write steps in
   practice, so writes came out attributed-but-unsigned. Job-level `env` guarantees every step
   sees it (the materialize step still writes the file at that path).

### 3c. Long-running / containerized services (bots)

Store the private key in your secret manager (Vault, cloud parameter store, etc.). A
multi-line PEM cannot be injected as a container env var; **materialize it to a `0600` file at
boot** (a dedicated boot step, not the container `.env`), then set `identity.signing_key` (or
`REBAR_IDENTITY_SIGNING_KEY`) to that path and the git email to the identity's email. If your
IaC declares the secret slot, **import an already-created secret into IaC state rather than
recreating it**, so an apply never clobbers the real value with a placeholder.

## Step 4 — Verify it's working

After configuring a signing key, make any write and check the event:
```
rebar comment <ticket> "hello"
rebar show <ticket>                 # the new event shows authorship: {signed: >=1}
rebar verify-authorship             # emits a "verified" verdict for signed events
```
The repo-wide CI merge-gate is `rebar verify-identity` (mount the ticket store first if your
store lives on a separate branch). It reports counts of `verified` / `unsigned` /
`unknown-author` / `bad-signature` events.

## Step 5 (optional) — Turn on enforcement

Signing is advisory until you opt in. When you're ready:

- Set `identity.require_authenticated = true` so the local write-gate refuses an unsignable
  write of a non-exempt type, and the CI merge-gate **fails** on any in-scope unsigned/
  unverified event.
- Set a **grandfathering boundary** so pre-enforcement history doesn't fail the gate: config
  key `identity.enforce_since`, overridable in CI by the `REBAR_IDENTITY_ENFORCE_SINCE`
  environment variable. Point it at the earliest event you want enforced (e.g. the
  enforcement-cutover commit).
- Roll out in the safe order: **provision keys in every writer environment and confirm signed
  events first**, *then* flip enforcement on — otherwise the gate goes red on writers that
  aren't signing yet.

## Which ticket types are exempt

Some event/ticket types are authorship-gate-exempt (e.g. session logs, code-review artifacts,
and identity tickets themselves) so bootstrapping and non-work artifacts are never blocked.
Everything that represents real work is subject to attribution/signing.

## Quick checklist

- [ ] An identity ticket exists for each writer, with its **public** key registered.
- [ ] Each writer's `git config user.email` matches its identity's email (or a
      `.rebar/current_identity` pointer is set).
- [ ] Each environment configures `identity.signing_key` / `REBAR_IDENTITY_SIGNING_KEY` to the
      writer's **private** key (materialized to a `0600` file in CI/containers; job-level env in CI).
- [ ] `rebar show` shows `authorship: {signed: >=1}` and `rebar verify-authorship` reports
      `verified` for a test write in each environment.
- [ ] (When ready) `identity.require_authenticated = true` + a sensible `enforce_since`
      boundary, flipped on only after every environment signs.
