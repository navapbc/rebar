---
schema_version: 1
title: Decomposition judgment
description: Plan-review scope-intent criterion G5 (1-TURN). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: scope-intent
---
Judge whether the ticket is right-sized or should be decomposed. Binary checks: (a) sizing signals — does it touch >3 files OR ≥3 layers OR introduce a new interface OR carry low scope-certainty? Any of these pushes toward 'too big, decompose'; (b) for an epic/parent: does it fail the single-concern test (a structural 'and' joining independent goals), span multiple personas, mix UI+backend, include a migration, or carry >6 success criteria — if so it should have children; (c) is decomposition into children present and sensible where the size demands it; (d) for a leaf: is it small enough to execute coherently in one session (not a one-criterion triviality either); (e) YAGNI/Rule-of-Three — is proposed structure justified by the current criteria, with ≥3 real call-sites for any new abstraction. SEVERITY: an epic-scale body with no children is MAJOR; a horizontal-layer split where a vertical slice is safer is MINOR. ANTI-FP: an incidental 'and' does not fail single-concern; a file list is a sample, not authoritative impact. PASS if sizing and decomposition are appropriate. Consume the DET P4 oversize signal and the resolved edit-set rather than re-deriving file/layer counts from prose. ALSO judge SEQUENCING: is there a thin vertical-slice / evidence-gated MVP that de-risks the riskiest piece first, or is it a horizontal big-bang? (Decomposing into many parallel parts does not by itself reduce big-bang risk.)
