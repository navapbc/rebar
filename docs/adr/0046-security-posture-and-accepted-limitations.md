# ADR 0046 — Security posture, assurance model, and accepted limitations

- **Status:** Accepted
- **Date:** 2026-07-13
- **Relates to:** ADR 0044 (asymmetric attestation substrate), ADR 0045 (authenticated
  identity), ADR 0043 (operator-attested completion evidence); epics
  `gnu-whale-ichor` / `e68d` (authenticated identity, shipped) and
  `sonic-columned-sturgeon` / `6d0d` (operation certificates, planned).

## Context

rebar's signing stack (DSSE-enveloped SSHSIG authorship attestations, the pluggable per-kind
scheme/policy registry, git-commit-ancestry key validity, and the merge-gate that re-verifies over
the merged log) is deliberately built on established supply-chain-security standards rather than
hand-rolled crypto. A benchmark of the shipped identity design and the planned operation-certificate
design against the leading, actively-maintained OSS projects and standards — **gittuf**, **TUF**,
**Sigstore**, **in-toto / SLSA**, git SSH commit-signing, **RFC 8725** (JWT BCP), and
**NIST SP 800-218 (SSDF)** — found the design **strongly aligned with current best practice**, and in
one respect (log-position key validity) independently convergent with gittuf's peer-reviewed
Reference-State-Log model (NDSS 2025 Distinguished Paper; OpenSSF Incubating).

That benchmark also surfaced a small set of points where rebar provides **less** than the maximal
posture some of those projects offer (e.g. Sigstore's public transparency log, TUF's threshold
signing). This ADR exists to record — durably and honestly — the **assurance model rebar targets**,
**what our certification does and does not mean**, and the resulting **accepted limitations**, so
that operators calibrate their trust correctly and future contributors understand these are
deliberate, bounded choices appropriate to our target, not oversights.

### Assurance target

rebar targets a **FISMA Moderate** assurance level (NIST SP 800-53 Moderate baseline). Our controls
map to that baseline as follows (illustratively — this ADR records design intent and control
alignment, **not** an Authorization to Operate or a compliance attestation):

| 800-53 family | rebar control |
|---|---|
| **AC** (Access Control) | Gerrit-authenticated, access-controlled write to `main` and to the `tickets` branch; branch protections; the merge gate. |
| **IA** (Identification & Authentication) | Authenticated contributor/bot identities (Gerrit OAuth), DCO sign-off, the authenticated-authorship identity model (ADR 0045). |
| **AU** (Audit & Accountability) | The event-sourced, append-only, replicated store; signed attestations; the merge-gate verdict record. |
| **SI-7** (Software/Information Integrity) | DSSE-PAE signing over exact bytes; fail-closed per-kind verification; merge-gate re-verification over the merged log. |
| **SR** (Supply Chain Risk Management) | in-toto Statement attestations; subject/state binding; the planned operation-certificate provenance. |

### What certification means (and does not)

Under this model, a **certification is issued from a single secure, controlled environment**
authenticated via Gerrit, which is our **source of certification**. The meaning of a certification is
precise and bounded:

> A certification attests that **the defined process was followed and no blocking issues were
> encountered** by the gate, as evaluated in the controlled environment against the authoritative
> state that environment fetched.

A certification is therefore an **assurance that the process ran and passed** — it is **not** an
assertion that the underlying work is correct, complete, or free of latent defects, and it is not a
substitute for human review. This is the same scoping every provenance system draws (SLSA states it
explicitly: provenance proves *how* an artifact was produced, not that its inputs were good), and it
is the correct reading of rebar's own gates — the completion verifier attests criteria were
demonstrably met by the implementation, not that the design is optimal.

## Decision

Record the assurance model above, and the following as **accepted limitations** of the current
system. Each is stated with (a) the bounding control or design property that makes it acceptable at
our target, (b) the authority tier it applies to, and (c) the tracked deferred-hardening item for any
higher-assurance future work. None of these is an open, unmitigated threat.

### L1 — Key-validity integrity rests on an access control, not a standalone anti-rollback primitive

**Limitation.** Key validity is decided by position in the tickets-branch commit graph
(`added_at`/`revoked_at` positions resolved with `git merge-base --is-ancestor`; ADR 0045). rebar does
**not** additionally carry an explicit monotonic anti-rollback counter of the kind TUF hard-codes
(strict N+1 root versioning) or that gittuf added after its rollback CVE (CVE-2026-44544).

**Why this is acceptable / how it is bounded.** The model already defeats the direct attack it is
designed against: a **revoked key cannot sign new work**, because a new event sits at the branch tip
and therefore *descends* the revocation commit, so that key's `revoked_at` commit is an ancestor of
the event and the key is excluded (`verify_authorship_at_commit`, with intra-commit position
refinement for same-commit ordering). The residual assumption — that the commit graph is
append-only and not adversarially rewritten — is discharged by an **access control**: write to the
`tickets` branch is Gerrit-authenticated and confined to the controlled environment, and an attacker
able to force-push/rewrite that history has already defeated a higher-level control (AC) than the
signing layer. At the FISMA Moderate target with a single controlled certification environment, an
access-controlled append-only log is a standard and sufficient integrity basis.

**Authority tier.** All kinds (authorship and operation-certificate).

**Deferred hardening (tracked).** `2422` / `mucky-tweedy-archerfish` — a regression test that a
revoked-then-re-introduced key at a later ancestry position fails, a documented tickets-branch-rewrite
threat analysis, and an evaluation of an explicit monotonic guard as defense-in-depth.

### L2 — No independent transparency log; detection relies on the append-only store and environment audit

**Limitation.** rebar has no public, independently-witnessed transparency log (Sigstore's Rekor),
so there is no external monitor that could *detect* misuse of a signing key by watching a global log.

**Why this is acceptable / how it is bounded.** rebar's posture is **prevention plus audit**, not
external detection: pinned per-kind trust roots, fail-closed verification, an append-only replicated
event log, and certification confined to a single controlled, Gerrit-authenticated environment whose
access is itself audited (AU/AC). Transparency logs earn their keep chiefly in **open,
lower-trust, multi-party** ecosystems (public package registries) where you cannot assume the store
is authoritative; that is not our target's trust model. The merged log provides append-only ordering
within the repository.

**Authority tier.** Primarily the high-authority operation-certificate (the low-authority authorship
kind is unaffected in practice).

**Deferred hardening (tracked).** `4e1d` / `halfdazed-monochrome-milksnake` — a bundled
inclusion-proof or KEY_ADD monitoring ("Rekor-lite") if a higher-trust, multi-party deployment is
targeted later.

### L3 — Trust-on-first-use (TOFU) at genesis for in-band authorship keys

**Limitation.** An identity's first public key is trusted on first use; every subsequent add/revoke
must be signed by a currently-valid key (a signed rotation chain, ADR 0045). A malicious or MITM'd
*first* key would therefore be trusted thereafter.

**Why this is acceptable / how it is bounded.** TOFU is applied only to the **low-authority**
authorship kind, whose keys live in-band on the always-readable store by design; it is the same trust
bootstrap SSH itself and TUF's out-of-band initial root use. Its authority is deliberately low —
authorship attribution, not gate certification — and it is anchored to the tamper-evident commit DAG.
The **high-authority** operation-certificate deliberately does **not** use TOFU: its trust root is
pinned **out-of-band** (a Gerrit-protected / CI config path, never the auto-pushed tickets branch).

**Authority tier.** Low-authority authorship only.

**Deferred hardening.** None planned; genesis-key attestation via an out-of-band channel is a possible
future option but is not required at the target. (Covered by the docs note in this ADR.)

### L4 — Single controlled certification environment; no threshold / multi-party signing

**Limitation.** Certification is produced by a single secure, controlled environment signing with one
environment key — there is no threshold or multi-party (M-of-N) signing of the kind TUF and gittuf
use for high-authority roots.

**Why this is acceptable / how it is bounded.** This is **by design** for the target: a single,
hardened, access-controlled certification environment authenticated via Gerrit *is* our source of
certification and authorization boundary — analogous to SLSA Build L3's single trusted control plane
whose signing key is inaccessible to the workloads it certifies. FISMA Moderate does not require
multi-party signing; a controlled environment with least-privilege key custody (KMS/secrets manager,
per the op-cert plan) meets the bar. Threshold signing addresses a higher-adversary model (tolerating
compromise of one root key) that exceeds this target.

**Authority tier.** High-authority operation-certificate environment key.

**Deferred hardening (tracked).** `5100` / `marked-repulsive-ocelot` — threshold signing for
required-environment keys, or (minimum) a documented key-compromise/rotation runbook, if a
higher-adversary model is targeted later.

### L5 — Certification attests process, not correctness (definitional scope, restated for operators)

**Limitation.** A valid certification proves the process ran in the controlled environment against
authoritative state and encountered no blocking issues; it does **not** prove the underlying work is
correct or defect-free.

**Why this is acceptable.** This is not a weakness but the **defined meaning** of certification (see
Context). It is called out as a limitation only so that operators do not over-read a green
certification as a correctness or quality guarantee — the same caveat SLSA, GitHub artifact
attestations, and git commit-signing ("Verified is not a substitute for review") all make explicit.
Correctness assurance comes from the separate review gates (plan-review, code-review, completion
verifier) and human review, not from the signature.

**Authority tier.** All kinds.

**Deferred hardening.** None — this is a scope clarification, permanent by design.

## Consequences

- rebar's security assumptions and their bounds are now recorded in one reviewable place; the meaning
  of a certification is unambiguous, so downstream tooling and operators calibrate trust correctly.
- Publishing these limitations *strengthens* the alignment story rather than weakening it: openly
  documented, mitigated limitations with a stated assurance target is exactly what NIST SSDF and
  SLSA/OpenSSF guidance call for, and what gittuf/TUF/Sigstore themselves do.
- Each limitation carries a bounding control and, where a higher-assurance option exists, a tracked
  deferred-hardening ticket — so "accepted for now" stays revisitable and does not silently calcify
  into "won't fix." Revisiting any of L1–L4 is the trigger to promote its idea ticket.
- The higher-adversary options (transparency log, threshold signing, explicit anti-rollback counter)
  remain available if the target moves above FISMA Moderate or toward an open multi-party deployment;
  none requires a redesign of the substrate, only additive work.

## Prior art / grounding

- **gittuf** — Reference State Log, log-position policy/key validity, deliberate omission of TUF
  wall-clock expiry and the timestamp role; CVE-2026-44544 (policy rollback, fixed by a monotonic
  counter in v0.14.0). Design doc + NDSS 2025 paper. *(When citing formally, pin the specific
  `docs/design-document.md` commit SHA — the doc evolves.)*
- **TUF** — spec v1.0.31: role/key model, threshold signing, signed-root rotation with strict N+1
  versioning, wall-clock `expires` and the freeze-attack it defends.
- **Sigstore** — cosign/gitsign/Fulcio/Rekor; the transparency-log detection property; keyless OIDC
  identity binding; bundled inclusion proofs for offline verification.
- **DSSE** — protocol v1.0.0: PAE over exact bytes, MUST-not-re-serialize, `keyid` is an
  unauthenticated hint (MUST NOT drive security decisions).
- **in-toto / SLSA** — Statement v1 subject/digest binding; SLSA Build L3 trusted control plane;
  "provenance ≠ correctness".
- **RFC 8725 (JWT BCP)** — never trust in-band algorithm selection; bind keys to a fixed algorithm
  (rebar's per-kind pinned scheme table is the structural form of this).
- **NIST SP 800-218 (SSDF)** / **SP 800-53 Moderate baseline** — the assurance target and control
  families mapped above.
