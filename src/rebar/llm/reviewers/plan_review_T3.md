---
schema_version: 1
title: Integration feasibility [overlay]
description: Plan-review overlay-feasibility criterion T3 (AGENT). The rubric the
  Pass-1 finder applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: overlay-feasibility
---
OVERLAY — apply only when the plan integrates an external API/CLI/service/library or asserts a capability it has not used before; else PASS not-applicable. Binary checks (tool-grounded where possible): (a) technical_feasibility — is the integration achievable as described, or is there a capability gap? (b) for a CLI/API: do the named subcommands/endpoints actually exist (verify against --help / docs) — MATCH / MISMATCH / UNVERIFIED; (c) auth/HTTPS preconditions stated; (d) a critical capability gap should route to a SPIKE before committing the full plan. SEVERITY: an asserted-but-unverified external capability the plan depends on = MAJOR. ANTI-FP: verify before asserting a mismatch; an internal, already-used integration is not-applicable.
