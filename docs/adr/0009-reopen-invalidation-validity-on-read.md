# ADR 0009: Reopen invalidation — validity-on-read over write-time mutation

- **Status:** Accepted
- **Context:** Epic *Additive, kind-keyed attestations* (`4702-a55c-7aeb-445a`,
  dark-acme-lumen); story 929e (head-relic-twine). Supersedes the reopen half of epic
  raze-vet-ditch (`retire_attested_pin`). Relates to ADR 0002 (code-drift invalidation).

## Context

Reopening a closed ticket must stop its **completion** attestation (and, by the same logic,
a stale **plan-review** attestation) from counting as a validated closure — otherwise a
reopened, in-progress ticket would still read as "done" to a parent's child-closure check or
a future CI gate.

The previous mechanism (`signing.retire_attested_pin`, called on every `closed → open`
transition) achieved this by **mutating the signature**: it appended a blank/`retired`
`SIGNATURE` event that cleared the single `state["signature"]` slot, so `verify_signature`
then reported `unsigned`.

Under the new **kind-keyed attestations map** (this epic), a single ticket holds *multiple*
independent attestations (plan-review + completion-verifier + future kinds). A blank clearing
event has no kind, so it cannot target one kind — clearing "the signature" would destroy
*all* of a reopened ticket's attestations, recreating the very cross-kind clobber
(grumpy-site-beard) the epic exists to fix.

## Decision

**Attestation records are immutable; reopen invalidation is computed on READ.**

- No transition mutates or clears an attestation record. `retire_attested_pin` is removed,
  along with its reopen call site in `transition.py`.
- The reducer records `state["last_reopened_at"]` = the timestamp of the most recent
  `closed → open` transition (`reducer/_processors.py:process_status`).
- `plan_review.attest.compute_validity(attestation, ticket_state, kind)` is the single place
  that decides whether an already-HMAC-certified attestation is **currently valid for its
  gate**. For every kind it rejects an attestation whose `signed_at` is at/before
  `last_reopened_at` (signed before the reopen → stale). `completion-verifier` additionally
  requires the ticket to be `closed` and its material fingerprint unchanged; `plan-review`
  additionally applies the ADR 0002 code-drift + material-fingerprint freshness.
- **Invariant:** gates (the claim gate, the child-closure check, a future CI gate) MUST call
  `compute_validity` on a certified attestation rather than trust HMAC certification alone or
  mutate the record. HMAC `certified` proves integrity + authorship; `compute_validity`
  proves current applicability.

## Consequences

- A reopened ticket keeps all its real attestation records (no clobber); they simply read as
  not-valid until the ticket is re-closed/re-reviewed (which re-signs the relevant kind with a
  fresh `signed_at`).
- `verify_signature` (HMAC) and "valid for this gate" are now distinct layers — a record can
  be `certified` yet not `valid` (e.g. after a reopen). Consumers that previously relied on
  `verify_signature → unsigned` after reopen now check `compute_validity(...).valid`.
- Re-close after reopen re-signs `completion-verifier` (new `signed_at` > `last_reopened_at`),
  so a legitimately re-closed ticket validates again with no special handling.
