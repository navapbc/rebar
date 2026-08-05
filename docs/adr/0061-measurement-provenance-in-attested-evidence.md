# ADR 0061 — Attested evidence must declare its measurement provenance

- **Status:** Accepted (epic `3147-7b57-5263-4e5d`; story `f161-c70a-1a69-4ed1`)
- **Date:** 2026-08-04

## Context

ADR-0043 established `[operator-attested]` as a deliberate trust escape hatch: when an acceptance
criterion's "done" evidence inherently lives outside the codebase, the completion verifier accepts a
recorded attestation instead of hunting for code proof. The criteria guide states that contract
three times in the same shape — evidence is sufficient when it is "a concrete attestation recorded
on the ticket (a change id / vote outcome / timestamp)".

That contract demands provenance of the **attestation event**. It demands nothing about provenance
of the **measurement**. The gate asks *"is there an attestation?"* — never *"was the measurement
taken somewhere that could have exhibited the failure it rules out?"*

Four escapes in the `061c-ecd1-8967-4a76` run went through that hole, and three of them are one
defect wearing different clothes: **a green signal whose measurement could not have observed the
failure it claims to rule out.**

1. **Wrong environment.** Bedrock caching and model-availability measurements taken in account
   `579718921998` / `us-east-2` were recorded as settled fact for work targeting `896586841071` /
   `us-east-1` — the account named by the Terraform backend `rebar-tfstate-896586841071`. No gate
   compared the claim's observation context to the project's environment. A human caught it by
   asking "is that this project's account?"
2. **Privilege that cannot fail.** Live `bedrock:Converse` calls were offered as validation of a
   *scoped* IAM grant while running under an identity holding `bedrock:*`. Evidence generated under
   broader privilege than production is structurally unable to fail, so its success carries zero
   information.
3. **An instrument that could not fail for the reason that mattered.** The prescribed remedy —
   `aws iam simulate-principal-policy` — then produced a confident, recorded, and **wrong**
   conclusion: that `bedrock:Converse` was the action to grant. The simulator answered exactly the
   question asked ("would this policy permit the action *named* `bedrock:Converse`?") but not the
   question that mattered ("which action does the Converse API actually *check*?"). Only a real call
   from the instance revealed `AccessDeniedException ... not authorized to perform:
   bedrock:InvokeModel` — meaning the original plan was right and the "fix" broke it.

Escape 3 is the sharpest, because the *first correction was also wrong*. Swapping one instrument for
a more rigorous-sounding one does not establish that the new instrument can fail for the reason you
care about.

These are structural, not calibration misses. `verified_at_sha` pins the code world completely and
the outside world not at all. Plan-review runs *before* the evidence exists, so it cannot audit a
measurement not yet taken; the completion verifier accepts attested evidence **on trust by design**
(ADR-0043). One gate cannot audit it, the other is designed not to.

## Decision

**Amend the `[operator-attested]` evidence contract to require measurement provenance**, and enforce
it in two layers that each do only what they can ground.

An `[operator-attested]` criterion whose evidence comes from an external system must declare,
alongside the existing change-id / vote / timestamp, an indented continuation line:

```
- [ ] [operator-attested] <criterion text>
      provenance: environment=<v>; principal=<v>; privilege_posture=<production-equivalent|broader|narrower>; instrument=<live-call|simulation|static-analysis> — <justification>
```

- `environment` — the concrete identifier (AWS account id, cluster, tenant, endpoint host).
- `principal` — the identity the measurement ran as.
- `privilege_posture` — the measurement's privilege relative to production.
- `instrument` — what KIND of instrument produced the measurement. Required, and judged
  **independently** of `privilege_posture`: a simulation and a live call are not interchangeable
  evidence for an authorization claim, and privilege posture cannot tell them apart. Escape 3 is
  invisible without this field.

**Layer 1 — deterministic, zero LLM.** `src/rebar/llm/plan_review/det_measurement_provenance.py`
asserts the declaration is present and well-shaped: all four keys, non-placeholder values, enum
membership, and a justification. It makes **no** judgement about correctness. It is surfaced through
`p6_ac_quality`, which is **advisory and never blocks** — so every ticket predating this contract
keeps claiming and closing with no migration step.

**Layer 2 — an LLM criterion judging the declaration against repo facts.** Once provenance exists as
text, "the plan attests account X, the IaC names account Y" becomes a **repo-checkable
contradiction** — exactly the kind of grounded probe the gate's strongest criteria already perform.
It simply had nothing to read before. The criterion `project.measurement-provenance` ships in the
`.rebar/` project overlay at **advisory** posture per ADR-0054; corpus replay decides promotion.

## Consequences

**What this buys.** The wrong-environment escape becomes mechanically catchable. The privilege and
instrument claims become explicit, attributable, and reviewable rather than silent omissions.

**What it does not buy, stated plainly.** This closes the **undeclared** hole, not the **lying** one.
`environment` is refutable against repo facts. `principal` and `instrument` are **not**: an author
who runs under `bedrock:*` and writes `privilege_posture: production-equivalent` is not caught, and
nothing in a repo-grounded gate could catch them. That class stays scoped out by ADR-0043's trust
boundary, and this ADR **inherits that boundary deliberately** rather than claiming to close it.
What the non-refutable fields buy is narrower and still real: they convert a silent omission into a
recorded assertion a human can challenge.

**Cross-client impact: none by default.** The four shipped sites carrying the contract text —
`plan_review_F1.md`, `plan_review_E2.md`, `plan_review_E6.md`, and `coach_moves.py` (move 14) — are
left byte-identical, as is the generated, parity-gated `docs/plan-review-criteria-guide.md`. The new
criterion lives only in this project's overlay, so no other rebar client gains a criterion from this
change. Rolling the requirement into the shipped default set is a separate, later decision that
ADR-0054 corpus replay must justify first. The Layer-1 lint does ship globally, but it is advisory
coaching only and never blocks.

## Alternatives rejected

- **Making the completion verifier re-run world measurements.** Expensive, flaky, and recursive — the
  verifier's own credentials carry the same privilege-masking problem, so it would validate using the
  instrument under suspicion.
- **Treating "reported green while typecheck was red" as a gate gap.** That is reporting discipline,
  already covered by the orchestration rule that agents report gate output faithfully.
- **Wording changes to the existing contract.** The rule was followed and still failed; emphasis is
  not the defect.
