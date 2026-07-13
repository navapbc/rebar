---
schema_version: 1
title: Leaf-parent containment [agent, leaf]
description: Plan-review leaf criterion G7 (AGENT). The rubric the Pass-1 finder applies;
  routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: leaf
---
LEAF-with-parent only: is the leaf's declared scope a SUBSET of its parent's plan? The parent's plan is the containing contract; the leaf may deliver PART of it (consistent narrowing), but it may NOT step outside it. This criterion maps its severity onto the existing `divergent_implementation` plan axis — a leaf diverging from its parent IS exactly that signal.

FETCH THE PARENT. The parent's id (`parent_id`) is provided in the ticket-graph context. Call `show_ticket(<parent_id>)` to read the parent's plan (its What/Scope/Success Criteria/Acceptance Criteria). Optionally also read the grandparent (`show_ticket(<grandparent_id>)`) when the parent is thin and the real contract lives one level up.

FIRE A FINDING when the leaf is NOT a subset of the parent — specifically when the leaf:
- (a) delivers something the parent's plan does not contain, or that the parent implies is out of scope;
- (b) contradicts a parent acceptance/success criterion; or
- (c) redefines a deliverable the parent specifies differently.
Consistent NARROWING — a leaf that does PART of what the parent describes, faithfully and without contradiction — is NOT a finding.

CONFLICT RULE — the PARENT WINS. On any conflict between the leaf and the parent, the parent's plan is authoritative. The productive move is to realign the leaf to the parent. If you believe the parent is genuinely wrong, do NOT silently diverge the leaf — instead update the parent first (which stales the parent's own plan-review attestation and forces its re-review), and only then re-review the leaf against the corrected parent. Realigning the leaf to a subset of the parent, or updating the parent, are the only acceptable resolutions.
