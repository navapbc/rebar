# Signing a manifest of verified steps

rebar can record a **cryptographic attestation** on a ticket: a *manifest* (a JSON
array of verified-step strings) plus an HMAC-SHA256 signature. It is the
machine-checkable proof that a gate ran and that the steps it verified are unaltered
since — a signal of rigorous agentic development rather than a vibe-check.

> **For most projects you never sign by hand.** The attestation is produced
> automatically by the review gates: a passing **plan-review** (at claim), a passing
> **completion-verifier** (at close), and **code-review** all leave a signed record on
> the ticket. Reach for the commands below only if you want to sign manifests yourself
> or customize the process — for example, driving rebar from a shared deployment (an MCP
> server) with an injected key, or attaching your own verified-step manifests to a
> ticket outside the built-in gates.

## The commands

`rebar sign <id> <manifest>` records the attestation; `rebar verify-signature <id>`
recomputes the HMAC with the local key and **certifies** that the recorded steps still
match the signature:

```bash
rebar sign abcd-1234 '["unit tests: PASS", "security review: clean", "deployed to staging"]'
rebar verify-signature abcd-1234        # SIGNATURE: certified — verified steps match the signature
```

The library and MCP surfaces mirror the CLI: `rebar.sign_manifest(ticket_id, manifest)`
/ `rebar.verify_signature(ticket_id)`, and the write-gated MCP `sign_manifest` /
`verify_signature` tools (pass `kind` to certify a specific attestation kind, e.g.
`plan-review` / `completion-verifier`).

## The key

The signature is computed with a key that is **specific to the environment** rebar
runs in. The key is resolved from:

1. `REBAR_SIGNING_KEY` — injected out-of-band into a shared deployment (e.g. an MCP
   server), or, failing that,
2. a per-environment `.signing-key` file generated on first use (written `0600`,
   owner-only, gitignored, never committed, never shared).

Because the key never leaves the environment, `verify-signature` reports `foreign_key`
(rather than `certified`) when a record was signed by a *different* environment — only
the environment that holds the key can certify its own attestations.

## Trust model

- **The signature binds both the ticket id and the manifest**, so it cannot be replayed
  onto another ticket, and any edit to the step list invalidates it.
- **It is a shared secret (HMAC), not a public-key identity.** The attestation proves a
  signature was produced by a holder of the environment key and that the steps are
  unaltered since — nothing more. Anyone who can read the `.signing-key` file or the
  injected `REBAR_SIGNING_KEY` can forge a `certified` record, so protect read access to
  the environment accordingly.
- **It is a normal append-only `SIGNATURE` event**, so it replays into `rebar show`
  output, survives compaction, and flows to other clones like any other write.

## Where it plugs in

- **Close gate.** The completion-verification close gate
  (`verify.require_completion_verification_for_close = true`) signs a PASS
  `completion-verifier` verdict onto the ticket at close (re-verify if HEAD moved, or
  bypass with `--force-close=<reason>`). See the [Configuration](../README.md#configuration)
  section of the README.
- **Attestation kinds.** A ticket holds a kind-keyed `attestations` map — independent
  records of different kinds (`plan-review` at claim, `completion-verifier` at close)
  coexist rather than clobbering one slot. See
  [`docs/plan-review-gate.md`](plan-review-gate.md).
- **Reusing the machinery.** The `rebar.signing` API (key resolution, manifest
  canonicalisation, `sign_manifest` / `verify_manifest`) is documented for reuse in
  [`docs/reuse-surface.md`](reuse-surface.md).
