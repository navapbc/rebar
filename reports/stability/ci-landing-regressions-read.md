# CI-at-landing regressions — 2026-09-01 (window 2026-08-25 .. 2026-08-30)

Ticket `floaty-imperfect-pomeranian` (`0880-0afb-7fe3-48c9`), read (b) of three, under epic
`wide-wimpy-insect` (Track I — reduce defect introduction).

R2S3 found CI-at-landing friction roughly doubling across its sample (9 → 16 of 25/60 session
logs), and left the cause unexplained. This is the mechanism read of that window: what actually
failed, not what it was labelled.

## Headline

- **Zero of the 20 substantiated cases was a flake.** Every one had a determinate mechanism.
  One session states it outright for its five ("Every `Verified -1` investigated was
  deterministic"), and case 19's investigation actively *disproved* a standing flake hypothesis.
- **Drift is the largest class: 9 of 20.** Generated artefacts, committed baselines, ratchets and
  surface parity — a change is correct, and CI rejects it because a second representation of the
  same fact was not regenerated. This is the same mechanism the sibling mirror-inventory read
  found (see `mirror-inventory.md`), arriving from the other direction.
- **3 of 20 involve cross-session interference**, and that count is a **floor, not a
  measurement** — it is detectable only where a session log happens to say so.
- **Two failures in the window were mislabelled environmental** by misreading a workflow that
  echoes its own script source as error output. The tell: genuine failures are `##[error]` lines
  **without** the `36;1m` colour prefix.

## Method

The population was derived from **Gerrit**, not from log keywords, because log narration is
incomplete:

```
merged Gerrit changes submitted 2026-08-25..08-30 : 213
  with >=1 `Verified-1` message before merge      :  84   (39.4%)
  total `Verified-1` votes cast in the window     : 241
```

`rebar session-logs --limit 400 -o json` returns 400 `session_log` tickets spanning 2026-06-20 ..
2026-09-01; **31** fall in the window, consistent with R2S3's "newer 30 of 60". Cross-referencing
all 84 changes against all 400 log bodies yields 35 with a narrative; **20 have a root-cause
mechanism I could substantiate**. R2S3's seeds 2359/2360/2361/2362/2345 are all in the window and
all in the 84. Its other seeds **2047 and 2057 are not in this window** — they are narrated
2026-08-22 — and the window was not stretched to include them.

Change shape is the sha-matched main commit's own diff, via the imported
`count_non_test_diff_lines` (the shipped predicate).

## The 20 cases

`ps` = patchsets; `V-1` = `Verified-1` votes before merge.

| # | Chg | Subject | ps | V-1 | LOC | files | Mechanism | Class |
|---|---|---|---|---|---|---|---|---|
| 1 | 2202 | Guard connected Jira probes | 3 | 2 | 750 | 11 | Probe imports the adapter package but calls module-level members that `AcliClient` owns; import-only preflight passed, the real call failed | drift (API surface) |
| 2 | 2222 | Fix mcp issuer URL env | 3 | 2 | 5 | 3 | New `REBAR_MCP_AUTH_ISSUER_URL` added to compose but not to `autodeploy.sh mcp_run_new` | drift (surface parity) |
| 3 | 2225 | Pin ticket reads, publish close atomically | 11 | 18 | 4669 | 45 | Six recorded cycles: Ruff 21 findings then I001; generated `docs/env-vars.md` drift; complexity ratchet (2 new + 1 increased); mypy 31 then 7; 14 deterministic test failures. Local gates deliberately skipped per operator instruction — CI was the only oracle | drift (generated + ratchet) |
| 4 | 2238 | RP-03 S5 T2 versioned outcome sections | 3 | 2 | 337 | 7 | LLM-Review provider 401 transient (recovered via `rerun-llm-review`, ADR 0069); then plan-review `stale-material` as `file_impact` went 6→7 | attestation staleness |
| 5 | 2241 | RP-03 S5 T3 coordinator E2E taxonomy | 3 | 2 | 74 | 5 | `ModuleNotFoundError` at collection: an E2E suite placed in the interface-parity tier imports engine internals via raw `sys.path.insert`; that tier collects **before** `tests/unit` and has no engine path | isolation (tier/collection order) |
| 6 | 2249 | Live-Cloud coordinator mutation lane | 4 | 4 | 232 | 11 | Real regression: read-via markers on `access_check.py` forbidden by the held-out seam-clean test; 3 JQL-backoff knobs bypassed the owned `config.py` resolver | drift (seam/ownership) |
| 7 | 2303 | warnings: cross-session detector + toggle | 4 | 4 | 273 | 7 | Two Linux-only findings surfaced only because a module-size fix ran the full suite for the first time: a test referenced `_warn_unknown` by attribute path and Ruff dropped it as F401 during an 808→733 LOC split; ARG ratchet hit 26 > 25 | drift (ratchet + re-export loss in a split) |
| 8 | 2309 | CLI cross-session warning | 3 | 2 | 50 | 4 | Two tests broken by a new mutation-time `show_ticket` read: `os.environ.copy()` tripped the subprocess-env-repr audit, and `_event_count` globbed `*.json` and counted the reducer's regenerable `.cache.json` as an event | isolation (shared state) |
| 9 | 2345 | Run sync MCP tool bodies off the loop | 3 | 2 | 107 | 3 | A real deterministic test failure, explicitly classified "not a flake" | plain |
| 10 | 2355 | Bound the MCP list read surface | 7 | 5 | 394 | 16 | Rebasing from a **hardcoded patchset ref** (`refs/changes/55/2355/1`) rather than `current_revision` silently discarded fixture fixes already pushed for an earlier failure | stack/rebase · cross-session |
| 11 | 2359 | Defer git auto-maintenance off store commit | 3 | 2 | 160 | 14 | `gitutil.py` at 833 against the hard 800 module-size cap; identical on all four platforms | drift (module-size ratchet) |
| 12 | 2360 | Surface op-cert keys with no public counterpart | 8 | 5 | 848 | 5 | Broad `except Exception` on the MCP boot path without the required `RemovedInputError` guard | guard contract |
| 13 | 2361 | Stop gating op-certs on signing environment | 3 | 2 | 305 | 16 | `types.py` stale — a **hand-edited generated file** had widened `trust_basis` from the schema `Literal` to `str` | drift (generated artefact) |
| 14 | 2362 | Prove the MCP request path before uvicorn serves | 3 | 2 | 343 | 5 | Same class as 2360 | guard contract |
| 15 | 2402 | Pin eval passes to production Bedrock models | 5 | 4 | 102 | 2 | Two of the author's own held-out tests were wrong: they assumed ambient `load_class_slots()` reads the real project `rebar.toml`, but `tests/conftest.py` globally sandboxes `REBAR_ROOT` | isolation (global sandbox vs ambient config) |
| 16 | 2416 | Register `pinned_ticket_read_failed` error code | 4 | 2 | 1 | 3 | Adding one code to `KNOWN_ERROR_CODES` drifted the public-surface gate; fix was regenerating `api_surface_baseline.json` (+1 line) | drift (generated baseline) |
| 17 | 2418 | Split overloaded `coverage.llm_unavailable` | 4 | 1 | 28 | 9 | Ancestors 2413/2415 merged and were Gerrit-rebased to new SHAs; the remaining stack had to be rebased `--onto` current `origin/main`, dropping merged commits, and re-pushed | stack/rebase · cross-session |
| 18 | 2419 | Error-code taxonomy prevention guardrail | 4 | 3 | 0 | 1 | Same stack-rebase mechanism as 2418 (stack top; submitting it atomically merged 2418+2416) | stack/rebase · cross-session |
| 19 | 2436 | e9d5 typed mutation payload union + comparator | 4 | 10 | 659 | 7 | Genuine isolation bug in the change's **own** new test file: `effect_spies` patched `socket.socket.connect`/`connect_ex` via `monkeypatch.setattr`, colliding with the autouse `_network_guard` (`mock.patch.object`); teardown ordering left `connect` permanently shadowed for every later test in the worker, matching all 7 CI failures exactly | isolation (fixture teardown leak) |
| 20 | 2441 | docs: ADR 0108 retire the severity label | 3 | 2 | 118 | 3 | Docs-only route's ADR-number bijection gate failed: missing `docs/adr/.numbers/0108` marker. `README.md` and `.numbers/` are generated by `gen_adr_index.py`; the hand edit was right in content, wrong mechanically | drift (generated artefact) |

## Aggregation

| class | n | cases |
|---|---|---|
| **Drift** — generated artefact, baseline, ratchet, surface parity, seam | **9** | 2202, 2222, 2225, 2249, 2303, 2359, 2361, 2416, 2441 |
| **Isolation** — test tier, fixture teardown, sandbox, shared state | **4** | 2241, 2309, 2402, 2436 |
| **Stack / rebase drift** | **3** | 2355, 2418, 2419 |
| Guard-contract defect (real code bug) | 2 | 2360, 2362 |
| Attestation staleness / provider transient | 1 | 2238 |
| Plain deterministic test failure | 1 | 2345 |
| *of which cross-session interference* | *3* | *2355, 2418, 2419* |

## Change shape of the window's failures

Same direction as read (a), on an independent window:

| group | n | non-test LOC median / mean | files median / mean |
|---|---|---|---|
| ≥1 `Verified-1` | 84 | 133 / 257.5 | 5 / 6.8 |
| clean first pass | 129 | 35 / 149.0 | 3 / 4.4 |

Roughly 3.8× the median size and 1.7× the median file surface.

## A structural generator that is not a code defect

One session records another session pushing under the same `RebarBotNava` identity, producing
duplicate changes on two tickets (2347 vs 2351; 2344 vs 2352), and **two sessions writing into one
worktree**, one amending the other's uncommitted work. It also names `CHANGELOG.md` as a live
conflict hotspot: every merge invalidates every other in-flight change touching it, and rebasing
**drops both votes**. That is a `Verified-1` generator with no defect anywhere in the change.

## Gaps

1. `Verified-1` root causes are **not machine-readable**. Gerrit messages carry only the vote and
   an Actions run URL; the failure text lives in the Actions log, which was not fetched (needs the
   GitHub API, and window runs are largely expired). Hence 20 mechanisms of 84 failing changes.
2. The `CI_RESULT` JSON convention would have given a labelled `failure_type` taxonomy, but only
   **9 records exist across all 400 logs**, 7 in the window, 6 of those from a single change. It is
   one session's convention, not a corpus.
3. Cross-session interference is **under-counted** — 3 of 20 is a floor. There is no structural
   marker in Gerrit or the store for "another session touched this worktree".
