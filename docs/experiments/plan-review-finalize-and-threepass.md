# Plan-review gate — finalize round: three-pass adoption + non-DSO generalization

Closes the five "remaining to finalize before implementation" items from the validation session
(epic `5fd2-a7c2-0aec-48fa`, session log `7031-…`), adopts the **three-pass review structure** (epic
`9da1-cb98-2e94-4f2b` / log `9dba-…`) per directive, validates the criteria set on a **non-DSO**
corpus, and runs the converged plan through the gate with all overlays.

> Reproduction: harnesses under `plan-review-gate/harnesses/` (run with a venv that has `anthropic`);
> raw run data under `plan-review-gate/runs/` (`e4_*.jsonl`, `lever_ab.jsonl`, `final_gate_3pass.json`,
> `corpus_sample.json`). Criteria sets `criteria_v7.json` (registry hardening) → `criteria_v8.json`
> (three-pass-native). Not tests, not prod (see the gate README).

## 0. The headline

- **Adopted the three-pass structure** (evidence → binary-verify → deterministic-gate), **replacing
  per-criterion model-emitted severity/confidence** — the architectural change the user requested.
- **The non-DSO generalization (E4) empirically motivates it:** a single-turn, Pass-1-only reviewer
  **over-fires badly** on a held-out population (G5 FAILs 87% of tickets, E6 100%, E1 80%, E5 64%,
  T7 67%), and the **independent verification corrects the false-fails** (real ticket: `e249` E4
  FAIL→PASS, G6/G1G2 AMBIGUOUS→PASS once grounded in the actual rebar code). Unverified Pass-1
  severity *is* the over-fire; Pass-2/Pass-3 is the fix.
- **The three-pass capstone on the epic itself** is calibrated: DET floor PASS, Pass-1 → 3 findings,
  Pass-3 → 1 dropped (low-confidence) + 2 advisory + 0 blocks, all converging on a **real gap** (no
  kill-switch / rollback for the gate itself).

## 1. Registry hardening (items D, E, B) — `criteria_v7.json`, `gate_lib.py`

| item | what shipped |
|---|---|
| **D** lift sub-checks → structured `checklist[]` | all 31 descriptors now carry a `checklist[]` of binary `{key, check}` items (the Q10 follow-up; CheckEval/TICK basis). |
| **A** proportionate scrutiny → declarative `applies_at{}` | lifted out of `retune.py`'s hard-coded `LEAF_ONLY/ALL_LEVEL` into each descriptor: `levels[]` (epic/story/task), `container_only`, `suppress_types:[bug]`, `suppress_when:[test_task, mechanical_leaf]`. Sanity: epic→20 / story→24 / task→29 candidate criteria, **bug→0 (exempt)**. |
| **E** structured-output parse hardening | `gate_lib.robust_findings()` never crashes on `no_tool_use` / empty / malformed; coerces to the schema; synthesizes an INDETERMINATE entry for any missing id; retries on empty. **126/126 chunk calls `ok` in E4** (zero crashes). |
| **B** route codebase-grounded criteria to AGENT | `T10/T11` flipped 1-TURN→AGENT (IaC/migration verification wants the real repo); `G6/G1G2/E4/A1/T1/T3/T8/G3/G4` already AGENT. **COH and T9 deliberately kept 1-TURN** — see §3 (the lever A/B showed their AMBIGUOUS-on-clean is a decisiveness artifact, not a tooling gap; COH is text-internal). |

`gate_lib.py` is the reusable substrate (robust parse, declarative `applies()` filter, `base_chunk(model)
× size_factor(ticket)` chunker, deterministic overlay triggers, decisiveness-tuned SYSTEM, hardened
single-turn/agent wrappers) — a preview of the production orchestrator shape.

## 2. E4 — generalization on a NON-DSO corpus (the held-out validation round-4 lacked)

Two real, polyglot, rebar-dogfooding corpuses: **rebar itself** (Python) + **snap-oakhart-manual**
(Rails/Ruby). Sampler `corpus_sample.py` → 19 tickets (11 rebar + 8 snap) across epic/story/task/bug,
several with children. (Snap has no story-type tickets — itself a population difference.)

**Single-turn suite (Pass-1-only) over-fires on the held-out population** (`e4_suite.jsonl`,
`analyze_e4.py`): per-criterion FAIL-rate across tickets — G5 87%, E6 100%, E1 80%, T7 67%, E5 64%,
E2/T4/T5e ~53%, T9 45%. On the same population the **agentic A/B** (`e4_agentic.jsonl`) shows the
verification pass *correcting* these: `e249` (NDJSON import) E4 **FAIL→PASS**, G6 **AMBIGUOUS→PASS**,
G1G2 **AMBIGUOUS→PASS** once the agent greps the real code; `05ac` (delete-bash-dispatcher, mid-strangler)
legitimately stays AMBIGUOUS. → The over-fire is **unverified Pass-1 severity**, which is exactly what the
three-pass verify+aggregate removes. This is the empirical case for the architecture, on held-out data.

**E5 retune (item A)** before/after on the same tickets (`e4_e5ab.jsonl`): v6 fired on 11/11 tickets
(8 FAIL, 3 AMBIGUOUS); v7-retuned fired on 9/11 (7 FAIL). Two snap tasks correctly flipped FAIL→PASS.
The retune reduces over-fire; the three-pass verification is the durable fix (E5 can't be trusted as a
raw Pass-1 verdict).

## 3. Overlay-trigger precision (E3) + the decisiveness-lever A/B

**E3 — deterministic vs LLM router** (`e4_trigger.jsonl`): the deterministic keyword triggers over-fire
on *plans* (which describe intent, not files). T11 `migration` fired on 2/19 — **both the bash→Python
strangler, not data-migration**; T12 `deploy`/`rollback` fired on 7/19; the **LLM router fired 0 on both
(correct)**. T8 det 13/19 vs llm 4 (confirms round-4). → **T10/T11/T12 are now LLM-routed for plan
review** in `criteria_v8.json` (on a code-review *diff* the 9da1 file-glob catalog triggers are
high-precision; on a plan there is no diff).

**Decisiveness-lever A/B** (`lever_ab.py`, seeded BAD/GOOD × v6-SYSTEM vs v7-SYSTEM): recall
**23/24 → 24/24**; **G6** clean-case **AMBIGUOUS:3 → PASS:3** (the lever fixes G6's hedge with no tools);
**COH, E5, T11, T12, A1 perfect at single-turn** both systems (BAD→FAIL, GOOD→PASS) — so **COH needs no
AGENT tier**, correcting the earlier STATUS list that named COH/T9 for AGENT routing. T9 still hedges
AMBIGUOUS on the clean (terse) plan — defensible (lifecycle under-specified), recall perfect.

## 4. Three-pass adoption (the directive) — `criteria_v8.json`, `three_pass.py`

Adopted from epic `9da1` + log `9dba`. The model emits **no holistic severity/confidence** anywhere in
the decision path.

- **Pass 1 — find:** `{finding, criteria[], evidence[] (flexible — quote / section / ABSENCE rationale /
  code citation), scenarios[], impact}`, no severity/confidence (`PASS1_TOOL=emit_findings`).
- **Pass 2 — verify (separate, independent context):** per finding, severity ATTRIBUTES
  `{prod_impact, debt_impact, blast_radius, likelihood, reversibility}` + typed BINARY sub-answers
  `{yes|no|insufficient}`; rules = atomic, independent (finding presented as a *claim to test*),
  verdict-with-citation not -with-fix, `insufficient` allowed; **agentic** (repo tools) for
  codebase-grounded findings, single-turn otherwise.
- **Pass 3 — decide (deterministic):** veto = cited-reference-accuracy *only when a citation is present*
  (no-op for non-citable absence findings — the plan-review specialization); confidence = graded fraction
  of the binary answers; severity = computed from the attributes; decision = `block | advisory | dropped`
  (drop < .5) against per-criterion `block_threshold`. **v1 = advisory-only for every LLM criterion**
  (`default_posture=advisory`); only the DET floor blocks.

`criteria_v8.json` is three-pass-native: `severity_by=pass3`, `default_posture=advisory`, `block_threshold`,
plus the v7 `checklist[]` + `applies_at`, plus the E3 routing fix. Registry-coverage guard passes.

## 5. Final gate run — the converged plan through the three-pass gate, all overlays

`final_gate_3pass.py` on epic `5fd2`, full v8 rubric (all 31 criteria forced on), Pass-1 Opus, Pass-2
agentic vs the rebar repo (`final_gate_3pass.json`):

```
DET floor : check-ac → pass (13 criteria lines) ; clarity → score 7, verdict pass
PASS 1    : 3 findings (across 6 facet-chunks of all 31 criteria)
PASS 3    : 1 dropped (conf .29) · 2 advisory · 0 block
```

All three findings converge on **T12 rollout/rollback** — and the load-bearing one is a **real gap the
five prior rounds missed**: the gate adds a *blocking change to the in-use `claim` (open→in_progress)
path* but **never specifies a kill-switch / disable-the-gate config / rollback for the gate itself**
(advisory, conf .71, severity critical). A second advisory flags rollout ordering for the
signature/sidecar scheme. The deterministic filter **dropped** the third (conf .29) — the plan already
addresses it via `--force` + the out-of-band design. This is the three-pass value in one run: a sharp,
verified, calibrated result on a well-formed plan (no block-spam), and it earned its keep by finding a
genuine omission.

**Adjudication (Joe):** the finding was an advisory **over-call** — the gate is **config-gated and
fail-open when off**, so disabling it via config IS the kill-switch / rollback (turning it off reverts
`claim` to current behavior instantly); no separate rollback mechanism is needed by design. The Pass-2
`no-existing-mitigation` check should have answered *no* (config-gating IS the mitigation) and softened it.
This is the three-pass flow working: advisory (not block) → human adjudication → dismissed. The epic's
config Success Criterion now states config-disable = the kill-switch explicitly (so the gate won't re-flag
it); the prematurely-filed follow-on `ce75` was closed resolved-by-design. Good calibration datum for the
verifier's mitigation check.

## 6. Status of the five finalize items

| # | item | status |
|---|---|---|
| 1 | retune E5 (was 9/12 over-fire) | **done** — declarative suppression + raised bar (v8); re-measured (§2). The durable fix is three-pass verification. |
| 2 | route codebase-grounded criteria to AGENT | **done, evidence-refined** — T10/T11→AGENT; COH/T9 kept 1-TURN with rationale (§3); T10/T11/T12 trigger→LLM (§3). |
| 3 | E4 generalization on a non-DSO corpus | **done** — rebar + snap; the over-fire-then-verify result (§2). |
| 4 | lift prose sub-checks → `checklist[]` | **done** — 31/31 (§1). |
| 5 | harden structured-output parsing | **done** — `robust_findings`, 126/126 ok (§1). |
| + | **three-pass adoption** (user directive) | **done** — §4–§5. |

Remaining before implementation proper: file the F-register as tickets, then build per the 9 child stories
(registry/orchestrator first). The T12 gate-found gap was adjudicated resolved-by-design (config-gating is
the kill-switch; `ce75` closed). The three-pass framework is shared with epic `9da1` — coordinate the
orchestrator so plan-review and code-review use one Pass-2/Pass-3 implementation.
