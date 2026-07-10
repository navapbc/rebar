# ADR 0041 — Sanitization of LLM-failure diagnostics

- **Status:** Accepted (story `authorial-hated-blackbear`, epic jira-reb-687)
- **Date:** 2026-07-10

## Context

When an LLM gate degrades, rebar records a **diagnostic** — the machine-readable
detail of *why* the call failed — so an operator can later surface and triage it
(persisted on the verdict as `coverage.diagnostic`, written to the session log, and
returned from the MCP tools on failure). That diagnostic is derived from provider
exceptions, HTTP error bodies, request metadata, and headers — all of which can
carry **secrets and PII**: `Authorization: Bearer …` / `x-api-key` headers,
`sk-ant-…` keys, request payloads with user data, emails, long hex/base64 tokens.

These diagnostics are **durable and shared**: the session log and the verdict are
committed to the `tickets` branch and pushed to the sync remote on every write. A
raw diagnostic would leak a key or PII into shared, replicated history. So the
diagnostic MUST be sanitized at the point of construction, before it is ever
persisted or returned.

## Allowlist + redaction rules

Sanitization (`rebar.llm.failure.sanitize_diagnostic`) is **allowlist-first, then
redact**:

1. **Field allowlist.** Only a fixed set of structural keys is carried through
   (e.g. `type`, `status_code`, `finish_reason`, `model`, `provider`, `attempts`,
   `trace_id`, a bounded `message`). Anything not on the allowlist is dropped
   entirely — an unknown provider field never rides along by default.
2. **Value redaction** on the surviving string values, for the classes that can
   appear even in allowlisted free-text (e.g. a `message`):
   - `Authorization` / `x-api-key` / bearer tokens → redacted marker
   - `sk-ant-…` / provider API keys → redacted
   - long hex / base64 runs (opaque tokens) → redacted
   - email addresses → redacted
3. **Bounded size.** The `message` is truncated so a diagnostic can never balloon
   the session log / verdict.

The result is a small, structural, secret-free dict — enough to classify and
triage, safe to commit and replicate.

## Threat model — what must never leak

The sanitizer is the trust boundary for the durable, shared diagnostic. It MUST
guarantee that none of the following ever reach `coverage.diagnostic`, the session
log, or an MCP result:

- **Credentials:** API keys (`sk-ant-…`), bearer tokens, `Authorization` /
  `x-api-key` header values, signing keys.
- **PII:** email addresses (and, by the allowlist, any un-modelled user-content
  field — it is dropped, not merely pattern-scrubbed).
- **Opaque secrets:** long hex / base64 tokens that could be session/credential
  material.

Allowlist-first is the load-bearing choice: a redaction-only (denylist) approach
would leak any secret shaped in a way the patterns did not anticipate; dropping
everything not explicitly modelled fails **closed**.

## Test coverage

- Unit tests (`tests/unit/test_llm_failure_classifier.py`) assert each redaction
  class (auth header, bearer, `sk-ant-`, hex, base64, email) is scrubbed and that
  non-allowlisted fields are dropped.
- The classifier is **total and never raises** (a sanitizer failure must not mask
  the underlying error), covered by the classifier's totality tests.
- The degrade-site plumbing (session-log write, MCP structured return) passes the
  *already-sanitized* dict, verified by the story's degrade-path tests — sanitize
  happens once, at construction, not at each sink.
