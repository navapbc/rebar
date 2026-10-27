# Citation-grounding coverage — Layer-2 behavioral eval fixture (story 266e)

This fixture self-gates the Layer-2 LLM coverage protocol added to the `G1G2`/`E4`/`E6`
Pass-1 finders: when a plan cites a prerequisite's symbol as `<subject> [rebar:<C>]` and the
Layer-1 deterministic edge check (`det_citation`) has verified the upstream edge, the finder
must retrieve `C` via `show_ticket(C)` and credit the cited symbol **only** on affirmative
coverage. It mirrors the deterministic-lexicon self-gating precedent of
`harnesses/operator_attested_eval.py` — but because Layer 2 is an intentional LLM judgment
(not a string match), the effect is validated by a **behavioral** eval over N samples against a
predeclared threshold, not a single deterministic unit assertion.

> **Status: operator-run.** The live-LLM run is `[operator-attested]` per the ticket — the
> sample pass-rate vs threshold is recorded on ticket `266e-c8b6-0cb0-44ae`, not asserted in
> the in-session unit suite. This file is the fixed test material + protocol; it does not
> itself invoke a model.

## Predeclared gate

- **Samples:** N = 10 per plan (independent finder runs).
- **Threshold:** >= 8/10 (80%) correct decisions per plan, **both** plans must clear it.
- **Model/runner:** the project's configured plan-review finder model (`[agents]` extra).
- A run below threshold on either plan **fails** the self-gate (the protocol wording must be
  revised and re-run) — the finders must not be shipped crediting fabricated citations nor
  false-flagging genuinely-covered ones.

## Plan A — verified edge + affirmative coverage (expect: symbol CREDITED, no finding)

Setup: plan ticket `P` declares `depends_on -> C`; prerequisite `C`'s plan/file_impact creates
the cited symbol.

- `C` (prerequisite) file_impact: `src/rebar/plugins/registry.py` — "new: `PluginRegistry.register()`
  entry point for adapters".
- `P` plan excerpt:
  ```markdown
  ## Approach
  Register the new Jira adapter through `PluginRegistry.register()` [rebar:C] at import time.
  ## Acceptance Criteria
  - [ ] adapter self-registers via `PluginRegistry.register()` [rebar:C]; proof: `pytest -q tests/test_adapter_registry.py`
  ```
- **Layer 1:** edge verified (`P.depends_on(C)`).
- **Expected finder decision:** retrieve `C`, confirm `C` establishes `PluginRegistry.register()`,
  **credit** the citation — treat the symbol as EXISTING and emit **no** missing-symbol finding.

## Plan B — edge-unbacked / coverage-absent (expect: finding STANDS)

Two sub-cases; a correct decision on either is that the missing-symbol finding **stands**
(fails closed).

- **B1 (edge-unbacked):** `P` has **no** `depends_on -> C` edge and `C` does **not** `blocks -> P`.
  The plan cites `PluginRegistry.register()` [rebar:C] exactly as in Plan A.
  - **Layer 1:** `det_citation` emits an advisory "unbacked citation" issue; no credit is
    available.
  - **Expected finder decision:** the missing-symbol finding **stands** (grounded as normal).
- **B2 (coverage-absent):** `P` declares `depends_on -> C` (edge verified), but `C`'s
  plan/file_impact establishes only an **unrelated** symbol (e.g. `src/rebar/plugins/loader.py`
  — "new: `PluginLoader.discover()`"), not `PluginRegistry.register()`.
  - **Layer 1:** edge verified.
  - **Expected finder decision:** on `show_ticket(C)` the finder finds no coverage for the
    SPECIFIC cited symbol, so it does **not** credit it — the missing-symbol finding **stands**.

## Scoring

For each sample, a decision is "correct" iff it matches the Expected decision above
(credit-and-drop for Plan A; finding-stands for Plan B1/B2). Record `correct/N` per plan and
compare against the 80% threshold. Anti-bypass check: Plan B1 must never be credited (a
fabricated/edge-unbacked citation earns no credit), which is what keeps the normal
missing-symbol block in force.
