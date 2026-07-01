# ADR 0015: Rewire the Gerrit review-bot voter to the four-pass code review

- **Status:** Accepted
- **Context:** Epic *Agentic code-review capability* (`b744`), WS6 (`4119`). Builds on the four-pass
  gate (WS1–WS5) and on epic `d251`'s shipped review-bot (S4b), whose seam this reimplements.
- **Supersedes (impl only):** the single-pass implementation of `review_bot.adapter` from d251's
  ADR `0013-llm-review-label` "proven pipe". The label/vote contract in 0013 is unchanged.

## Context
`d251` shipped the complete Gerrit review-bot — the receiver (`app.py`), the voter (`voter.py`),
the Gerrit REST client (`gerrit_client.py`), and the verdict→label **seam**
`adapter.code_review_decision(diff_text, repo_root, ref) → {decision, message, findings}`. As a
deliberate "proven pipe," that seam was implemented over the OLD single-pass `rebar.llm.review_code`
+ a blocking-severity heuristic, and `adapter.py`'s docstring reserved the swap: *"b744-WS6 will
reimplement this same signature over `gate_dispatch.produce_code_review_verdict`."*

## Decisions

### 1. The seam is reimplemented over `produce_code_review_verdict`, drop-in
`code_review_decision` now calls the four-pass gate (`produce_code_review_verdict`) and maps its
typed verdict to the receiver's `{decision, message, findings}` (plus an additive `coverage_gap`
flag). The signature + return shape are unchanged, so `voter.py` / `app.py` / the value mapping in
`ReceiverConfig` are untouched. The four-pass gate's own deterministic **Pass-3** blocker decides
PASS vs BLOCK (via `criteria_routing.json` thresholds), so the adapter no longer applies the
`ReceiverConfig.blocking_severities` severity heuristic — that knob is now vestigial for this path.

### 2. Force-enable: voter activation is the authoritative gate
The code-review gate is OFF by default (`verify.enable_code_review`, read from the *reviewed
repo's* config, which defaults false). A voter that respected that flag would fail-closed on every
change. So `produce_code_review_verdict` gains an `enabled: bool | None = None` override (None =
read config; the adapter passes `enabled=True`). Rationale: the receiver only reviews a project
once it is deliberately deployed + configured, so the per-repo flag is a redundant second gate in
this context. Defense-in-depth: an inert-disabled verdict (`coverage.enabled == false`) is still
mapped to BLOCK, never a submittable PASS.

### 3. Fail-closed vote mapping + a distinct coverage-gap message
PASS-with-full-coverage → PASS; everything else → BLOCK: a real blocking finding, an INDETERMINATE
(LLM outage), a fail-closed security-scanner abstain (WS5), an inert-disabled verdict, or ANY
exception. The receiver's voter casts `llm_review_max_value` / `llm_review_block_value` from the
decision — unchanged. The distinction between an INFRA veto and a CODE veto lives in the vote
**message**: its first line is a machine-parseable tag —
`[LLM-Review: PASS]`, `[LLM-Review: BLOCK — finding]`, or
`[LLM-Review: BLOCK — coverage-gap (<gate-disabled|llm-unavailable|scanner|review-error>)]`. The
sub-reason is derived deterministically from `verdict.coverage`. A security-scanner MATCH
(`reason == 'detector-finding'`) is a real finding, NOT a coverage gap.

## Consequences
- Gerrit's `LLM-Review` vote is now driven by the full four-pass review (overlays + Pass-2 verify
  + deterministic Pass-3 + WS5 fail-closed detectors), not a single-pass heuristic — with no change
  to the d251 receiver/voter/label contract.
- Operators can distinguish an infrastructure `-1` (re-run when healthy) from a real-finding `-1`
  by the message tag.
- `ReceiverConfig.blocking_severities` is retained for back-compat but unused by this adapter path;
  a follow-on may remove it once no other consumer reads it.
- The receiver now depends (at review time) on the `[agents]` extra + the four-pass gate's runtime
  scanners (gitleaks/opengrep, per ADR 0012) — imported lazily, fail-closed on absence.
