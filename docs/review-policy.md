# Review policy — what the `LLM-Review` and `Verified` gates mean

Every change to `main` must earn **two `+1` votes** on Gerrit before it can be
submitted — an LLM code review and CI — plus **no unresolved comments**. This page
is the **authoritative document** for what those votes mean, who may cast them, how
to respond to each, and how to dispute one. (The in-flow quick reference lives in
[CONTRIBUTING.md](../CONTRIBUTING.md) §2c; this document carries the normative
content.)

> **Note on novelty.** A *blocking* `LLM-Review` merge vote appears to be
> first-of-kind in public open source — peers such as Kubernetes keep AI review
> **advisory-only**. Because it is unusual, this policy documents it fully. The
> **document of record** for vote semantics is this file; the **source of truth**
> is the code (the tag enums below are transcribed from it — see the drift-guard).

## The two required labels

### `Verified` — CI
`Verified` is cast by CI running on GitHub Actions via
[`gerrit-verify.yaml`](../.github/workflows/gerrit-verify.yaml) against your exact
patchset. `+1` = CI passed; `-1` = CI failed (a run link is posted with the vote).

`Verified` is **more than three make targets.** In addition to lint/typecheck/test,
the workflow runs the `rebar-ticket` trailer check, the module-size gate, the
prompt-index and env-registry drift gates, security-rules freshness, criteria-routing
parity, the public-types drift check, `make config-check`, `pip-audit`, and both the
unit and integration pytest tiers. **You can locally _approximate_ it** with
`make lint && make typecheck && make test`, but the authoritative list is the
workflow file linked above — treat it as the source, not the three make targets.

### `LLM-Review` — LLM code review
`LLM-Review` is cast by the rebar review-bot, which reviews the diff of your change.
**Only the bots and administrators may cast either label** — there is no
self-approval and no self-verification, so you cannot vote your own change through.

## Vote-semantics table

The `LLM-Review` bot tags every vote so you can tell a *finding* (your code) from a
*coverage-gap* (infra could not prove your change safe — a fail-closed veto that is
**not** about your diff). The tags below are transcribed from the code
(`src/rebar/review_bot/adapter.py` and `voter.py`):

| Bot message tag | Meaning | Your productive response |
|---|---|---|
| `[LLM-Review: PASS]` | The review passed. | Nothing — this is a `+1`. |
| `[LLM-Review: BLOCK — finding]` (+ inline comments) | **Real finding(s)** in your code. | Fix the code, amend, and re-push. |
| `[LLM-Review: BLOCK — coverage-gap (gate-disabled)]` | Infra veto: the review gate was disabled. | Not your code — a maintainer re-triggers once infra is back. |
| `[LLM-Review: BLOCK — coverage-gap (llm-unavailable)]` | Infra veto: the LLM backend was unavailable. | Not your code — re-push the same commit or ping the maintainer to re-run. |
| `[LLM-Review: BLOCK — coverage-gap (scanner)]` | Infra veto: a scanner step failed. | Not your code — maintainer re-triggers. |
| `[LLM-Review: BLOCK — coverage-gap (review-error)]` | Infra veto: the review errored out. | Not your code — maintainer re-triggers. |
| `[LLM-Review: BLOCK — coverage-gap (indeterminate)]` | Infra veto: the outcome could not be determined. | Not your code — maintainer re-triggers. |
| `[LLM-Review: BLOCK — coverage-gap (merge-review)]` | Infra veto **on a merge change** (epic feature-branch merge-back): the merge-change review could not run. | Not your code — maintainer re-triggers the merge review. |
| `… (merge-change, N integrated commit(s))` suffix | A merge change carrying N already-reviewed commits; the suffix annotates the merge path. | Informational; respond to the base tag as above. |

**In one line:** a `finding` means *fix and re-push*; **any** `coverage-gap (…)`
means *infra veto, not your code — the maintainer re-triggers it*; a `Verified −1`
means *CI failed — open the linked run*, and for a flake comment **`recheck`** to
re-run CI on the same patchset.

> **Drift-guard.** If you see an `LLM-Review` tag that is **not** listed above, treat
> it as coverage-gap-class (an infra veto, not a code finding) and ping the
> maintainer. (A CI gate that mechanically ties this table to the code enums is
> future work.)

## Bot vs. human authority

The gate is **required-with-human-override** — it mechanically enforces a floor, but
a human is always above it:

- **The lead maintainer owns the rubric** and may **waive or override** any finding.
  The bot supplements maintainer judgment; it does not replace it.
- **Dispute path.** If you disagree with a finding, resolve the comment thread with a
  written justification, or raise it to the maintainer. Expect a **best-effort**
  response within **5 business days** (matching SECURITY.md's acknowledgement window).
  If it stays unresolved, the maintainer's decision is final — rebar is
  solo-maintained today, and this policy states that plainly.
- **Bypasses are admin-only and auditable.** Only an administrator can bypass a gate,
  and any such **bypass** is recorded.

## Responsibility clause

You must be able to **personally explain your change** — what it does and why —
whether or not it was AI-assisted. "The tool wrote it" is not an answer to a review
question; authorship carries responsibility.

## This gate vs. the plan-review gate

This document is about the **code-review merge gate** (`LLM-Review` + `Verified` on a
Gerrit change). It is distinct from the **plan-review ticket gate**, which reviews a
ticket's *plan* before you may claim it — see
[docs/plan-review-gate.md](plan-review-gate.md). Different gate, different stage,
different artifact.

## Status

This is **v1 of this policy** and is deliberately **human-override**-first. The
dispute path in particular **will iterate** with feedback from the first external
contributors.
