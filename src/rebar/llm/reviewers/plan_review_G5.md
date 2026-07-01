---
schema_version: 1
title: Decomposition judgment
description: Plan-review scope-intent criterion G5 (1-TURN). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: scope-intent
---
Judge whether the ticket is a single COHERENT unit of work or bundles work that belongs in
separate children. COHERENCE — not raw size — is the primary axis: a large, single-concern
vertical slice is correctly ONE unit.

PRIMARY — single-concern. A unit warrants decomposition when it bundles MORE THAN ONE
independently-valuable / independently-releasable OUTCOME, carries more than one "reason to
change" (a distinct actor/persona/concern), or MIXES heterogeneous change kinds (e.g. a
bug-fix AND a new feature AND an unrelated refactor). For an epic/parent that means it should
have children; the tell is a structural 'and' joining genuinely independent goals, spanning
independent personas, or a set of unrelated success criteria.

VALUE-PRESERVATION (a decomposition finding must satisfy this to stand). A unit is right-sized
as one piece when it delivers a SINGLE increment of value whose parts would be tightly coupled,
order-dependent, or individually worthless split apart — such a unit PASSES, and keeping it
whole is correct. A finding is warranted only when the unit would divide into pieces that are
EACH independently valuable, testable, and releasable on their own.

VERTICAL SLICE, not layers. A coherent slice deliberately touches multiple architectural layers
(UI + logic + storage) and often several files — that is the SHAPE OF A GOOD UNIT, not a
decomposition trigger. Do NOT flag a unit merely for spanning layers, for touching several
files, or for introducing an interface whose consumer ships in the same unit (keep a new API
and its first caller together). Splitting one feature horizontally by layer is an anti-pattern —
each layer alone delivers no value.

WEAKER PRIORS (advisory only — never the sole basis for a finding). Genuine DIFFUSION — the work
is scattered across many UNRELATED subsystems (not merely many files within one coherent area) —
and LOW SCOPE-CERTAINTY (the unit is exploratory / its final shape is not yet known) are soft
signals worth SURFACING, but neither by itself establishes a decomposition finding; weigh them
only alongside a real single-concern violation. Surface the concern as an observation; do not
prescribe a remedy.

LEAF: a leaf is right-sized when it executes coherently in one session (and is not a
one-criterion triviality). YAGNI / Rule-of-Three: proposed structure/abstraction is justified by
the CURRENT criteria (≥3 real call-sites for any new abstraction), not a hypothetical.

SEQUENCING: judge whether a thin vertical-slice / evidence-gated MVP de-risks the riskiest piece
first, versus a horizontal big-bang (decomposing into many parallel parts does not by itself
reduce big-bang risk).

ANTI-FP: an incidental 'and' does not fail single-concern; a file/layer count is a WEAK PRIOR,
not authoritative impact; a coherent multi-layer slice PASSES. Treat any deterministic size
signal (e.g. DET P4 oversize) as a coarse prior only — the test is coherence, not counts. PASS
when the unit is a single coherent concern whose parts could not each stand alone.
