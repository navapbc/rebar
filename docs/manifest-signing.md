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

On POSIX, the op-cert signing service refuses a `REBAR_OPCERT_KEY_PATH` key file with any group or other permission bit when it composes its signer (it must be `0600` or stricter). Windows has no POSIX permission bits, so rebar skips that check there and relies on the user profile's default access control list (normally the owner, SYSTEM and Administrators). The residual risk on Windows: a key inherits its folder's ACL, `chmod` only toggles the read-only attribute, and nothing checks for extra entries such as Users or Everyone. Keep Windows signing keys in a folder whose ACL you have checked (for example with `icacls`). This is a deliberate decision, recorded on cloistered-decomposed-nymph. The signing service never signs from that file directly: it copies the key into a fresh temporary folder, and on Windows it first restricts that folder to the current user alone with `icacls`, because the folder Python creates grants OWNER RIGHTS, which Win32-OpenSSH refuses (decision recorded on plump-smelly-indianabat). If `whoami` or `icacls` fails, the service refuses to start.

The certificate principal comes from `REBAR_OPCERT_ENV_ID` when that deployment setting is present. Otherwise rebar uses the ticket store's `.env-id`. The principal attributes the certificate to the signing environment. It does not identify a person. Authorship attestations use a separate key, namespace, and trust model.

Signing requires OpenSSH 8.9 or newer. A missing or unusable `ssh-keygen` produces a signing error. Gate callers record that outcome as unsigned, and a gate that requires certification remains unsatisfied. Verification never creates a missing private key.

## Signed material and verification

The DSSE payload is an in-toto Statement that binds the ticket ID, attestation kind, material fingerprint, code commit, and full manifest. Verification accepts those bound values only after checking the SSHSIG signature against an Ed25519 public key. A copied certificate therefore cannot certify another ticket or another attestation kind.

`rebar verify-signature` verifies the DSSE envelope signed through SSHSIG. By default it checks validity only (ADR 0125): the signature must verify under the public key carried inside the SSHSIG blob, for the certificate's principal and the `rebar.opcert.v1` namespace, and the signed subject must bind the ticket and kind. The result names the signing key (`signing_key`, its SHA256 fingerprint) and the environment (`key_id`). Anyone can sign; the signing environment is not a gate unless the verifying environment asks for one.

The result reports its trust basis in `trust_basis`:

| `trust_basis` | meaning |
|---|---|
| `envelope_key` | validity only: the signer's own key, carried inside the SSHSIG blob |
| `trust_list` | the verifying environment's `verify.trusted_opcert_keys` lists this (environment, key) pair |

`envelope_key` is **self-consistent**: `ssh-keygen -Y verify` still checks signature, namespace and principal binding in full, so a forged or altered envelope is still `mismatch`. What it does not establish is that the key belongs to a *known* environment, so the basis stays visible rather than being folded silently into `certified`.

**Opt-in: a per-environment trust list.** An environment that wants to restrict signers sets `verify.trusted_opcert_keys` in its own configuration (user config, deployment environment variable `REBAR_VERIFY_TRUSTED_OPCERT_KEYS`, or CI job variables; never the repository's `rebar.toml`). Each entry is `<env_id> <key-type> <base64> [comment]`. With a list set, a valid certificate whose pair is not listed is `untrusted`. Rebar's MCP server sets its list in its deployment and so trusts only its own signatures. `verify.require_environment` (with `verify.opcert_enforce_since`, per `infra/runbooks/mcp-opcert-enforcement-flip.md`) additionally requires the certificate to name that environment and requires a trust list; without a list it refuses with `foreign_key`. `rebar verify-opcert` is the corresponding merge-gate lane. Signing never consults a trust list.

A certified signature establishes the integrity of the signed process record. It does not establish that the reviewed work is defect-free. Gate validity also checks current ticket material, code freshness, reopen state, and related-ticket pins where applicable.

## Gate behavior

- A passing plan-review records a DSSE envelope that carries an SSHSIG signature over its PAE bytes, produced with the signing environment's Ed25519 key. Its principal identifies that environment, and the certificate is stored under the `plan-review` key.
- A passing completion-verifier records the same DSSE, SSHSIG, Ed25519, and environment-attribution mechanism under the `completion-verifier` key.
- The `attestations` map allows both records to coexist. The top-level `signature` field is a compatibility mirror of the most recent record.
- A gate run with `source=local` does not sign. A forced claim or close also records no certificate.
- A code or ticket-material change can make a certified certificate stale. Run the relevant gate again when its status is no longer current.

## Legacy HMAC records

Older `SIGNATURE` events can contain an HMAC-SHA256 hex value and a key fingerprint instead of a DSSE envelope. These events remain readable as append-only history. The HMAC scheme is retired (2026-09): any record without a DSSE envelope now verifies `unsigned`, with a reason naming the retired scheme, and cannot certify a gated operation. Run the relevant gate again to issue an operation certificate.

No HMAC key custody or verifier remains: `REBAR_SIGNING_KEY` and `<tracker>/.signing-key` are no longer read, `rebar init` no longer creates the key file, and the `rebar.signing` HMAC helpers (`compute_signature`, `verify_record`, `signing_key`, `key_fingerprint`) are removed. An existing `.signing-key` file can be deleted. See [the migration record](migrations/hmac-opcert-removal.md) for the compatibility window and reissue procedure.

## Storage

An operation certificate is an append-only `SIGNATURE` event. It replays into the ticket's kind-keyed `attestations` map, survives compaction, and reaches other clones through the ticket branch. New records use `algorithm: "sshsig"`, carry an encoded `envelope`, and have no HMAC `signature` field. See [the event schema](event-schema.md) and [the reusable signing API](reuse-surface.md) for field and library contracts.
