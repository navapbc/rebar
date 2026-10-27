SPIKE RESULTS — validating findings #1 (T1-floor refutation yield) and #2 (grep-ast repo-wide index;
OpenGrep-as-registry). Real tools, real polyglot fixture. Reproduction: `python3 docs/experiments/code-grounding-spike/spike_yield.py`
(self-contained: builds the polyglot fixture in a tempdir, runs universal-ctags, prints the metrics below);
the OpenGrep/registry-substrate rules are in `detectors_builtin/` + `detectors_project/`.

ENVIRONMENT: universal-ctags 6.2.1 (164 languages), grep-ast 0.9.0 (+tree-sitter-language-pack), semgrep
(== OpenGrep engine; OpenGrep is a fork with identical rule format/CLI/SARIF — faithful proxy for the mechanism).
Fixture: polyglot repo (Python, JavaScript, TypeScript, Go) with cross-file definitions + an unparseable-
language file (.qzx); a 20-item labelled reference set (10 real cross-file symbols, 9 hallucinated names, 1 real-
but-unparseable).

RESULT 1 — grep-ast is NOT a repo-wide indexer (finding #2b CONFIRMED). grep-ast 0.9.0's public API is
TreeContext (AST context around matches in ONE file) + a per-file `grep-ast <pattern> <files>` CLI. No
get_tags / tags.scm / repo-wide symbol extraction — that machinery lives in *aider*, not the `grep-ast`
package. => the design's "grep-ast ... repo-wide symbol/definition index" is wrong; the floor index must be
universal-ctags (or self-authored tree-sitter tag-queries). grep-ast at most provides AST-context DISPLAY.

RESULT 2 — universal-ctags repo-wide index has HIGH refutation yield (finding #1 ADDRESSED, bet holds).
On the fixture, querying the repo-wide ctags index for each reference (find a definition of this NAME anywhere):
  REFUTATION YIELD (real cross-file symbols resolved): 10/10 = 100%  (per-lang: py 4/4, js 2/2, ts 2/2, go 2/2)
  FALSE-REFUTE (hallucinated names wrongly 'found'):    0/9          (the critical safety property holds)
  ABSTAIN on hallucinated (correct — can't disprove):   9/9
  FAIL-OPEN on unparseable lang (real symbol -> abstain, no false-refute): PASS
HONEST CAVEAT: this measures NAME-existence on DISTINCT names — the bare-symbol / import-name / dependency-name
refutation class (which is exactly the high-base-rate hallucination class: slopsquatting, hallucinated imports,
hallucinated top-level symbols). It does NOT cover (a) common-name collisions (a hallucinated `get`/`Config`
colliding with a real unrelated symbol -> false-refute risk) or (b) member/attribute/signature resolution
(`foo.bar` on a type) — both are coarse at T1 and must ABSTAIN to T2 (semantic). So the floor's value is real and
high FOR ITS CLASS; the design must SCOPE the T1 REFUTE verdict to name-existence and abstain the rest.

RESULT 3 — OpenGrep/semgrep is a viable registry substrate (finding #2a VALIDATED). Pointing the engine at TWO
detector dirs (`--config detectors_builtin --config detectors_project`) unions their rules; the thin-envelope
`metadata: {rebar_envelope: {...}}` block is preserved VERBATIM in JSON output; rule IDs are path-NAMESPACED
(builtin vs project); SARIF 2.1.0 emitted. => "reuse OpenGrep rule-loading AS the registry; thin envelope rides
in `metadata:`; do NOT re-invent a match DSL" HOLDS.

RESULT 4 (bonus) — engine does NOT fail-open on a malformed rule. A schema-invalid rule alongside good ones
makes semgrep ABORT the whole run (exit 7, InvalidRuleSchemaError + SemgrepError; 0 findings from the good
rules). => the per-detector recover boundary CANNOT be delegated to the engine; rebar's loader must PRE-VALIDATE
each detector and QUARANTINE an invalid one (recorded coverage skip, reason=invalid_detector) BEFORE invoking
OpenGrep. (Target-file parse errors are different — those the engine skips-and-continues.) Reinforces finding #3:
fail-open is rebar's responsibility, owned at the loader/harness boundary.

NET DESIGN IMPACT: bets #1 and #2a hold (ctags floor is high-yield + safe; OpenGrep is the registry substrate);
bet #2b is corrected (grep-ast -> universal-ctags as the index). Plus two hardening requirements surfaced: scope
the T1 REFUTE verdict to name-existence (abstain member/collision to T2), and own the per-detector recover
boundary at rebar's loader (the engine aborts on a bad rule). The spike fixture + harness become the seed of S6's
refutation-yield eval (real corpus, real numbers — replacing the asserted yield).
