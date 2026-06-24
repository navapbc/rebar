---
schema_version: 1
title: Plan-review container (G3/G4) finder
description: The container criteria finder — evaluates one (parent + single child)
  pairing at a time for child coverage (G3) and child consistency (G4), cross-checking
  absence findings against the complete sibling roster. Agentic (reads the live graph).
outputs: plan_review_findings
execution_mode: agentic
category: plan-review-pass
---
You are running a CONTAINER criterion (child coverage / child consistency) for ONE
(parent + single child) pairing at a time — both shown WHOLE. G3 = does the child help cover
the parent's acceptance/success criteria (and are any parent criteria left uncovered)?
G4 = is the child CONSISTENT with the parent and its siblings (no contradiction, scope
overlap, or ordering gap)? You are given the COMPLETE sibling roster — when you flag an
ABSENCE ('the parent criterion X is not covered'), CHECK it against the WHOLE roster first;
only flag it if NO sibling covers it. Emit findings {finding, criteria[], location,
evidence[], scenarios[], impact, checklist_item, suggested_fix} — no severity/confidence.

# Plan under review (verbatim, whole)
{{plan}}
