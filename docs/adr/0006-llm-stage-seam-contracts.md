# ADR 0006: Enforceable, testable contracts at LLM-workflow stage seams

- **Status:** Accepted
- **Context:** Epic *codebase-health remediation* (`5ca8`), story *Enforceable, testable
  contracts at LLM-workflow stage seams (kill the silent-degrade class)*
  (`drag-gripe-brake` / `91e7-7159-8af1-4d4d`); discovered from the prompted-structured
  verifier bug (`2f18-9294-7be8-4eff`, PR #74) and the `no_repo_root` code-root drop
  (PR #71).

## Context

Two gate defects this cycle shared one root pattern: **a load-bearing contract between
pipeline stages was implicit and only weakly enforced**, so a break degraded *silently*
instead of failing loudly, and the tests could not catch it without brittle
grep/glob/substring heuristics.

- **The verify→decide shape contract.** Pass-2 emits one `verification` per finding,
  keyed by an integer `index`; Pass-3 consumes them. The Pydantic boundary used the
  default `extra='ignore'`, so a divergent verifier shape — a `findings` wrapper instead
  of `verifications`, or a per-item `attributes` instead of `severity_attributes` —
  validated to an EMPTY-but-valid object. Every finding then went `no-verification` and
  the review came back **misleadingly green**. The break was invisible until the symptom
  (all findings unverified) was noticed downstream.
- **The code read-root contract.** Which snapshot a stage reads was threaded ad-hoc
  across five hops and silently became `None`, making the det-floor P2 abstain
  `no_repo_root` — a review running blind to the code while still reporting PASS.
- **Brittle self-checks.** The module-size gate is a shell `awk`/`comm`/`grep`
  one-directional check with no Python function a unit test exercises; a literal
  `</content>` paste-artifact line sat in the allowlist undetected because `comm -23`
  only flags over-cap files *missing* from the allowlist, never a stale/garbage entry.

We want STRONG, EXPLICIT, TESTABLE contracts at the seams — so a break is a loud,
deterministic test failure (and a loud runtime signal), not a silent degrade. This ADR
records the OSS prior art surveyed and the patterns adopted.

## Prior art

How actively-maintained OSS LLM/agent frameworks make multi-stage pipeline and
structured-output contracts **explicit** (a declared schema is the contract),
**enforceable / loud** (a violation raises or retries, never silently degrades), and
**testable without string heuristics** (deterministic tests over typed/structured values).
All projects below were verified actively maintained as of mid-2026.

- **Pydantic AI** — `output_type` + `@agent.output_validator` + `ModelRetry`. Schema
  validation then custom validators; a validator raises `ModelRetry` to re-ask (default 1
  retry), and on exhaustion raises `UnexpectedModelBehavior` — never a silent degrade.
  Extra keys: Pydantic-governed; default `extra='ignore'` (the silent-drop) vs
  `ConfigDict(extra='forbid')` to raise. Tested with `TestModel`/`FunctionModel` +
  `capture_run_messages()` asserting on typed message parts, not substrings.
  ([output](https://pydantic.dev/docs/ai/core-concepts/output/),
  [agent/retries](https://pydantic.dev/docs/ai/core-concepts/agent/),
  [testing](https://pydantic.dev/docs/ai/guides/testing/))
- **Instructor** (`567-labs/instructor`) — `response_model=` + reask-on-`ValidationError`
  loop (Tenacity) + Pydantic validators. "The error message itself becomes the
  self-correction prompt"; on exhaustion raises `InstructorRetryException` with full
  forensic context. `extra='forbid'` makes the contract closed-world and composes with
  the reask loop. Deterministic core tests (`tests/core/`) assert on the reconstructed
  message history, no LLM, no substring matching.
  ([reask](https://python.useinstructor.com/concepts/reask_validation/),
  [retrying](https://python.useinstructor.com/concepts/retrying/))
- **Outlines** / **Guidance** — constrained decoding (FSM/CFG + per-step logit masking):
  an out-of-schema key is *structurally unemittable* (`additionalProperties: false`
  honored). The guarantee is structural-only and needs logit access. Tested model-free by
  asserting the compiled regex / the exact forbidden byte (`to_regex(...) == ...`;
  `check_match_failure(...)`).
  ([outlines](https://github.com/dottxt-ai/outlines),
  [guidance JSON tests](https://github.com/guidance-ai/guidance/tree/main/tests/unit/library/json))
- **DSPy** — typed Signatures (always-on contract) enforced at the adapter parse layer:
  exact field-set equality (`fields.keys() != signature.output_fields.keys()` →
  `AdapterParseError`); per-field type mismatch raises. (`dspy.Assert`/`Suggest`
  backtracking is deprecated as of 2.6 → `Refine`/`BestOfN`.) Tested with `DummyLM` and
  `pytest.raises(AdapterParseError)`.
  ([signatures](https://dspy.ai/learn/programming/signatures/),
  [adapter](https://github.com/stanfordnlp/dspy/blob/main/dspy/adapters/chat_adapter.py))
- **BAML** — schema-first typed LLM functions + Schema-Aligned Parsing, with an explicit
  two-tier dial: `@assert` (raises `BamlValidationError`, stops) vs `@check` (non-fatal,
  *evaluated even on failure* and returned for inspection). Tested via first-class `test`
  blocks with `@@assert`/`@@check`.
  ([SAP](https://boundaryml.com/blog/schema-aligned-parsing),
  [checks/asserts](https://docs.boundaryml.com/guide/baml-advanced/checks-and-asserts))
- **Guardrails AI** — `Guard` + Validators + per-clause `on_fail` actions: `EXCEPTION`
  (raise) / `REASK` vs `NOOP`-but-logged / `FIX` / `FILTER`. The clearest prior art for an
  explicit *prod-log-loud vs strict-raise* dial selected per contract clause.
  ([on_fail actions](https://www.guardrailsai.com/docs/concepts/validator_on_fail_actions))
- **LangGraph** — typed State + channels + reducers: an update naming no known channel, or
  two unreduced writes to one key, raise `InvalidUpdateError` (closed channel set); a
  Pydantic `BaseModel` state schema upgrades to runtime value validation.
  ([graph API](https://docs.langchain.com/oss/python/langgraph/graph-api))
- **Marvin** — Pydantic-typed AI functions (delegates to Pydantic AI); the `result_type`
  is the contract, loud-by-default via inherited Pydantic validation.

## Decision

Adopt the prior art's convergent patterns at our seams, **expand-contract style** — close
the *testing* gap and add loud runtime *observability* now, without changing production
gate outcomes; leave the strict live-flip as a documented one-liner for later.

The patterns (each backed by ≥1 project):

- **P1 — REJECT, don't ignore.** A closed-world output schema (`extra='forbid'` +
  required `index`) so a divergent shape raises instead of coercing to empty. *(Pydantic
  AI, Instructor, DSPy; structurally, Outlines/Guidance.)*
- **P2 — Fail loud at the seam.** A contract violation becomes a raise/log, never a
  silent empty/default set. *(Pydantic AI, Instructor, DSPy, BAML.)*
- **P3 — A distinct, counted contract-violation report** for recoverable shape problems —
  BAML's `@check` / Guardrails' `on_fail` "collected, surfaced, not fatal" tier — vs an
  honest "couldn't verify".
- **P5 — Test the seam deterministically** over typed/structured values or by exception
  type; never grep/glob/substring on model prose. *(Universal.)*
- **P6 — Prefer construction-time guarantees** (constrained decoding) where the contract
  is a grammar/schema and the backend supports it — with P1–P5 as the always-on net.
  (Noted as future direction; not adopted now.)

### What shipped in this story

1. **Strict verification model, test-pinned (P1).** `review_kernel.verify.verification_model(strict=True)`
   switches the whole model tree to `extra='forbid'`; a wrong wrapper key / wrong per-item
   key / missing-or-non-int `index` raises `StructuredOutputError`. The LIVE registration
   stays `strict=False` (tolerant) — the flip is a one-line change, pinned by a
   deterministic test that also documents the current tolerant behavior.
2. **One shared structural reshape seam (P2/P3).** `review_kernel.verify.reshape_verifications`
   is the SINGLE place a flat verifier list becomes the `{index: verification}` map. It
   returns the byte-identical tolerant map PLUS a violation report (malformed / duplicate /
   out-of-range indices). It replaces the duplicated inline silent-drop in BOTH the kernel
   `verify_findings` and the workflow `plan_review_decide`. In prod the report is LOGGED at
   ERROR and counted on the verdict coverage (`coverage["verification_contract_violations"]`),
   present ONLY when non-empty so a clean run's verdict stays byte-identical (attestation-safe);
   `verify_findings` additionally treats a `StructuredOutputError` from its chunk seam as a
   distinct, logged contract failure (vs a benign degrade). **No decision/verdict changes.**
3. **Read-root contract lever (P2).** `resolve_code_root(..., require=True)` raises
   `LLMConfigError` (fail-closed) when a stage that requires a root would resolve `None`.
   Opt-in (not wired live here); pinned by a test that also asserts the #71 cascade grounds
   an active snapshot.
4. **A brittle grep/glob check replaced by a structural assertion (P5).** A
   `compute_over_cap_modules()` function + a unit test asserting SET EQUALITY between the
   computed over-cap set and the allowlist (both directions) — which immediately caught and
   removed the stale `</content>` allowlist line the one-directional shell `comm -23` missed.

## Consequences

- **Production behavior is unchanged.** The live verifier parse stays tolerant; the live
  decide reason stays `no-verification`. The only runtime additions are additive
  observability (an ERROR log + a coverage counter that appears only on a real violation),
  so a clean run's verdict bytes — and its signature — are identical to before.
- **Regressions are now caught two ways.** Deterministic seam tests fail in CI if the
  contract drifts, and a runtime violation is loud (logged + counted) rather than a
  misleadingly-green review.
- **The strict flip is ready when telemetry is clean.** Flipping the live contract to
  `verification_model(strict=True)` (and threading the distinct decide reason) is a small,
  reviewed change; back-out is a one-line revert. This mirrors Guardrails' `on_fail` dial
  in time: ship the `@check`/log-loud tier now to quantify drift, then move to
  `@assert`/raise once the counters confirm the contract holds.
- **Construction-time guarantees (P6)** remain available as a stronger future first line
  where the verifier backend supports constrained/JSON-schema decoding.
