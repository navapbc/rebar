# ADR 0034 — LLM-routed enumeration overlays (T13 prohibition scan, T14 CI-trigger audit)

**Status:** Accepted (epic cite-stone-sea — DSO plan-review gap adoption / WS3 — win-attic-wren)
**Date:** 2026-07-06

## Context

DSO gap-report G-5 and G-10 both concern an **invisible affected set** — existing sites a plan
silently breaks:

- **G-5** — a plan that newly FORBIDS a previously-permitted action ("require tests before merge")
  breaks existing call sites that perform the outlawed behavior (`gh pr merge`), which nothing in
  the remaining plan references.
- **G-10** — a plan that adds a new git ref pattern / event / schedule silently fails to fire when
  existing CI workflow trigger filters do not include it (`branches: [main]` skipping per-story
  PRs); release-infra changes have the analogous gap.

An earlier framing had G1G2 emit an "overlay trigger" that the orchestrator turns into a spawn.
That was dropped after verifying G1G2 produces `list[dict]` findings, not a typed signal, and that
`route_criteria` already has an LLM-routed overlay path (`orchestrator.py:332`). A lexical trigger
over-fires on descriptive prose; an LLM-routed overlay running its own criterion call (its own
context window) is more reliable and needs no new inter-pass machinery.

Two id/registration facts constrain the solution:

- The gap-report labels are "G-5"/"G-10", but id **`G5` already exists** (the decomposition
  criterion), so reusing it would collide and overwrite a built-in.
- `registry.is_overlay()` recognizes an overlay only by the id pattern `T\d+[a-e]?`. A G-prefixed
  id is not recognized, so the overlay skip-guard and scrutiny gating would not treat it as one.

## Decision

Register the two overlays under the next free **Txx ids — `T13` (prohibition-enumeration) and
`T14` (CI-trigger / release-infra)** — so `is_overlay()` recognizes them and there is no collision.
Each is an **`exec:AGENT`, `overlay_routing:llm`** criterion (like T10/T11): it always enters the
finder (LLM-routed overlays are absent from the deterministic trigger map, so `route_criteria`
never skips them) and the LLM decides applicability — PASS not-applicable is cheap.

Each overlay enumerates its invisible affected set into a **closed classification enum**, rendered
in its prompt:

- **T13** → per existing call site of the outlawed behavior: `MIGRATED | EXEMPTED | UNCOVERED`
  (each UNCOVERED site is the finding).
- **T14** → per workflow trigger filter for the new pattern: `INCLUDED | EXCLUDED | NO_FILTER`
  (each EXCLUDED-but-should-fire workflow is the finding), plus a release-infra dependency check.

Both **fail open**: when the outlawed behavior cannot be reduced to a checkable grep (T13), or a
workflow's trigger syntax / CI system is unknown (T14), the overlay ABSTAINS with coverage rather
than asserting an ungroundable gap — it never fails closed on unknown CI and never fabricates a
call site.

## Consequences

- Added to `CANONICAL_LLM` + `criteria_routing.json` (validate-routing parity is bidirectional).
  Adding two ids bumps the registry version that plan-review attestations are keyed to — expected;
  each subsequent story is reviewed and claimed at the new version.
- The gap-report labels ("G-5"/"G-10") remain the human/eval vocabulary (the eval cases and tests
  are named `g5_prohibition` / `g10_citrigger`); the registry ids are `T13`/`T14`.
- No orchestrator change: the two overlays ride the existing LLM-routed overlay path.
