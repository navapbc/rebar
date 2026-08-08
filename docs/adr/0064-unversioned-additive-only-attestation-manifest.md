# ADR 0064 — The plan-review attestation manifest is unversioned and additive-only

- **Status:** Accepted (epic `b5bc`; ticket `ad16`)
- **Date:** 2026-08-08

## Context

A signed plan-review attestation carries a **manifest**: a sorted list of prefixed text lines
built by `plan_review/manifest.py` (`build_manifest`) — the criteria-registry version stamp
(`regver:`), the gate-code version/SHA (`rebar-version`), per-path code-drift dependency hashes
(`dep <sha256> <path>`), the plan-material pins (`plan-material-pin: <role> <id> <fp16>`),
per-component material fingerprints (`material-part:`), the disabled-built-ins set
(`disabled_builtins:`), review-phase/priority-floor metadata, and file-scope. The manifest is
what a later **claim gate** re-derives and compares against the signed copy to decide whether a
prior verdict may be reused.

The manifest has **no format version field**. New generations of the gate keep adding new line
kinds (`material-part:` for bug `94a3`, `disabled_builtins:` for story `08af`, `regver:` /
`refreshed-from:` for the drift-refresh work, the `verified-at-sha` pin, review-phase metadata).
Because live attestations are signed over the **exact bytes** of the manifest, the format cannot
be changed in a way that alters the bytes an already-signed manifest would produce — that would
invalidate every certificate in the field.

## Decision

Treat the manifest as an **unversioned, additive-only, byte-stable** format. The invariant is:

- **New line kinds are APPENDED, never inserted into or reordered within the existing set**, and
  each new kind is **emitted only when it has content** (e.g. `disabled_builtins:` is written only
  when the overlay disabled something; `material-part:` only when parts exist; `activated`/
  `disabled` registry-stamp inputs are folded in only when non-empty). A repo/run that would not
  populate the new kind produces a manifest that is **byte-identical** to the pre-change one, so
  attestations signed before the change stay valid with **zero churn**.
- **Readers ignore unknown lines.** Each field-parser (`manifest_material`, `manifest_pins`,
  `manifest_regver`, `manifest_rebar_version`, …) matches only its own prefix and skips the rest,
  so an older verifier reading a newer manifest silently ignores kinds it does not understand
  rather than failing. A newer verifier reading an older manifest sees the absent kind as "not
  present" (e.g. `manifest_regver` → `None` for a pre-stamp manifest).
- The only line kind that is strictly validated is the **plan-material pin** (`manifest_pins`
  raises `ManifestFormatError` on a malformed record and `build_manifest` self-checks via
  `manifest_pins(lines)` before returning) — so the gate never mints a manifest its own strict
  reader would later reject.

This is why there is deliberately **no schema_version on the manifest**: additivity + prefix-
scoped parsing + emit-only-when-non-empty give forward/backward compatibility without a version
gate, and keep the byte-image stable for existing signatures.

## Consequences

- Adding a new manifest field is safe **iff** it is appended and emitted only when non-empty; any
  change that reorders, renames, or unconditionally emits an existing kind is a byte-break that
  invalidates live attestations and must be treated as a signing-format migration, not a tweak.
- The per-line "Additive — byte-identical to the pre-X manifest — an older verifier ignores it"
  invariants in `manifest.py` are the load-bearing content of this contract; they stay in the
  source as CURRENT-behaviour invariants next to each field, and the module docstring cites this
  ADR as their fuller home.
- This contract is distinct from the byte-exact **material recomputation** grandfather (bug
  `96d1`, in `attest.py`/`pass1.py`): that governs how a *material hash* is recomputed to match a
  legacy signature; this ADR governs the *manifest line format*. Neither may be converged into the
  other.

## Alternatives rejected

- **Add a `manifest-version:` line and branch readers on it.** A version field cannot retroactively
  describe already-signed manifests (they carry no version), so readers would still need the
  "absent ⇒ legacy" fallback — the version field buys nothing the additive/ignore-unknown rule
  does not already give, while inviting non-additive changes that break existing signatures.
- **Rewrite the manifest to a structured (JSON) blob.** Changes the signed byte-image for every
  existing attestation and throws away the append-only compatibility property; rejected as a
  gratuitous invalidation of the field.
