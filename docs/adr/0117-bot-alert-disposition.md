# ADR 0117 — Bot alert recovery closes use attested dispositions, not force

**Status:** Accepted
**Date:** 2026-09-08

## Context

The dependency advisory canary, reconcile heartbeat canary, and bridge binding-drift canary all file
bug tickets for externally observed conditions. When the detector later observes recovery, those
alert tickets do not have implementation acceptance criteria for a completion verifier to prove: the
evidence is the detector's current clear verdict plus the provenance showing which detector owns the
alert.

Those lanes previously encoded that fact by closing their alert tickets with an automated
`--force=<reason>` bypass. That made the scripts use the same escape hatch reserved for human
judgement and left the close without the stronger disposition signal.

## Decision

Bot-alert recovery closes must use the existing close-class vocabulary instead of automated force:

- close with `--class env_integration` and a `--reason` describing the observed recovery;
- never pass `--force` from the automated alert lanes;
- allow the completion-verification bypass only when the ticket's immutable `detected_by`
  provenance is one of exactly `dependency-advisory-canary`, `heartbeat-canary`, or
  `binding-drift-canary`;
- refuse `--class env_integration` as a disposition for tickets without that provenance.

The core close precheck owns this boundary. Script convention alone is not sufficient because a
regression in one caller would silently reintroduce a gate bypass or a general-purpose completion
escape.

## Rejected alternatives

A new ticket type was rejected because alert tickets are still bugs in the tracker: they represent a
red operational condition that should be deduplicated, commented on, prioritized, and closed through
the same bug lifecycle as other canary findings.

A tag-scoped bypass was rejected because tags are mutable classification, while `detected_by` is
creation provenance from the alert lane. The provenance more directly answers whether the same
detector that opened the alert is allowed to close it as recovered.

A new detected-by-specific close primitive was rejected because it would duplicate the existing
bounded close-class semantics. `env_integration` already communicates that the observed problem was
in the environment/integration lane; the missing piece was a narrow provenance guard.

## Consequences

The close event records a bounded class and the recovery reason, while the ticket retains its
alert-lane provenance. Ordinary task or bug tickets cannot use `--class env_integration` to skip
completion verification. Adding another bot-alert lane requires updating the allowlist and tests in
the same change.
