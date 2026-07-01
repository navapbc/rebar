# T5c trust-boundary recalibration (task 2e89)

Old rubric (category-based) vs new rubric (explicit trust-boundary scope gate), run as the T5c
Pass-1 finder in isolation via `criterion_preview` against three real ticket plans. Because the
changed artifact is a prompt, these are **live** LLM runs (not replayed fixtures) — the old rubric
is supplied inline, the new via `criterion_id="T5c"`.

Reproduce: `REBAR_MCP_ALLOW_LLM=1 REBAR_GATE_ALLOW_UNGATED=1 python scripts/t5c_calibrate.py`
(the one-off script lives in the PR; it reads each plan from the store and runs both rubrics).

| Fixture | Kind | Old verdict | New verdict | Outcome |
|---------|------|-------------|-------------|---------|
| `d251` (AWS Gerrit — internet-facing :443/:29418, no human/admin auth) | positive | fire | **fire** | true positive preserved — the trust-boundary gate opens (public internet actor) and T5c surfaces the exposed surface |
| `8a1c` (deterministic signed-gate work — local, in-process, no network) | negative | **fire** | **no-fire** | **false positive removed** — old rubric flagged Ed25519 key-lifecycle on a local no-network tool; the new gate PASSes it as not-applicable (no lower-trust actor reachability) |
| `4702` (kind-keyed attestations — local, in-process, no network) | negative | no-fire | no-fire | stable pass — no boundary crossed |

## Reading

- **Recall preserved.** `d251`, the networked-service positive, still fires — the framing sharpened
  the *scope*, not the sensitivity.
- **Precision improved.** `8a1c` is the headline: the pre-change rubric reasoned by *category*
  ("there is new private-key material → demand a key lifecycle") and fired on rebar's own local,
  in-process signing work — a false positive. The trust-boundary gate asks *is any lower-trust actor
  reachable?* first; the answer is no, so it PASSes not-applicable. This is exactly the FP class the
  ticket set out to remove while keeping rebar's own local work FP-free.
- **No regression on the other negative.** `4702` was already a clean pass and stays one.

Net: 1 true positive kept, 1 false positive eliminated, 1 stable — a strict precision gain with no
recall loss on this set.
