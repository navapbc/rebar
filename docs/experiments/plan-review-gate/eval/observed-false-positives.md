# Observed false positives — eval-suite seed (labeled false-fires)

Real false positives the gate produced while reviewing its OWN epic (`5fd2`) this session. They are
**labeled false-fires** to SEED the standing eval suite (the `5fd2` eval-suite AC) so the Pass-2/Pass-3
filter + the criteria prompts can be calibrated to suppress them — grounded in real misfires, not synthetic
cases. **These are NOT the only FP failure modes we want to assess** — see the taxonomy below; the suite
should cover the whole taxonomy, not just these instances.

## Labeled false-fires (from the re-run)

| id | criterion | the FP | why it's a false-fire | mode | status |
|----|-----------|--------|-----------------------|------|--------|
| FP1 | T5c | flagged "no declared access level" on rebar | rebar (git-backed lib/CLI) has no "access level" concept; the reviewer imported a generic web-app requirement | domain-inappropriate-standard-import | fixed (prompt refit) |
| FP2 | T5c | flagged secrets "baked at deploy almost certainly means env-var leakage" on snap's OAuth host | speculation from plan text; the actual code uses Secrets Manager + constant-time compare — the agent (codebase-grounded) correctly dismissed it | speculative-from-text | fixed (T5c → AGENT) |
| FP3 | T5c | flagged "leakage into logged findings / eval corpus" | all review inputs already live in the repo; findings also go back to the repo → nothing private is leaked; secrets-in-repo is an UPSTREAM concern | wrong-threat-model | fixed (prompt refit) |
| FP4 | G6 | "fail-open DET + advisory-only ⇒ hollow signal; gate barely blocks" | debatable misreading of intent — the gate DOES provide blocking levers (per-criterion threshold config); thresholds are high during calibration BY DESIGN, not "hollow" | debatable-misinterpretation-of-intent | epic wording corrected; filter should have dropped it |
| FP5 | T5a/T2 | latency targets "asserted without a benchmark step" | low-impact nitpick; initial targets from UX/timeout projections + passive logging is the right approach, not an upfront benchmark | low-impact-nitpick | epic clarified; filter should have downgraded |
| FP6 | T2/E3 | flagged the cross-provider "hard requirement" as unvalidated because completion was only proven on Anthropic (OpenAI/Google billing-blocked) — on the `e6d9` workflow-engine-v2 dry-run | provider-agnosticism is the **core documented contract** of the adopted library (Pydantic AI). Requiring the plan to end-to-end re-prove a maintained dependency's headline guarantee is "testing code that isn't ours." Adoption of the library IS the mitigation. | library-guaranteed-capability | filter SHOULD have dropped (Pass-2 `no_existing_mitigation`=YES); verifier missed it — tuning proposed below |
| FP7 | A1/E4/G1G2 | flagged NIH / "rebuild vs extend" because a near-complete v1 workflow pkg (`src/rebar/llm/workflow/`) already exists — on the `e6d9` dry-run | the v1 pkg was built as a **proof-of-concept / experimental validation — a proven reference, not production code**. Rebuilding it for production is the *intended* lifecycle (we encourage experimentation during brainstorming). Flagging NIH against a designated probe/reference is a false-fire. | rebuild-of-designated-experiment-reference | A1 was already dropped (conf .29) here for a different reason; needs a reliable carve-out so it never fires (or fires only as a confirm-the-framing coaching note) |

> **Crucial precision (do NOT over-suppress).** FP6 is an FP because provider-agnosticism is the library's
> *guaranteed contract*. It must be distinguished from the SEPARATE, LEGITIMATE finding the same dry-run
> raised (T3/T1): whether Pydantic AI's `NativeOutput` works for **Anthropic specifically** — a *maturing
> feature*, not a core contract. That one was a true flag, and a live experiment resolved it (NativeOutput
> sends Anthropic's `output_config`, no tools; works in pydantic_ai 1.107.0 / anthropic 0.111.0). The tuning
> must keep flagging "is this specific, newer, possibly-immature integration point proven?" while dropping
> "re-prove the library's headline guarantee." Library-CONTRACT → drop; library-FEATURE-MATURITY → keep.

## FP failure-mode taxonomy (assess ALL of these — the above are only instances)

1. **domain-inappropriate-standard-import** — applying a standard/requirement the application's domain doesn't have (FP1).
2. **speculative-from-text** — "almost certainly means X" reasoning without grounding in the actual implementation (FP2).
3. **wrong-threat-model / wrong-frame** — a concern that doesn't apply given the system's actual model (FP3: data already in the repo).
4. **debatable-misinterpretation-of-intent** — a contestable reading of what the plan/gate means, scored as a defect (FP4).
5. **low-impact-nitpick** — technically-true but immaterial; should be filtered/downgraded, not surfaced as a finding (FP5).
6. **over-reading-ambiguous-language** — manufacturing a defect from phrasing that has a clearly sound reading (the charity axis).
7. **stale-snapshot** — flagging something already fixed in a later edit (re-grounding / material-edit-invalidation gap).
8. **already-mitigated** — flagging a concern the plan/codebase already handles (the `no_existing_mitigation` check).
9. **fabrication / ungrounded** — a finding with no supporting evidence.
10. **library-guaranteed-capability** — flagging the absence of validation for a capability that is the documented contract of an adopted, maintained dependency (FP6). Validating it = testing code that isn't ours. Distinguish from an unproven *feature-maturity* edge of that same library, which IS worth flagging.
11. **rebuild-of-designated-experiment-reference** — flagging NIH / rebuild-vs-extend when the "existing" implementation is a designated POC / experimental-validation reference, not production code intended for reuse (FP7). Rebuilding a probe for production is the intended lifecycle.

Plus the INVERSE axis to eval (not an FP — a MISS): **sycophancy-induced false negative** — the verifier
charitably PASSing a real defect (the agent-under-fire risk). The eval suite must measure recall/false-accept
too, not only false-fire.

## How the suite should use these
Each labeled false-fire is a GOOD-case (the plan is sound on that axis) whose criterion should return
PASS / be dropped by Pass-2 entailment or Pass-3 impact-floor. Track per-criterion false-fire rate across
the taxonomy; calibrate the verifier + prompts until the modes above are suppressed WITHOUT trading them for
false negatives (the sycophancy axis). Source corpuses: DSO (read-only) + snap + rebar tickets.

## Proposed verifier/criterion tuning (from the `e6d9` workflow-engine-v2 dry-run)

Two new modes (FP6, FP7) the current Pass-2 verifier did NOT drop. Proposed levers, smallest-blast-radius first:

**Mode 10 — library-guaranteed-capability (FP6).** The mitigation is the dependency's own contract, which the
verifier didn't credit (`no_existing_mitigation` came back `no`).
- *Pass-2 system-prompt rule (preferred — generic, one place):* "If the asserted gap is a capability that is
  the **documented contract of an adopted, maintained third-party dependency** the plan commits to, the
  dependency's contract IS the existing mitigation: answer `no_existing_mitigation=yes` and, if a charitable
  reading of the plan relies on that contract, `evidence_entails_finding=no`. Do not require the plan to
  re-validate a dependency's headline guarantee (that is testing code that isn't ours). EXCEPTION: a specific,
  newer, or not-yet-GA *feature* of that dependency whose support is uncertain IS a legitimate gap — keep it."
- *T2/T3 descriptor ANTI-FP line (defense in depth):* "A capability that is an adopted library's advertised
  contract is PROVEN by adoption; flag only the project's OWN code paths or a *specific* integration point
  whose maturity is genuinely in question."
- This is exactly the FP6-vs-(T3/T1) boundary above — encode it so the verifier keeps the maturity flag and
  drops the contract flag.

**Mode 11 — rebuild-of-designated-experiment-reference (FP7).** A1/E4/G1G2 should not assert NIH against a
designated probe/reference.
- *A1 (+ codebase-grounded E4/G1G2) ANTI-FP clause:* "Before flagging rebuild-vs-extend NIH, check whether
  the 'existing' implementation is a designated experiment/POC/reference: signals = path under
  `docs/experiments/` or a `*_poc.*` name; an explicit 'reference, not deliverable / POC' designation in the
  plan or its linked brainstorm; an experimental marker in the code. If so, rebuilding it for production is the
  intended lifecycle — do NOT flag NIH."
- *Residual nuance (this case):* the v1 pkg lives under `src/rebar/llm/workflow/` (looks production), so the
  path heuristic alone won't catch it; the designation lives in the plan/brainstorm framing. When the
  artifact's production-vs-reference status is **ambiguous from location**, the criterion should COACH
  ("confirm whether the existing v1 engine is production-to-extend or a reference-to-rebuild") rather than
  ASSERT NIH — turning a false-fire into a cheap clarifying nudge.

**Calibration guardrail:** add FP6/FP7 as good-cases to the per-criterion false-fire set and re-measure recall
on the sycophancy axis after the change — neither lever may trade these FPs for a real-defect miss (e.g. the
T3/T1 feature-maturity flag must still fire).
