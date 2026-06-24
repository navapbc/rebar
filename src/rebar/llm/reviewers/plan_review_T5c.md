---
schema_version: 1
title: Security (overlay)
description: Plan-review overlay-security criterion T5c (AGENT). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: overlay-security
---
OVERLAY — apply ONLY if the plan actually adds a security surface in THIS application's domain: a new endpoint, network exposure, an authn/authz boundary, storage/transmission of sensitive data, PII, or a credential/secret/grant. If the application has no such surface (e.g. a local library / CLI / git-backed tool with no network or auth), PASS as not-applicable. DERIVE the security model from the application's ACTUAL domain — do NOT import generic web-app concepts (e.g. a 'declared access level', endpoint authn) that this application does not have; a finding that imposes a security requirement the application's domain does not contain is a FALSE POSITIVE, not a gap. Where a real surface exists, check (OWASP only where the category applies): (a) sensitive paths use the app's own auth mechanism; (b) data protection — encryption at rest/in transit where data is actually stored/transmitted; (c) LEAST-PRIVILEGE on any new credential/role/grant (no wildcard / admin-for-convenience); (d) SECRET LIFECYCLE — no plaintext secrets in code/IaC/logs; use a secrets manager. ANTI-FP: do NOT flag 'leakage' of data that is ALREADY in the ticket/repo — review findings that also live in the repo leak nothing; secrets sitting in tickets/the repo are an UPSTREAM concern, not this review's. SEVERITY priors: an undeclared sensitive surface or a plaintext secret is high. PASS if the application's actual security boundaries are explicit and sound.
