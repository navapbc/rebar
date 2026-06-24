---
schema_version: 1
title: Infrastructure / IaC [overlay]
description: Plan-review overlay-infra criterion T10 (AGENT). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: overlay-infra
---
OVERLAY — apply only when the plan provisions or configures infrastructure (cloud resources, IaC: Terraform/CloudFormation/CDK/Pulumi/Ansible, Kubernetes/Helm); else PASS not-applicable. Binary checks: (a) STATE: remote state + locking (no local state); plan-before-apply discipline. (b) LEAST-PRIVILEGE IAM: roles/policies scoped to the minimum, no wildcard `*:*`/admin-for-convenience, no long-lived credentials committed. (c) IDEMPOTENCY & DRIFT: changes are idempotent; drift / out-of-band manual changes considered. (d) BLAST RADIUS & ENV ISOLATION: dev/stage/prod separation; destroy/replace safety — does an apply risk data loss (RDS deletion, S3 force-destroy, instance/volume replacement)? `prevent_destroy` on stateful resources? (e) SECRETS: no plaintext secrets in IaC/vars; use a secrets manager / SSM / vault. (f) COST & SIZING: obviously-expensive or unbounded resources flagged; limits/autoscaling/quotas considered. (g) OBSERVABILITY & OWNERSHIP: logging/metrics/alarms for new infra; the resource is reproducible (as-code) with a clear teardown. SEVERITY: a destructive apply with no safeguard, a wildcard-admin grant, or a plaintext secret = MAJOR. ANTI-FP: not-applicable for non-infra tickets; managed defaults that are documented are fine.
