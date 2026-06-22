# SPIKE 2 — de-risking the per-child plan-review findings (epic 8f6c)

Experiments run against real tools to de-risk the major findings from the per-child review (session log
`plum-strut-reek`). Reproduce: `python3 docs/experiments/code-grounding-spike/spike2_derisk.py` (E1/E2/E3/E5,
local) and `python3 docs/experiments/code-grounding-spike/spike2_deps.py` (E4, needs network to api.deps.dev).

## E1 — engine failure-mode matrix (de-risk S4#0 engine-faithful validation, S4#5 per-backend)
- **OpenGrep/semgrep `--validate`** is engine-faithful and needs NO scan target: good rule → exit 0, schema-invalid
  rule → exit 2. So the loader can pre-validate each detector with the engine's OWN checker and quarantine a bad
  one (→ `abstain(invalid_detector)`) BEFORE the scan — closing the exit-7 whole-run-abort hole. **S4#0 resolved.**
- **ast-grep** rejects a malformed rule with its own non-zero exit (8, "Cannot parse rule") — per-rule, pre-validatable.
- **scc/lizard** are metric tools that consume FILES and have NO rule schema → `invalid_detector` is **N/A** for the
  metric backend; only missing-binary / unparseable-file → abstain applies. **S4#5 resolved** (per-backend: OpenGrep
  + ast-grep pre-validate via their own checkers; metric tools have no invalid-detector failure mode).

## E2 — collision/member false-refute: NAIVE vs GUARDED (de-risk S6#0; strengthen R-B)
On a fixture with `config` defined twice (collision) and dotted member refs (`store.reconcile_tickets`):
- **NAIVE** bare repo-wide name-existence **FALSE-REFUTES** the collision (1 false-refute).
- **GUARDED** (dotted/member → `abstain` (member binding is T2); name with >1 definition → `abstain(ambiguous)`;
  refute ONLY a unique bare name) → **0 false-refute**.
- **Verdict:** "scope the T1 refute verdict to name-existence" (R-B) is **necessary but NOT sufficient** — it must
  carry the **ambiguity + member guard**: refute only a *unique, bare, non-member* name; abstain ambiguous/member.

## E3 — refutation yield on rebar's OWN source (real, non-self-planted corpus; de-risk S6#1)
- ctags index over `src/rebar`: 3826 defs / 1998 distinct names. From 137 real INTERNAL import references:
  **resolved 120/137 = 88%** (107 unique single-def, 13 ambiguous multi-def); 12 hallucinated controls → **0
  false-refute.** **Verdict:** real-world yield is high but **88%, not the fixture's self-fulfilling 100%** — the
  eval must measure on a real corpus; and the 13 ambiguous names are exactly the collision class E2's guard abstains.

## E4 — deps-lane existence + abstain gauntlet vs REAL deps.dev (de-risk S3#0)
- 4 real pkgs (requests/pypi, react/npm, serde/cargo, pkg/errors-go) + 1 normalized (`scikit_learn`→`scikit-learn`
  via PEP 503) → **refute** (200). Hallucinated typos, a slop candidate, and stdlib `os` → **abstain** (404 →
  `not_on_public_registry`, NEVER a confident "absent"; stdlib → `abstain(stdlib)`). **Confident-"absent" emitted: 0,
  by construction.** **S3#0 resolved:** the deps lane is now exercised end-to-end against the real oracle and the
  gauntlet structurally cannot emit a false-absent.

## E5 — evidence normalization from real SARIF (de-risk S4#2)
- A real semgrep SARIF result + the rule's `metadata.rebar_envelope` maps to one normalized record:
  `{outcome, detector_id, reason, provenance_tier, job, location, coverage}` — and a skipped backend uses the SAME
  shape with `outcome=abstain, reason=…, coverage.status=skipped`. **S4#2 resolved:** `evidence-mapping` is concrete;
  match and abstain share one shape. (Impl note: derive the namespace + envelope fields from `rule.properties`, not
  the rule's temp path as in the throwaway fixture.)

## Net
All five empirical findings de-risked: engine-faithful pre-validation exists (`--validate`); the collision/member
guard is required and restores 0 false-refute; real-world yield is 88% (measured, not asserted); the deps gauntlet
can't false-absent; the evidence-mapping shape is concrete. These results feed the S2–S6 remediations.
