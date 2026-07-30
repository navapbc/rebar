---
schema_version: 1
title: Leaf-parent containment [agent, leaf]
description: Plan-review leaf criterion G7 (AGENT). The rubric the Pass-1 finder applies;
  routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: leaf
---
LEAF-with-parent only: is the leaf's declared scope a SUBSET of its parent's plan? The parent's plan is the containing contract; the leaf may deliver PART of it (consistent narrowing), but it may NOT step outside it. This criterion maps its severity onto the existing `divergent_implementation` plan axis — a leaf diverging from its parent IS exactly that signal. That axis is graded by DIVERGENCE KIND (plan-v4), so map onto the grade that matches the divergence: a leaf that steps OUTSIDE the parent's contract (delivering work the parent's plan does not contain) is `contradicts_reality`; a leaf whose scope omits part of the parent's contract that must be delivered for the parent's goal to hold is `omits_required_site`; a merely cosmetic mismatch in wording or an optional mention is `incomplete_enumeration` (coached, never auto-blocking). Do NOT grade this axis `low`/`medium`/`high` — those are not valid values for it.

FETCH THE PARENT. The parent's id (`parent_id`) is provided in the ticket-graph context. Call `show_ticket(<parent_id>)` to read the parent's plan (its What/Scope/Acceptance Criteria). Optionally also read the grandparent (`show_ticket(<grandparent_id>)`) when the parent is thin and the real contract lives one level up.

FIRE A FINDING when the leaf is NOT a subset of the parent — specifically when the leaf:
- (a) delivers something the parent's plan does not contain, or that the parent implies is out of scope;
- (b) contradicts a parent acceptance criterion; or
- (c) redefines a deliverable the parent specifies differently.
Consistent NARROWING — a leaf that does PART of what the parent describes, faithfully and without contradiction — is NOT a finding.

FLOOR-KIND GUIDANCE (blocking scope — ticket 28d5): ONLY a genuine contract contradiction or a provably required omission is a floor kind. Grade `contradicts_reality` only when the leaf demonstrably steps OUTSIDE the parent's contract, contradicts a parent acceptance criterion, or redefines a parent deliverable; grade `omits_required_site` only when the parent's goal provably CANNOT hold without the omitted scope AND no sibling ticket covers it. A cosmetic or wording-level mismatch, an optional mention, or an omission whose absence leaves the parent's goal intact is `incomplete_enumeration` — coached, never auto-blocked.

CLOSED PARENT: when the parent is CLOSED, check the leaf against the parent's FINAL (as-closed) state — its contract as it actually ended, including any recorded scope decisions — not against an earlier draft of it. Scope the parent explicitly handed to sibling tickets is NOT a leaf violation: a leaf omitting work the parent assigned elsewhere is consistent narrowing, not divergence.

DEDUP-AT-SOURCE: emit at most ONE finding per contradicted parent clause. When several leaf statements collide with the SAME parent clause, fold the additional manifestations into that single finding's evidence rather than emitting one finding each; distinct findings are justified only by DISTINCT contradicted parent clauses.

FAIL-OPEN (abstain-with-coverage): if the parent cannot be resolved — `show_ticket(<parent_id>)` errors, returns nothing, or the parent's plan is unreadable — ABSTAIN: record the parent as covered-but-unverified and emit NO finding. An unresolvable parent is a tooling/visibility gap, never evidence of divergence — it is never a finding. Fail open, never fail closed on an unresolvable parent.

CONFLICT RULE — the PARENT WINS. On any conflict between the leaf and the parent, the parent's plan is authoritative. The productive move is to realign the leaf to the parent. If you believe the parent is genuinely wrong, do NOT silently diverge the leaf — instead update the parent first (which stales the parent's own plan-review attestation and forces its re-review), and only then re-review the leaf against the corrected parent. Realigning the leaf to a subset of the parent, or updating the parent, are the only acceptable resolutions.
