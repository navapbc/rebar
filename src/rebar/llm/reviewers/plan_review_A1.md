---
schema_version: 1
title: Anti-slop / over-engineering / NIH [agent]
description: Plan-review codebase-grounding criterion A1 (AGENT). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: codebase-grounding
---
For each proposed abstraction/dependency/config, Grep the codebase to check: Rule-of-Three (>=3 existing call-sites or it's premature); YAGNI (serves a current done-definition, not a hypothetical); NIH (doesn't rebuild functionality already in the codebase or an imported dependency); no config-surface proliferation. Every finding cites concrete codebase evidence. ANTI-FP: Justified-Complexity needs affirmative evidence, not absence-of-disqualifier. ALSO screen the full anti-pattern set (DSO decider): golden-hammer (one tool/pattern forced everywhere), cargo-cult (copied without understanding why), resume-driven (trendy tech with no requirement), premature-optimization (optimizing before evidence), in addition to NIH, premature-abstraction/Rule-of-Three, and config-surface-proliferation. ANTI-FP (designated experiment/reference, FP7): before flagging rebuild-vs-extend NIH, check whether the "existing" implementation is a designated experiment/POC/reference — signals: a path under docs/experiments/, a *_poc.* name, or an explicit "reference, not deliverable / POC" designation in the plan or its linked brainstorm. If so, rebuilding it for production is the INTENDED lifecycle — do NOT flag NIH. When production-vs-reference status is ambiguous from location, COACH ("confirm whether the existing impl is production-to-extend or a reference-to-rebuild") rather than ASSERT NIH.
