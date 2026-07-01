# ADR 0018: Trust-boundary crossing is the explicit scope gate for the T5c plan-review security overlay

- **Status:** Accepted
- **Context:** Task *Trust-boundary framing for the T5c plan-review security overlay*
  (`2e89`, `wig-grove-eye`), discovered from the container/leaf scrutiny bug (`a278`,
  ADR 0016) and the AWS-hosted Gerrit epic regression (`d251`). Relates to the T10 infra
  overlay (ADR 0012) and the unified criteria registry (ADR 0017).

## Context

The plan-review security overlay **T5c** decides whether a plan introduces a security
concern worth flagging. Its long-standing hazard is the *false positive*: rebar itself is
a local, git-backed, in-process library/CLI, so a security rubric that reasons by
*category* ("is there auth? is there encryption?") fires on work that has no attacker to
defend against, drowning real findings in noise.

The overlay already leaned on the right idea — it PASSed local/in-process work as
not-applicable — but only *implicitly*, as an anti-FP footnote. Research on how mature
security / LLM-review systems avoid false positives converges on one scoping principle:
**trust-boundary crossing**. A security concern is real only when a component is
*reachable by a lower-trust actor*. This is the shared spine of STRIDE (Spoofing ↔ authn
at a boundary), OWASP ASVS enforcement points (§1.4.x), and the Semgrep/Anthropic
"exploitability over category" posture: a category alone is never the finding; a crossed
boundary is.

Leaving the principle implicit had two costs: (a) it was applied unevenly — mostly to
auth, not to encryption / least-privilege / secret-lifecycle — and (b) the reachability
judgement was ad hoc, so mixed-scope and reachability-silent plans were scored
inconsistently between runs.

## Decision

Make trust-boundary crossing a **first-class, explicit scope gate** at the head of the
T5c rubric (`src/rebar/llm/reviewers/plan_review_T5c.md`), mirrored in the T5c routing
checklist (`src/rebar/llm/plan_review/criteria_routing.json`, new leading `trust_boundary`
key):

1. **The gate, applied first.** A concern is in scope ONLY when the plan exposes a
   component reachable by a lower-trust actor (public internet, another tenant, an
   untrusted network, an unauthenticated user). The boundary is derived from the
   application's *actual* domain — importing a generic web-app requirement the domain does
   not have is a false positive, not a gap.
2. **Generalised across dimensions.** The same gate governs T5c's four dimensions
   (authn/authz, encryption-in-transit, least-privilege, secret-lifecycle). Each fires
   only at the point a lower-trust actor can reach the surface — not merely because the
   category is present.
3. **Positive-pass carve-out preserved.** A pure library / in-process module / single-user
   local CLI / loopback-only surface crosses no boundary → PASS not-applicable, no auth
   demanded. This keeps rebar's own work false-positive-free.
4. **Mixed-scope rule.** When a plan introduces both a boundary-crossing surface and
   purely local/in-process components, the sub-checks apply *only* to the boundary-crossing
   components; the in-process parts stay not-applicable even though the plan as a whole
   opened the gate.
5. **Ambiguous-reachability fallback.** If the plan is silent on network-reachability,
   treat the component as not-applicable (deny-to-fire, not deny-by-default) and note the
   assumption — silence means out of scope, not a gap.
6. **Zero-trust caveat.** A single-tenant / private-network deployment is *not exempt* — a
   boundary still exists at reduced blast radius — so it is raised at LOWER severity
   (advisory), not passed silently. Severity is graded via the rubric's severity priors;
   the overlay does not set a severity field directly (that is computed downstream).

## Alternatives considered

- **Component-type heuristics** (classify each component as "network" / "local" by
  keywords). Rejected: brittle, and re-introduces the category-over-exploitability failure
  the gate exists to remove.
- **Keep the principle implicit** (status quo). Rejected: it was applied unevenly and left
  the reachability judgement non-deterministic on mixed-scope / silent plans.
- **Fold the gate into T10 as well** (one boundary framing across both overlays).
  Rejected — see below.

## Why T10 is explicitly excluded (no blurring)

The infra overlay **T10** (`endpoint_access_contract`) checks a different thing:
*contract completeness* for infrastructure a plan stands up — that every network-reachable
service names how humans/admins authenticate. That is an infra-provisioning concern, LLM-
routed on infra intent so it stays FP-safe on non-infra tickets. Re-scoping it to the
general trust-boundary framing would blur two distinct checks and risk regressing the
`a278`/`d251` fix. T10 keeps its `overlay-infra` facet, its infra-intent trigger, and its
`endpoint_access_contract` key unchanged; a unit test
(`test_t10_not_reframed_by_trust_boundary_generalisation`) locks this in.

## Consequences

- T5c reasons about *reachability*, not *category*, uniformly across its dimensions —
  fewer false positives on rebar's own local work, sharper findings on genuinely exposed
  surfaces.
- The rubric and the routing checklist stay coherent (both carry the gate), so the
  `validate-routing` parity gate and the criteria-overlay consumers see one framing.
- Behavioural calibration (old-vs-new on a networked-service positive and a local-no-network
  negative) is recorded in `docs/calibration/T5c_trust_boundary.md`; because the changed
  artifact is a prompt, that calibration must be a *live* LLM run to be meaningful.
