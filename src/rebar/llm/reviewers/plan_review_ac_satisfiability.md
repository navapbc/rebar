---
schema_version: 1
title: Acceptance-criteria joint satisfiability
description: Plan-review coherence criterion ac-satisfiability (1-TURN). The rubric
  the Pass-1 finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: coherence
---
JOINT SATISFIABILITY of the ticket's OWN commitments: is there any end state in which ALL of this plan's acceptance criteria AND its Scope / Out-of-Scope declarations are simultaneously true? This is NOT COH (which scans for contradictions BETWEEN sections and disclaims within-section ones) and NOT E1 (criterion↔description mapping, terminology, duplicates) and NOT F1 (is one criterion measurable): those all pass on a criteria set that is individually well-formed but collectively impossible. A plan whose criteria cannot all hold is structurally guaranteed to fail the completion verifier, so the cost of missing it is paid after the code is written.

THE PRIMITIVE — nearly every instance has one shape: an acceptance criterion QUANTIFIES OVER A SET, and something else in the same description places a member INSIDE the excepted region. Quantification is either UNIVERSAL ("no file under X matches P", "all callers updated", "zero errors") or CARDINAL ("the 108 items are migrated", "all three endpoints"). So for EACH criterion that quantifies over a set: name the set, then hunt the SAME description for anything that exempts, defers, preserves, or excludes a member of it. A hit is a finding — quote BOTH halves.

THREE SHAPES to check explicitly:
- UNIVERSAL-vs-CARVE-OUT — a criterion demands a property hold across a set while this ticket's Scope, an Out-of-Scope / deferred / "explicitly excluded" clause, or ANOTHER of its own criteria keeps a member of that set in the violating state. Both halves sit in the one document you were given, so this needs no outside knowledge. The commonest form: one criterion demands zero occurrences in a container, another demands a specific occurrence inside that same container survive.
- DERIVED-ARTIFACT CLOSURE — a criterion asserts a property of an artifact the plan says is GENERATED / regenerated / derived from some source, while the plan leaves that source carrying the negation of the property. Regenerating from a source that contains X cannot yield an artifact free of X. Assert the property of the SOURCE too, or the criterion is unsatisfiable by construction. Fires for generated docs, indexes, lockfiles, snapshots, compiled bundles — any derived output.
- SNAPSHOT CARDINALITY — a criterion pins a literal count or a fixed enumeration of a set whose membership the plan says is DISCOVERED, computed, or recomputed at execution time. The number was measured when the plan was written and drifts before the work runs, so a literal reading can become unsatisfiable through no fault of the implementer. The productive fix is to state the invariant ("every item matching P is migrated") instead of the census.

SEVERITY: two criteria that cannot both hold, or a criterion its own Scope makes impossible, send the implementer in two directions and guarantee a failed close = MAJOR. A drifting count is MINOR when the plan elsewhere states the recompute rule and MAJOR when the count is the only stated target.

ANTI-FP — the safe direction is SILENCE. Require the contradiction to be demonstrable from QUOTED text of this description alone; if you must assume facts about the codebase, or reason about a sibling ticket you were not given, do NOT fire (cross-ticket coverage is G3/G4's concern, parent containment is G7's). A criterion that NARROWS another (a subset, a stricter bound, a staged rollout) is consistent, not contradictory. An explicit exception a criterion ITSELF names ("no matches except the frozen fixtures") is satisfiable and not a finding — the defect is an exception stated ELSEWHERE that the criterion does not admit. Sequential criteria describing different points in time are not contradictory. Judge by MEANING, not wording; when in doubt whether two commitments truly cannot co-hold, emit nothing. PASS when every criterion and the declared scope can hold at once.
