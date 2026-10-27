# Escaped-bug analysis — did code review catch it, and where are the criteria gaps?

Population: **43** bug tickets closed `close_class == regression` (a defect NEWLY introduced by a
change = the true "escaped code review" set). Excluded as non-escapes: preexisting (256),
plan_defect (42, a plan-review concern), duplicate (40), env_integration (37), flaky (20),
not_a_bug (23), undetermined (2), unclassified (232).

Method: index every code-review sidecar by reviewed file (deps keys + finding locations). For each
regression bug, look at PASS reviews of its buggy source file(s) BEFORE the bug was filed, and
score each non-blocking finding (advisory/coaching/dropped) by shared distinctive identifiers
(function names, `--flags`, filenames) with the bug's root-cause text. `>= 3` shared identifiers =
the finding plausibly describes the SAME defect (file-level co-location alone is noise — high-churn
files accrue dozens of unrelated nits).

## Result

| bucket | n | meaning |
|---|---|---|
| (a) genuine defect-match | 8 | a non-blocking finding described the same defect |
| (a-weak) file-level only | 25 | file had nits but none matched the defect |
| (b) criteria gap | 3 | file PASS-reviewed, ZERO non-blocking findings on it |
| ( ) not joinable | 7 | introducing change predates the sidecar corpus |

## (a) Would tightening thresholds have caught the 8 genuine catches?

The finding EXISTED as advisory/dropped for all 8, but only **1 of 8** would have been converted to
a BLOCK by the approved flips:

| bug | matched finding (criteria, priority) | approved flip blocks it? |
|---|---|---|
| autumnal-defectible-xeme (`rebar metrics` with no dates crashes) | regression, **0.60** | **YES** — regression@0.54 |
| citric-preregal-ladybird (removed `rebar.llm.review_ticket` breaks 4 CI lanes) | deletion-impact/api-compat/regression, 0.30 | no — below 0.60/0.51 |
| effusive-sportive-sapsucker (CLOSE profile never wired) | maintainability, 0.128 | no — non-flipped, low prio |
| brattish-ladyish-jaguar (unguarded CI gate steps) | regression/edge-cases, **DROPPED** (val 0.43) | no — verifier dropped a correct finding |
| waspish/stationary/athletic/archetypic | supply-chain/tests/docs/scope-intent, 0.0–0.3 | no — tangential or low prio |

**Takeaway:** the approved flips are validated (they catch xeme, a real crash-on-default-invocation
regression, at regression prio 0.60) but threshold-tightening is NOT the main lever for the escaped
tail. The other catches sat at priority 0.0–0.30 or were DROPPED by the Pass-3 decider. Two
non-threshold levers matter more:

1. **Verifier recall.** brattish-ladyish-jaguar's finding correctly described the defect and was
   DROPPED (validity 0.43). A correct low-validity drop is a recall loss no threshold can fix —
   worth auditing decider drop-rate on `regression`/`edge-cases`.
2. **Targeted impact boost for removed-public-symbol breakage.** citric-preregal-ladybird's
   `deletion-impact`/`api-compat` finding named the removed public function AND a concrete broken
   caller (an external test) yet scored only priority 0.30, below the flips. A finding that cites a
   removed public symbol with a named breaking caller is near-certain breakage and should get an
   impact boost so it clears the block threshold — a precise tightening, not a blanket drop.

## (b) Criteria gaps — recurring escaped-regression classes with NO relevant finding

Clustering the (b) + zero-score (a-weak) + not-joinable regressions by nature:

1. **Import / packaging resolution integrity** (≈4): `cinderlike-faulty-yucker` (helpers can't
   import git adapter), `spinal-grayish-perch` (`import alert_dedup` only resolves with `scripts/`
   on sys.path), `fibrous-fabulous-dungbeetle` (preview lock test can't import reconciler),
   `vapid-ferrous-weaverbird` (monkeypatch breaks lazy provider import). The diff imports/runs
   fine in-tree but breaks under real import order / sys.path. **No current criterion checks import
   resolvability.**
2. **CI-gate / workflow rollout backward-compat** (≈4): `medium-loved-newt` (Git-floor gate
   hard-fails in-flight branches), `impudent-sodium-ferret` (new-gate steps fail pre-gate changes),
   `rancid-aerophobic-skylark` (module-size cap breaks main), `brattish-ladyish-jaguar` (unguarded
   gate steps). A new hard-fail gate/floor/cap that doesn't grandfather in-flight or pre-gate
   branches. **No criterion reviews `.github/workflows` + gate scripts for rollout compat.**
3. **CLI ↔ MCP ↔ import-format surface parity** (≈3): `halfwhite-chordal-stonefly` (`--class`
   requirement added to CLI, not to MCP `transition_ticket` + NDJSON import — 9 prior PASS reviews,
   ZERO findings), `obnoxious-ogreish-neontetra`, `lemon-rabid-solenodon`. A guard/arg/behavior
   added to one entry point and not mirrored to its siblings. **No criterion enforces entry-point
   parity.**
4. **Live / external-integration signatures** (≈4): `ulcerative-confined-blacklemur`
   (`observed_upsert()` unexpected kwarg on the bedrock lane), `vaporish-germicidal-chamois`,
   `kimberlite-mindful-nabarlek`, `glass-worriless-mammal`. The diff is self-consistent in-tree but
   breaks against the real integration — largely undetectable by static diff review; belongs to CI,
   not review.

## Recommendations

**Threshold levers (beyond the already-approved flips):**
- Audit the Pass-3 decider drop-rate on `regression`/`edge-cases`; a correct-but-dropped finding
  (brattish) is a recall loss thresholds can't recover.
- Add a targeted impact boost in `impact_code` for `deletion-impact`/`api-compat` findings that
  cite a REMOVED public symbol together with a named broken caller (would lift citric-preregal-
  ladybird over the block line without a blanket threshold drop).

**New / expanded criteria (each addresses a recurring escaped class current criteria miss):**
- `import-integrity` (content-triggered when a change moves/removes a module, edits sys.path, or
  reorders a lazy import / monkeypatch target) — OR expand `deletion-impact` to cover import-graph
  breakage, not just dangling call sites.
- `surface-parity` (content-triggered when a CLI command's args/guards change) — require the MCP
  tool + NDJSON/import path be updated in lockstep.
- `ci-gate-compat` for `.github/workflows/**` + gate scripts — a new hard-fail gate must guard /
  grandfather in-flight and pre-gate branches. **REJECTED — do not re-propose** (operator
  decision 2026-08-19, reaffirmed 2026-08-21; ticket 6f2e-58a5, Gerrit change 1919 abandoned):
  when a new hard-fail gate breaks an in-flight branch, the remedy is to rebase on origin/main
  and fix the actual gate failure; a grandfathering criterion institutionalises working around
  gate failures instead of fixing them.

**Out of scope for code review:** live/external-integration signature breaks (class 4) are a CI
concern, not a static-diff-review concern.

## Caveats
- File-level join + identifier-overlap is a heuristic; the 8 genuine matches were hand-adjudicated,
  the (b)/gap clustering is by title/theme. A git-blame of each regression's introducing commit →
  its exact review would sharpen attribution but is not needed for the class-level conclusions.
- 7 regressions predate the joinable sidecar corpus; the true escaped-tail is modestly larger.
