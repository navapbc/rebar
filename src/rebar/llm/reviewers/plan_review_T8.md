---
schema_version: 1
title: LLM / prompt structural-completeness probe [overlay]
description: Plan-review overlay-llm criterion T8 (AGENT). The rubric the Pass-1 finder
  applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: overlay-llm
---
OVERLAY — apply when the plan explicitly defines an LLM/agent surface: prompts, model/reviewer/sub-agent behavior, or schemas/enums consumed or emitted by an LLM/tool structured-output contract. Probe (tool-grounded) for STRUCTURAL GAPS a generic checklist misses: (a) a schema/enum referenced but whose value vocabulary is never defined; (b) a processing protocol/decision rule referenced but not co-located with the schema that needs it; (c) a counter/state increment with ambiguous placement; (d) an unspecified fallback for an incomplete/failed sub-step; (e) instruction-locality / pink-elephant antipatterns. Use Grep/Read to confirm referenced agents/skills/enums exist and are fully specified. Report each PROVEN gap with evidence. SEVERITY: an undefined-but-referenced enum/protocol an executor needs = MAJOR. ANTI-FP: ordinary dataclasses, value objects, standard-library enums, and deterministic CLI adapters/readers remain under the non-overlay criteria; their data-shape gaps do not activate T8. Cite concrete evidence for every finding.
