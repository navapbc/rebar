# ADR 0036 — ACLI subprocess 429 rate-limit backoff (bounded, jittered, Retry-After-aware)

**Status:** Accepted (epic adept-hedge-stain / 943f — C4)
**Date:** 2026-07-08

## Context

Every ACLI call goes through `_run_acli` (`acli_subprocess.py`), whose retry loop slept a
uniform `2s`/`4s` for *any* non-zero exit (except auth/assignee fast-aborts). Under Jira
rate-limiting (HTTP 429) that is both too aggressive (no jitter → thundering herd across a
batch) and blind to a server-provided `Retry-After`. ACLI is a **subprocess**, so a 429
surfaces only as text in stderr — the exact exit code ACLI returns for 429, and whether
`Retry-After` is emitted, are provider-dependent and could not be verified on demand (a real
rate-limit is not reliably triggerable). `_call_with_backoff` (the `urllib`-domain helper) is
dead code and unrelated to the subprocess path.

## Decision

Add **add-on** 429 handling to the live `_run_acli` loop — it augments, never replaces, the
general retry policy:

- **Detection from stderr.** `_rate_limit_backoff(attempt, stderr)` returns a delay when
  stderr contains a rate-limit marker (`429`, `too many requests`, `rate limit`), else `None`.
- **Honor `Retry-After` iff present.** A parseable `Retry-After: N` (seconds) is used directly;
  otherwise jittered exponential backoff (`2**(attempt+1) + rand[0,1)`).
- **Bounded.** Every delay is capped at `_MAX_BACKOFF_S = 60s` (a hostile/huge `Retry-After`
  cannot hang a pass), and `_MAX_ATTEMPTS` still bounds the total attempts.
- **Contract-preserving.** On a 429 attempt the rate-limit delay *replaces* the uniform sleep
  for that attempt (no double-sleep); every other non-zero exit keeps its current behavior. On
  exhaustion `_run_acli` raises exactly what it raised before — `CalledProcessError`. No new
  exception type is surfaced to the 9 call sites; `RetryExhaustedError` stays in the app-layer
  `_call_with_retry`.
- **Observability.** Rate-limit detection, Retry-After-honored vs jittered-fallback, and the
  chosen delay are logged at WARNING.

Retryable-code scope stays **ADD-ON**: narrowing retries to `{429,502,503}` would drop the
existing retry behavior for other codes and is out of scope. The non-idempotent-write timeout
guard (`AcliTimeoutError` is not a builtin `TimeoutError`) is untouched.

## Probe disposition (AC #1)

A live 429 could not be forced on demand, so the exact ACLI 429 exit code and whether ACLI
retries 429 internally remain **unverified in production**. The implementation is therefore
**defensive**: it keys off stderr markers (works regardless of the exit code) and honors
`Retry-After` only when the value is actually present. If ACLI is later found to retry 429
internally, this Python-layer backoff simply never triggers (its markers won't appear) — no
double-retry, no change needed.

## Consequences

- Rate-limited batches back off politely (jitter avoids herding; `Retry-After` is respected)
  without any change to the caller contract.
- Left untouched: dead `_call_with_backoff` (optional follow-up cleanup, not in this ticket).
