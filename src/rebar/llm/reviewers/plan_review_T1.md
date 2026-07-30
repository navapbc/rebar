---
schema_version: 1
title: Prior-art / novel-architecture justification [overlay]
description: Plan-review overlay-priorart criterion T1 (AGENT). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: overlay-priorart
---
OVERLAY — apply when the plan crosses a bright-line (external integration, unfamiliar dependency, security/auth, a novel architectural pattern, a performance/scalability target, or a migration). Tool-grounded where possible. A server-side web-search tool MAY be offered on this run (it is provider-dependent, available only when this criterion's routing declares it and the provider supports it): when the web-search tool is offered, USE it to ground prior-art claims in real, current sources; when it is not available, fall back to codebase and plan-text reasoning (repo file tools + your own knowledge) and do NOT fabricate web citations. Checks: (a) is there relevant PRIOR ART the plan should consider before committing, or is it reinventing/repackaging something that exists? (b) for a novel pattern: is the novelty justified vs an established approach (anti-repackaging, Rule-of-Three)? (c) are unverified capability assertions ('library supports X') resolved? SEVERITY: a novel architecture chosen with no consideration of prior art = MAJOR. ANTI-FP: a well-trodden pattern needs no prior-art search; not-applicable when no bright-line fires.
