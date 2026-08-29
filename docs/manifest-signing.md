# Signing manifests and operation certificates

rebar can attach a manifest of verified steps to a ticket through a `SIGNATURE` event. New records are operation certificates. Each record contains a DSSE envelope whose payload is an in-toto Statement. The envelope carries an SSHSIG signature over its PAE bytes, produced with the signing environment's Ed25519 private key. The certificate principal identifies that environment. The plan-review and completion-verifier gates use this certificate form.

## Commands and APIs

`rebar sign <id> <manifest>` records a manifest certificate. `rebar verify-signature <id>` verifies one recorded certificate without an LLM call or a network request.

```bash
rebar sign abcd-1234 '["unit tests: PASS", "security review: clean", "deployed to staging"]'
rebar verify-signature abcd-1234
```

The library operations are `rebar.sign_manifest(ticket_id, manifest)` and `rebar.verify_signature(ticket_id)`. The MCP server exposes the write-gated `sign_manifest` tool and the read-only `verify_signature` tool. Pass `kind` when verification must select one entry from the ticket's kind-keyed `attestations` map.

The code-review gate reports its verdict through the configured review system. It does not create a ticket operation certificate.

## Environment key and principal

The default private key is `<tracker>/.opcert-key`. rebar generates this passphrase-free Ed25519 key on the first signing operation, writes the private key with owner-only permissions, derives `<tracker>/.opcert-key.pub`, and excludes both files from the ticket log. `REBAR_OPCERT_KEY_PATH` can select a provisioned private key instead.

The certificate principal comes from `REBAR_OPCERT_ENV_ID` when that deployment setting is present. Otherwise rebar uses the ticket store's `.env-id`. The principal attributes the certificate to the signing environment. It does not identify a person. Authorship attestations use a separate key, namespace, and trust model.

Signing requires OpenSSH 8.9 or newer. A missing or unusable `ssh-keygen` produces a signing error. Gate callers record that outcome as unsigned, and a gate that requires certification remains unsatisfied. Verification never creates a missing private key.

## Signed material and verification

The DSSE payload is an in-toto Statement that binds the ticket ID, attestation kind, material fingerprint, code commit, and full manifest. Verification accepts those bound values only after checking the SSHSIG signature against an Ed25519 public key. A copied certificate therefore cannot certify another ticket or another attestation kind.

`rebar verify-signature` verifies a DSSE envelope signed through SSHSIG. What it gates on is the **signature**, not the signing environment. Certification environment is **not** a gate under current operator policy (bug `c21f-6f29-5d2d-4a5a`): *"Any certification is as good as any other certification right now. Limited to a trusted set of environments is a future feature, but not currently in use."* A certificate minted by another environment therefore certifies here, provided its signature verifies — which is the ordinary deployment shape, where the on-box MCP server signs a review and a local CLI worktree consumes it.

The verifier picks the strongest key available and reports which one it used in `trust_basis`:

| `trust_basis` | key used |
|---|---|
| `own_key` | this environment's own op-cert key (the signer is this environment) |
| `pinned_environment` | a key pinned out-of-band for the signing environment in `.rebar/trusted_environments.yaml` |
| `envelope_key` | the signer's own key, carried inside the SSHSIG blob |

`envelope_key` is **self-consistent** rather than pinned: `ssh-keygen -Y verify` still checks the signature, the namespace and the principal binding in full, so a forged or altered envelope is still `mismatch`. What it does not establish is that the key belongs to a *known* environment — exactly the property the operator has deferred. The field exists so that weaker basis is **visible** rather than silently folded into `certified`.

`foreign_key` is now narrower: it means no usable signer key could be obtained at all, or the opt-in restriction below is configured and this certificate is from a different environment.

**Future, opt-in: restricting the trusted set.** Setting `verify.require_environment` (with `verify.opcert_enforce_since`, per `infra/runbooks/mcp-opcert-enforcement-flip.md`) re-enables the restriction: only that environment's certificates are accepted, they must verify against its key pinned in `.rebar/trusted_environments.yaml`, and the `envelope_key` basis is withheld. Both keys are unset in this project. `rebar verify-opcert` is the corresponding merge-gate lane, described in [ADR 0049](adr/0049-opcert-asymmetric.md).

A certified signature establishes the integrity of the signed process record. It does not establish that the reviewed work is defect-free. Gate validity also checks current ticket material, code freshness, reopen state, and related-ticket pins where applicable.

## Gate behavior

- A passing plan-review records a DSSE envelope that carries an SSHSIG signature over its PAE bytes, produced with the signing environment's Ed25519 key. Its principal identifies that environment, and the certificate is stored under the `plan-review` key.
- A passing completion-verifier records the same DSSE, SSHSIG, Ed25519, and environment-attribution mechanism under the `completion-verifier` key.
- The `attestations` map allows both records to coexist. The top-level `signature` field is a compatibility mirror of the most recent record.
- A gate run with `source=local` does not sign. A forced claim or close also records no certificate.
- A code or ticket-material change can make a certified certificate stale. Run the relevant gate again when its status is no longer current.

## Legacy HMAC records

Older `SIGNATURE` events can contain an HMAC-SHA256 hex value and a key fingerprint instead of a DSSE envelope. These events remain readable as append-only history. A legacy HMAC record filed as `plan-review` or `completion-verifier` now returns `unknown_scheme` and cannot certify a current gated operation. Run the relevant gate again to issue an operation certificate.

The generic HMAC helpers in `rebar.signing`, including `compute_signature`, `verify_record`, and `signing_key`, remain supported for non-operation-certificate consumers. Current `rebar sign`, plan-review, and completion-verifier writes do not use that HMAC path. See [the migration record](migrations/hmac-opcert-removal.md) for the compatibility window and reissue procedure.

## Storage

An operation certificate is an append-only `SIGNATURE` event. It replays into the ticket's kind-keyed `attestations` map, survives compaction, and reaches other clones through the ticket branch. New records use `algorithm: "sshsig"`, carry an encoded `envelope`, and have no HMAC `signature` field. See [the event schema](event-schema.md) and [the reusable signing API](reuse-surface.md) for field and library contracts.
