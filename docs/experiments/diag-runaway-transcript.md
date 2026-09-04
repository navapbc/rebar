# Diagnostic: why the completion verifier runs away

**Question posed:** during the f6fc close the completion verifier aborted a runaway
tool-call loop (`requests=188 tool_calls=196 distinct=164 max_consecutive_repeat=9
distinct_ratio_window=0.5`, top repeats all `search_files`). The durable gate-error
record hashes tool arguments and drops results by design, so it could not tell us *why*.
`docs/experiments/diag_runaway_transcript.py` closes that gap: it wraps the real
verifier tools with a local (uncommitted) transcript logger and drives the real
`verify_completion` against f6fc with the step floor lowered so the run is cheap.

## Method

- Target: **f6fc** (8 acceptance criteria), verified against its own commit tree — the
  same ticket+tree whose close ran away.
- Tools wrapped: `filesystem_tools` / `grounding_tools` / `rebar_tools`, logging
  `(tool, args, result-preview, is_error, is_empty_sentinel, len)` to a LOCAL JSONL
  outside the repo. Raw results are **not** committed (they contain repo content) — this
  file records only content-free aggregates, mirroring `run_shape`'s privacy contract.
- Greedy decoding (temperature 0.0, the verifier's pinned default).
- Induced-exhaustion knob: `completion._VERIFY_MIN_STEPS` lowered to 50 (the sanctioned
  "lower the iteration limit" pattern) so the run is bounded and cheap.

## Result — the same ticket that ran away at 480 steps *converged and PASSED at 50*

| run | step floor | request budget | tool calls | outcome |
|---|---|---|---|---|
| f6fc close | 480 | 240 | 196 | runaway-guard abort → recovery |
| this diagnostic | 50 | 25 | **98** | **converged → PASS** |

Tool breakdown of the PASS run (98 calls): `read_file` 48, `search_files` 31,
`list_directory` 12, `show_ticket` 7. **0 errors. 6 honest empty-sentinels.**

## What is actually happening (measured, not inferred)

1. **It is NOT a tight loop and NOT tool false-negatives.** Zero tool errors; every
   `(no matches)` was a correct, path-scoped miss (`type: ignore`/`noqa` genuinely absent
   from `agent_call.py`; `agent_call` genuinely not in `.github/complexity-baseline.json`).
   The tools behaved correctly.

2. **The dominant inefficiency is RE-READING.** 48 `read_file` calls touched only **11
   distinct files → 37 re-reads (77%)**. The verifier loses track of what it has already
   read and re-fetches the same files while working through the 8 criteria. Because
   `read_file` re-reads use different `line_start`/`line_end`, most of them carry a
   *different* signature — so the f6fc exact-signature memo does **not** collapse them.

3. **Budget-anchored expansion (hypothesis, n=1 each but a strong, cheap-to-confirm
   signal).** Given 25 requests the greedy model committed to a PASS in 98 calls; given
   240 requests the same input wandered past 196 and tripped the runaway guard. The
   verification appears to "expand to fill" the budget it is given. This is consistent
   with the variance the code already documents (bug e458 / ad9f: FAIL↔PASS on identical
   sha), but adds a new axis: the `_VERIFY_MIN_STEPS = 480` floor may itself be a driver,
   not just a safety ceiling.

4. **Minor, real tax:** the model occasionally issues *regex* queries against the
   *substring-only* `search_files` (`Guard\|Memo\|Steering\|wrapper`) and line-number
   literals (`:243`), which correctly return `(no matches)`. Small, not the driver.

## Implications

- **Validates 2948's core thesis with real data.** Banking verified criteria so they are
  not re-derived, and running batched successors over only the *remainder*, directly
  attacks the re-read volume (#2) and the wander-to-exhaustion mode (#3).
- **New lever not in 2948's plan:** the flat `_VERIFY_MIN_STEPS = 480` floor plausibly
  *induces* wandering. A lower or criteria-scaled floor may converge more reliably and
  cheaply. Confirm cheaply by running `verify_completion(f6fc)` a handful of times at
  floors 50 / 240 / 480 and comparing convergence vs runaway rate before committing to
  banking-only.
- **Memo refinement candidate:** normalizing `read_file` signatures by *path* (cache file
  content once, serve sub-ranges from cache) would collapse the 77% re-read tax that the
  current exact-signature memo misses.

## Reproduce

```
AWS_REGION=us-east-1 REBAR_LLM_STANDARD_PROVIDER=bedrock \
REBAR_LLM_STANDARD_MODEL=us.anthropic.claude-sonnet-4-6 \
REBAR_LLM_MAX_STEPS=50 REBAR_GATE_ALLOW_UNGATED=1 \
python docs/experiments/diag_runaway_transcript.py f6fc <f6fc-sha>
```

## Confirmation matrix (causal test of budget-anchoring)

`docs/experiments/diag_runaway_matrix.py` ran the SAME f6fc verification K times at
three step floors (greedy, same tree). Recording total tool calls, whether the in-flight
runaway guard fired, and the verdict:

| floor | request budget | runs | tool calls | runaway tripped | verdict |
|---|---|---|---|---|---|
| 50  | 25  | 2 (+1 diagnostic) | 98, 101, 102 | **0/3** | PASS (clean) |
| 240 | 120 | 2 | 145, 165 | **2/2** | PASS (via recovery) |
| 480 | 240 | 2 (+1 close) | 131, 144, 196 | **3/3** | PASS (via recovery) |

**Budget-anchoring is confirmed.** At floor 50 the verifier converges to a PASS in ~100
calls and NEVER trips the runaway guard; at floors >=240 it ALWAYS trips and falls into
recovery. The verification reaches sufficient evidence around ~100 calls / ~25 requests,
but a larger budget does not make it stop there — the surplus is spent on the redundant
re-reading the runaway guard then (correctly) catches. The flat `_VERIFY_MIN_STEPS = 480`
floor hands an 8-criterion ticket roughly 10x the budget it needs.

**Implication for lever 1:** f6fc (8 criteria) converged cleanly at ~6 steps/criterion
(floor 50). A criteria-scaled primary floor (`steps_per_criterion x n_criteria`, a sane
minimum, well under the flat 480) would let typical tickets converge without ever
entering recovery — attacking the runaway at its source, complementary to 2948's banking
(which insures the recovery path when a genuinely large ticket still exhausts).
