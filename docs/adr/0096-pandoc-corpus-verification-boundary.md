# ADR 0096 — Complete Pandoc corpus replay belongs in external integration

**Status:** Accepted
**Date:** 2026-08-14
**Supersedes:** the testing-assurance clause in
[ADR 0095](0095-dc-segmenting-wiki-renderer.md) that required every conversion
assertion to execute the real Pandoc binary
**Superseded by:** N/A
**Ticket:** `a38f-e290-9585-40be`

## Research findings

### Current renderer and test boundary

- **Sources:** [ADR 0095](0095-dc-segmenting-wiki-renderer.md),
  `tests/unit/rebar_reconciler/test_wiki_render_corpus.py`,
  `tests/unit/rebar_reconciler/test_wiki_render_hardening.py`, and
  `scripts/generate_dc_wiki_legacy_outputs.py`.
- **Finding:** rebar pins `pypandoc-binary==1.17` and Pandoc 3.9. The
  committed compatibility fixture pins both supported binaries: Darwin arm64
  SHA-256
  `d7b1e75cd20ee6a788a1399492be07d4559949e18e55f34cfc5c91807fdfa90d`
  and Linux x86_64 SHA-256
  `decd3dd11a3fe0c16ce56443343ec53adde6fbed6f97d7f56f06b1c424248e7b`.
- **Finding:** the corpus and hardening suites collectively cover 178 source
  bodies and 884 prepared Pandoc-bound units. They assert corpus integrity,
  secret scrubbing, protected excerpts, exact table and comment retention,
  deterministic settling, richness floors, non-vacuous dispatch, prepared-input
  alignment, immutable expected bytes, and binary provenance.
- **Finding:** GitHub Actions run 31798375024 spent 149.03 seconds setting up
  the shared first-pass corpus and 35.29 seconds in the prose fixed-point call
  on macOS. Those two visible phases are a 184.32-second lower bound, not a
  guaranteed saving; smaller hardening phases are excluded. A same-host
  three-worker run of both modules took 38.80 seconds wall time.
- **Constraint:** the production segmenting renderer, its safety and fallback
  invariants, the exact pin, and both supported platform binaries remain
  unchanged. This decision changes where repeated verification runs, not what
  the renderer does.

### External-integration destination

- **Source:** `.github/workflows/external-integration.yml` at the decision
  date.
- **Finding:** the workflow is already weekly and manually dispatchable. Its
  four current jobs all use `ubuntu-latest`; it has no macOS arm.
- **Constraint:** implementation must add a dedicated
  `pandoc-corpus-replay` job with `ubuntu-latest` and `macos-latest` matrix
  arms. Assigning replay to the existing workflow does not imply its current
  jobs already provide the required platform coverage.

### Maintained open-source practice

- **Pandoc:**
  [`test/Tests/Writers/Jira.hs` at `03a0a6484321`](https://github.com/jgm/pandoc/blob/03a0a6484321/test/Tests/Writers/Jira.hs)
  uses literal expected Jira-writer output for compact construct cases.
- **nbconvert:**
  [`tests/filters/test_pandoc.py` at `78ed30837a60`](https://github.com/jupyter/nbconvert/blob/78ed30837a60/tests/filters/test_pandoc.py)
  and its Pandoc utility tests use small real-binary probes rather than a
  production-sized repeated corpus.
- **pypandoc:**
  [`tests/test_pypandoc.py` at `f57e9ab51049`](https://github.com/JessicaTegner/pypandoc/blob/f57e9ab51049/tests/test_pypandoc.py)
  uses representative real conversions and explicitly avoids broader format
  coverage that would test Pandoc rather than pypandoc.
- **Kubernetes client-go:**
  [`dynamic/golden_test.go` at `3fcdd4c72588`](https://github.com/kubernetes/client-go/blob/3fcdd4c72588/dynamic/golden_test.go)
  uses committed request/response fixtures, fake or in-process transports, and
  an explicit fixture-regeneration switch.
- **Rust:**
  [`rustc-dev-guide` UI-test documentation at `059bf4a660dd`](https://github.com/rust-lang/rust/blob/059bf4a660dd/src/doc/rustc-dev-guide/src/tests/ui.md)
  documents blessed `.stdout`/`.stderr` UI snapshots, compact independent
  annotations, and normalization of platform noise.
- **Finding:** all five projects were active and unarchived when inspected.
  All use the same assurance shape: committed deterministic outputs for broad
  routine checks, bounded independent live or structural probes, and an
  explicit regeneration path. The surveyed sources contain no rejection of
  this pattern for reliability. This exceeds the session's two-thirds OSS
  consensus threshold.

## Context

ADR 0095 deliberately required every conversion assertion to execute the real
binary because the renderer and its Pandoc contract were new. That rule gave the
initial implementation a strong oracle, but applying it to every prepared unit
and every settling pass now dominates the macOS Verify critical path. The same
content repeatedly crosses a stable, pinned third-party boundary even when the
change under review does not affect Pandoc, the renderer, or the corpus.

The tests need to preserve two different assurances:

1. rebar's segmentation, protection, fallback, fixed-point, and byte-output
   contracts remain true over the full corpus; and
2. the pinned Pandoc binaries on Linux and macOS still produce the bytes on
   which those contracts were based.

Those assurances do not need the same cadence. Maintained OSS projects separate
broad deterministic output verification from compact live boundary checks, and
the weekly/manual external-integration workflow is the existing home for
complete third-party integration replay.

## Decision

1. **Gerrit Verify uses committed deterministic corpus outputs for the broad
   oracle.** Every existing assertion class remains required. The full corpus
   still covers cardinality and scrubbing, protected fragments, tables, HTML
   comments, settling/fixed-point behavior, richness floors, prepared-input
   alignment, and exact expected bytes. No assertion, source stratum, supported
   operating system, or supported Python cell is removed.
2. **Gerrit Verify retains a bounded real-Pandoc contract.** One deterministic
   body from each `code_arrow`, `table`, and `prose` stratum executes the real
   product conversion path. The existing compact pin, version, provenance, and
   non-vacuous-dispatch probes remain. The implementation chooses the exact
   three bodies by mutation sensitivity so the sample exercises real
   conversion, protected fallback, and settling behavior rather than merely
   selecting the first fixture entries.
3. **Complete real-Pandoc replay moves to External Integration Tests.** A new
   `pandoc-corpus-replay` job in `.github/workflows/external-integration.yml`
   runs every corpus unit and every required settling pass on both
   `ubuntu-latest` and `macos-latest`. It runs on the workflow's weekly schedule
   and on manual dispatch. This external job is the complete binary-integration
   oracle; the Verify sample is not represented as complete replay.
4. **Drift fails fast in both tiers.** Verify checks the installed Pandoc
   version and the supported platform's full binary SHA-256 before trusting
   committed outputs. A pin or binary change cannot silently keep using old
   fixtures. External replay compares live product output with committed bytes
   on each supported platform.
5. **Fixture updates are deliberate and reproducible.** The deterministic
   generator remains the only supported regeneration path. It records Pandoc
   version and platform-binary fingerprints and emits machine-independent
   fixture bytes. A renderer or pin change updates the fixture in the same
   reviewed change. Before those bytes become a compatibility baseline, the
   author manually dispatches External Integration Tests against that exact
   proposed ref (using a temporary GitHub mirror branch when the change is still
   under Gerrit review) and records the complete Linux and macOS replay result.
6. **Savings are measured, not assumed.** The implementation records paired
   before/after focused runs on the same host and the first genuine Gerrit
   Verify observation. The 184.32-second CI attribution is reported only as a
   one-run lower bound. If the result does not reduce the critical path as
   expected, the implementation is reverted without changing this decision's
   coverage requirements.

This decision supersedes only ADR 0095's requirement that every conversion
assertion execute the real binary. ADR 0095's production renderer, pin,
provenance, platform, safety, degradation, immutability, and cutover decisions
remain authoritative.

## Alternatives considered

### Keep complete real-Pandoc replay in every Verify run

- **Advantage:** every patch receives immediate complete binary replay.
- **Disadvantage:** it preserves at least 184.32 seconds of observed macOS
  critical-path work and repeatedly tests the same pinned external writer.
- **Why rejected:** the broad rebar assertions remain deterministic against
  committed real-Pandoc outputs, while three live contracts and fail-fast
  provenance preserve immediate boundary coverage.

### Cache or parallelize the complete replay more aggressively

- **Advantage:** retains the current cadence.
- **Disadvantage:** pass-one sharing is already implemented across xdist
  workers. More parallel subprocesses increase contention and runner usage,
  which conflicts with the CI-capacity constraint.
- **Why rejected:** it attacks scheduling rather than removing redundant
  third-party work, and unchanged aggregate runner usage is not an accepted
  remediation.

### Keep only the three live samples and delete complete replay

- **Advantage:** smallest CI cost.
- **Disadvantage:** loses exhaustive detection of corpus-specific Pandoc or
  platform drift.
- **Why rejected:** complete replay moves to a lower-cadence destination; it is
  not removed.

### Run complete external replay only on Ubuntu

- **Advantage:** consumes fewer scheduled runner minutes.
- **Disadvantage:** the distributed binaries differ by platform, and the
  measured critical path is macOS. Ubuntu-only replay cannot preserve the
  current macOS integration boundary.
- **Why rejected:** the external job has both Linux and macOS arms.

## Consequences and rollback

Gerrit Verify keeps immediate behavioral and contractual coverage while
removing repeated full-corpus subprocess work from its hot path. Based on the
one observed run, removing the 184.32-second lower bound would move the recent
31m29s average to roughly 28m25s, inside the 20–30 minute target; this is an
estimate until paired implementation and CI evidence exist.

Complete Pandoc drift may surface on the weekly job instead of the originating
unrelated change. Immediate version, fingerprint, and representative live
checks bound that delay. The new external macOS arm consumes scheduled runner
minutes and can still contend when a weekly/manual run overlaps Verify. The
aggregate cost is nevertheless reduced because complete replay moves from every
change to weekly/manual cadence rather than being parallelized at unchanged
frequency.

The committed outputs become reviewed compatibility artifacts. A fixture update
that merely blesses a regression remains possible, so regeneration is explicit,
provenance is pinned, three independent live contracts stay in Verify, and the
complete external replay must pass before a fixture-changing implementation is
accepted.

Rollback is a Gerrit revert of the implementation change: restore complete
real-Pandoc corpus execution to the default test tier and remove the dedicated
external replay job. No production data, renderer behavior, or fixture format
migration makes rollback destructive.

## References

- Gerrit Verify run `31798375024`
- Ticket `4f50-b1f2-78af-4326` (`inedible-subsimious-brahmancow`), containing
  the timing dossier, assertion map, OSS research, and approved critique
- [ADR 0095](0095-dc-segmenting-wiki-renderer.md)
- `.github/workflows/external-integration.yml`
- `scripts/generate_dc_wiki_legacy_outputs.py`
