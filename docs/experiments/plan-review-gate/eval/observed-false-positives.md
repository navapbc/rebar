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

Plus the INVERSE axis to eval (not an FP — a MISS): **sycophancy-induced false negative** — the verifier
charitably PASSing a real defect (the agent-under-fire risk). The eval suite must measure recall/false-accept
too, not only false-fire.

## How the suite should use these
Each labeled false-fire is a GOOD-case (the plan is sound on that axis) whose criterion should return
PASS / be dropped by Pass-2 entailment or Pass-3 impact-floor. Track per-criterion false-fire rate across
the taxonomy; calibrate the verifier + prompts until the modes above are suppressed WITHOUT trading them for
false negatives (the sycophancy axis). Source corpuses: DSO (read-only) + snap + rebar tickets.
