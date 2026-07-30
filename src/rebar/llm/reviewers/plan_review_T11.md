---
schema_version: 1
title: Data-migration / backfill safety [overlay]
description: Plan-review overlay-migration criterion T11 (AGENT). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: overlay-migration
---
OVERLAY — apply only when the plan changes a schema / persisted format or backfills data; else PASS not-applicable. This is migration-EXECUTION safety (distinct from T4 which is breakage-acknowledgement). Binary checks: (a) ONLINE / EXPAND-CONTRACT: the migration runs without downtime and via expand-contract (add nullable -> backfill -> enforce), not a single blocking DDL that locks a large table. (b) BATCHING & SCALE: large backfills are batched/throttled, not one giant transaction. (c) RESUMABILITY: a partially-completed migration is resumable/idempotent (re-runnable without double-applying). (d) DUAL-WRITE WINDOW: rows written DURING the migration are handled (no lost writes between backfill and cutover). (e) ROLLBACK / DATA-LOSS: there is a back-out path and data loss is impossible on partial failure. SEVERITY: an irreversible single-shot migration with no rollback, or a long blocking lock on a large table = MAJOR. ANTI-FP: not-applicable for non-persisted/in-memory changes.

POST-ROLLBACK STATE — APPLICABILITY: raise post-rollback-state findings ONLY when the plan (a) changes how existing persisted records are interpreted, (b) rewrites or migrates records in place, (c) removes or narrows a read path, or (d) performs a one-way transformation with no back-out. Under one of those conditions, the plan must assert on the state AFTER rollback, not merely that rollback completed — a back-out can report success while leaving persisted state incorrectly interpreted or partially transformed; where destructiveness is ambiguous, default to compensating-forward and attach a literal `DATA LOSS RISK` note rather than assuming reversibility. ANTI-FP: purely additive, backward-compatible persistence changes that existing readers ignore (new record/event types, optional fields, new tables or columns with defaults) do NOT qualify — residual data after rollback is the norm in append-only and backward-compatible systems, not a risk.
