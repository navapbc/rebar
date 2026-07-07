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

EMPIRICISM DEPTH — three axes: (a) per-command --help / flag-level empiricism — OBSERVE flag names from --help, do not infer them from memory or a prior version (flag-level mismatches like --label vs --labels cause silent runtime failures after ship); (b) endpoint granularity — the unit is a specific endpoint SURFACE, not the vendor: a new endpoint on an already-used vendor is a NEW integration (different path, possibly different OAuth scopes / rate limits); (c) environment preconditions — a platform-capability probe (an HTTP-only environment is a CONTRADICTED signal for any OAuth-callback flow regardless of API-capability verification). Do NOT mark a signal verified on general knowledge alone; if you recall a URL from training, treat it as unverified until confirmed. CLASSIFY each signal into one of FOUR EVIDENCE CLASSES, recorded in the Pass-1 evidence[] for Pass-2 to read: Verified (observed), Partially-verified (the integration exists but the specific surface is unconfirmed), Unverified (could not probe), Contradicted (the environment falsifies it).
