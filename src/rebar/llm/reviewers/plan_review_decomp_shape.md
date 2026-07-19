---
schema_version: 1
title: Decomposition-shape [agent, container]
description: Plan-review container criterion `decomp-shape` (AGENT, advisory). Audits
  the (parent, children, sibling-roster) decomposition for two SHAPE smells G3/G4
  don't target — layer-cake splits (children partitioned by architectural layer instead
  of vertical slices) and consumed-artifact-without-ordering-edge (a child consumes
  a sibling's artifact with no ordering dependency). Prior art T1 (CodePlan layer-cake
  / claude-task-master DAG / Kiro waves). Routing in criteria_routing.json. Ships
  advisory; blocking-promotion is a future dogfood + E6-order-stability-gated change.
  See docs/plan-review-gate.md.
execution_mode: agentic
category: plan-review-criterion
dimension: container
---
CONTAINER-only (has_children): audit the SHAPE of the parent's decomposition into children —
the partition axis and the producer/consumer wiring across siblings — for two smells. This is
NOT coverage (G3) and NOT the broad cross-child interaction sweep (G4); a decomposition can be
fully covering and internally consistent yet still have a bad SHAPE. ONE concern: a container's
children should be VERTICAL slices with explicit ordering for cross-child artifacts.

(A) LAYER-CAKE SPLIT — the children are partitioned by ARCHITECTURAL LAYER rather than by
vertical, independently-shippable slice. Tell-tale: one child is "the DB/schema/migration
layer", another "the service/API/business-logic layer", another "the UI/frontend/CLI layer" (or
"the tests layer") for the SAME feature — a horizontal cut. Why it is a smell: no single child
is demoable or independently valuable on its own, and all the integration risk is deferred to
whichever child lands last. Prefer vertical slices (each child delivers a thin end-to-end
capability touching whatever layers it needs). Prior art: CodePlan's layer-cake antipattern;
Kiro's "waves". A finding names the children and the layer axis they were cut along.

(B) CONSUMED-ARTIFACT-WITHOUT-ORDERING-EDGE — a child NAMES or CONSUMES a concrete artifact
that a SIBLING produces (an output file, a schema/migration, an emitted event, a new
symbol/module, an endpoint), yet there is NO ordering dependency (a `depends_on`/`blocks` link,
or an explicit "after child X" in the plan) between the producer and the consumer. Why it is a
smell: a parallel-agent scheduler (rebar's `next-batch`/`ready`) can start the consumer before
the producer exists, so the consumer builds against an artifact that isn't there yet. Prior art:
claude-task-master's explicit DAG ordering. A finding names the producer child, the consumer
child, and the shared artifact, and notes the missing ordering edge.

HOW TO GROUND: read the parent's plan and EVERY child's plan (and the sibling roster). For (A)
classify each child's slice axis and check whether the set is a horizontal layer cut. For (B)
build a small producer→consumer map over the artifacts children name, then check each
consumed-from-sibling artifact for a declared ordering dependency. Use the live ticket
dependency graph where available; when ordering must be inferred from plan text, treat an
explicit "after"/"once X lands" as a satisfied edge.

FIRE A FINDING only when a smell is clearly present:
- (A) the children are a layer-cake (horizontal) partition of one feature; or
- (B) a child consumes a sibling-produced artifact with no ordering edge between them.
Attribute the finding to the specific child(ren) via `location` ('child <id>') so per-child
attribution survives; cite the layer axis (A) or the producer/consumer/artifact triple (B).

FAIL-OPEN (anti-FP): a decomposition of one child, or of children that are genuinely
independent vertical slices with no cross-child artifact, is NOT a finding — PASS. A layer-named
child that is nonetheless an independently-shippable slice is not a layer-cake. A consumed
artifact that already carries an ordering edge (declared dependency or explicit "after") is
fine. When in doubt about independence or ordering, do NOT fire. Never fabricate a child, an
artifact, or a dependency.

CHECKLIST SUB-ANSWERS (criterion-local):
- has_container_decomposition {yes|no|insufficient} — the GATE: is the parent decomposed into
  >=2 children forming a decomposition? `no` → not-applicable → PASS.
- decomposition_shape_sound {yes|no|insufficient} — only meaningful when gated in: is the
  decomposition free of BOTH smells? A layer-cake partition or a
  consumed-artifact-without-ordering-edge is `no` (the finding); a genuinely ambiguous case is
  `insufficient` (abstain, do not fire).

ADVISORY: this criterion errs toward surfacing and coaches; it does NOT block a claim, and
advisory is its PERMANENT posture. Promotion to blocking is a future change gated on BOTH the
standing effectiveness recorder (`criterion_effectiveness.py` auto-monitors this criterion with
zero per-criterion wiring) AND E6's judge order-stability clearing floor — see the
advisory→blocking promotion gate in docs/plan-review-gate.md.
