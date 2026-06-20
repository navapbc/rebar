import json, os
TMP = os.path.expanduser('~/.claude/jobs/3ef65161/tmp')
c = json.load(open(os.path.join(TMP, 'criteria_v5.json')))
# Bring the AGENT-tier descriptors (previously only in the agentic harness) into the registry JSON
agent_tier = [
 {"id":"G1G2","exec":"AGENT","facet":"codebase-grounding","routing":"leaf",
  "name":"Edit-set / scope accuracy [agent]",
  "scenario":"Verify (via Glob/Grep) that every file/symbol the plan names actually exists; enumerate consumers/callers OUTSIDE the artifact's dir that a change would require updating; flag hallucinated/missing edit targets and unenumerated consumers; classify behavioral hunks in/ambiguous/out-of-scope (CREATION=new behavior->out-of-scope). High blast-radius alone is not a fail if acknowledged. ANTI-FP: report only high-confidence; STOP if scope too vague."},
 {"id":"E4","exec":"AGENT","facet":"codebase-grounding","routing":"leaf",
  "name":"Assumption/premise verification [agent]",
  "scenario":"Scan the plan for assertions about the codebase ('X already exists', 'Y does Z', hedges/confident-assertions) and FORCE a Grep/Read probe per assertion; cached/training knowledge is not a substitute. Fail-closed on absent evidence (unverifiable assertion = gap). ANTI-FP: read the named implementation file before flagging a contract-doc-only claim."},
 {"id":"A1","exec":"AGENT","facet":"codebase-grounding","routing":"leaf",
  "name":"Anti-slop / over-engineering / NIH [agent]",
  "scenario":"For each proposed abstraction/dependency/config, Grep the codebase to check: Rule-of-Three (>=3 existing call-sites or it's premature); YAGNI (serves a current done-definition, not a hypothetical); NIH (doesn't rebuild functionality already in the codebase or an imported dependency); no config-surface proliferation. Every finding cites concrete codebase evidence. ANTI-FP: Justified-Complexity needs affirmative evidence, not absence-of-disqualifier."},
 {"id":"G3","exec":"AGENT","facet":"container","routing":"container",
  "name":"Child coverage [agent, container]",
  "scenario":"CONTAINER-only (has_children): does the union of children cover the parent's acceptance/success criteria? 4-bucket audit per criterion (fully / partially / uncovered / structural) + a coverage map; an uncovered parent criterion is a finding. ANTI-FP: a criterion covered-by-definition by a named consumer counts."},
 {"id":"G4","exec":"AGENT","facet":"container","routing":"container",
  "name":"Child consistency [agent, container]",
  "scenario":"CONTAINER-only (has_children): check the 7 cross-child interaction modes — implicit shared state, conflicting assumptions, dependency gap, scope overlap, ordering violation, consumer impact, residual references. Each detected mode is a finding. ANTI-FP: high-confidence only; benign-reading filter."},
]
have0 = {x['id'] for x in c}
for a in agent_tier:
    if a['id'] not in have0:
        c.append(a)
byid = {x['id']: x for x in c}

# ---------- ROLL-INs: extend existing criterion scenarios (append a tight sub-check) ----------
rollins = {
 'A1': " ALSO screen the full anti-pattern set (DSO decider): golden-hammer (one tool/pattern forced everywhere), cargo-cult (copied without understanding why), resume-driven (trendy tech with no requirement), premature-optimization (optimizing before evidence), in addition to NIH, premature-abstraction/Rule-of-Three, and config-surface-proliferation.",
 'T9': " ALSO assess CONCURRENCY SAFETY (distinct from lifecycle completeness): is shared/mutable state mutated atomically (lock/CAS/transaction, no check-then-act TOCTOU), and is the operation idempotent under retry / at-least-once delivery? A fully-specified lifecycle can still have a race.",
 'T5a': " ALSO assess COST/economics (not just latency): per-call $ (e.g. an LLM/embedding call per item), egress, always-on vs serverless, unbounded fan-out — a design can be fast and ruinously expensive.",
 'T5b': " ALSO check OBSERVABILITY (are new failure points instrumented with a metric/log/trace/alert so operators can see and debug them?) and DEPENDENCY-FAILURE blast radius (when a hard external dep is down/slow: timeout, circuit-breaker, fallback/degraded-mode, or does the feature — or an unrelated one — go down?).",
 'T5c': " ALSO (broaden trigger to any plan that introduces a credential/role/grant/secret) check LEAST-PRIVILEGE (grants scoped to the minimum; no wildcard *:* / admin-for-convenience) and SECRET LIFECYCLE (no plaintext secrets in code/IaC/logs; use a secrets manager).",
 'E5': " ALSO flag the SELF-AUTHORED-ORACLE / change-detector anti-pattern: tests that merely assert the current (possibly wrong) behavior, tautological tests, or source-greps masquerading as behavioral tests — these lock in the bug instead of exposing it (DSO test-quality: source-grep & tautology = always-critical).",
 'T4': " The REMEDY for a destructive/breaking change is an explicit ROLLBACK / back-out plan or expand-contract sequencing — checking only that breakage is *acknowledged* is insufficient; require the reversibility mechanism.",
}
for cid, add in rollins.items():
    byid[cid]['scenario'] = byid[cid]['scenario'].rstrip() + add

# G5: consume P4 + add vertical-slice/MVP sequencing (mis-tier + critical-review #5)
byid['G5']['scenario'] = byid['G5']['scenario'].rstrip() + (" Consume the DET P4 oversize signal and the resolved edit-set rather than re-deriving file/layer counts from prose. ALSO judge SEQUENCING: is there a thin vertical-slice / evidence-gated MVP that de-risks the riskiest piece first, or is it a horizontal big-bang? (Decomposing into many parallel parts does not by itself reduce big-bang risk.)")

# ---------- NEW criteria (distinct, valuable, generic) ----------
new = [
 {"id":"G6","exec":"AGENT","facet":"approach-soundness","routing":"overlay",
  "trigger":"a real design choice exists: novel/complex approach, multiple viable mechanisms, or an irreversible/high-blast decision (LLM-routed; skip mechanical tickets)",
  "name":"Approach soundness, anti-patterns & alternative-selection [overlay]",
  "scenario":("Judge whether the plan's chosen APPROACH is sound and the best available — the defect a well-formed plan can still have. "
   "(1) MECHANISM CORRECTNESS: reason through whether the proposed mechanism actually achieves the goal — logic/data-flow complete, edge/empty/concurrent/failure cases handled, no hidden ordering/atomicity assumption that breaks (e.g. a check-then-act idempotency that is really a TOCTOU race). "
   "(2) FITNESS-FOR-PURPOSE: does this solution actually solve the named problem (not a proxy)? "
   "(3) APPROACH SELECTION (alternatives WITHOUT negative priming): YOU (the reviewer) generate 1-2 plausible alternative approaches that differ structurally (data-layer / control-flow / dependency-graph / interface-boundary) and judge whether the plan's chosen approach is defensibly at-least-as-good on codebase-alignment, blast-radius, testability, simplicity, robustness. If defensible -> PASS and DISCARD your generated alternatives (never write them into the plan). If a clearly-superior alternative was missed -> a FINDING coaching the PLANNER to adopt the better approach ('consider X because Y') — the implementer's plan still contains only ONE approach. "
   "(4) Confirm the plan states a POSITIVE rationale for the chosen approach (why it fits) — its ABSENCE is a finding; do NOT require a rejected-alternatives section (that primes implementers with rejected behavior). "
   "SEVERITY: a mechanism that won't work or a clearly-wrong approach = CRITICAL (agent builds the wrong thing); a missed clearly-better alternative = MAJOR; missing positive rationale = MINOR. "
   "ANTI-FP: mechanical/well-understood changes have no real design choice -> PASS not-applicable; do not manufacture alternatives for a forced solution; ground correctness reasoning in the actual code via the tools.")},
 {"id":"T10","exec":"1-TURN","facet":"overlay-infra","routing":"overlay",
  "trigger":"infrastructure/IaC signals (deterministic, low-FP): terraform|.tf|tfvars|cloudformation|cdk|pulumi|ansible|kubernetes|k8s|helm|IAM|VPC|security group|provision|S3|RDS|EC2|lambda|aws_|gcp|azure",
  "name":"Infrastructure / IaC [overlay]",
  "scenario":("OVERLAY — apply only when the plan provisions or configures infrastructure (cloud resources, IaC: Terraform/CloudFormation/CDK/Pulumi/Ansible, Kubernetes/Helm); else PASS not-applicable. Binary checks: "
   "(a) STATE: remote state + locking (no local state); plan-before-apply discipline. "
   "(b) LEAST-PRIVILEGE IAM: roles/policies scoped to the minimum, no wildcard `*:*`/admin-for-convenience, no long-lived credentials committed. "
   "(c) IDEMPOTENCY & DRIFT: changes are idempotent; drift / out-of-band manual changes considered. "
   "(d) BLAST RADIUS & ENV ISOLATION: dev/stage/prod separation; destroy/replace safety — does an apply risk data loss (RDS deletion, S3 force-destroy, instance/volume replacement)? `prevent_destroy` on stateful resources? "
   "(e) SECRETS: no plaintext secrets in IaC/vars; use a secrets manager / SSM / vault. "
   "(f) COST & SIZING: obviously-expensive or unbounded resources flagged; limits/autoscaling/quotas considered. "
   "(g) OBSERVABILITY & OWNERSHIP: logging/metrics/alarms for new infra; the resource is reproducible (as-code) with a clear teardown. "
   "SEVERITY: a destructive apply with no safeguard, a wildcard-admin grant, or a plaintext secret = MAJOR. ANTI-FP: not-applicable for non-infra tickets; managed defaults that are documented are fine.")},
 {"id":"T11","exec":"1-TURN","facet":"overlay-migration","routing":"overlay",
  "trigger":"schema/data-shape change, migration, or backfill over persisted data (deterministic prior + LLM)",
  "name":"Data-migration / backfill safety [overlay]",
  "scenario":("OVERLAY — apply only when the plan changes a schema / persisted format or backfills data; else PASS not-applicable. This is migration-EXECUTION safety (distinct from T4 which is breakage-acknowledgement). Binary checks: "
   "(a) ONLINE / EXPAND-CONTRACT: the migration runs without downtime and via expand-contract (add nullable -> backfill -> enforce), not a single blocking DDL that locks a large table. "
   "(b) BATCHING & SCALE: large backfills are batched/throttled, not one giant transaction. "
   "(c) RESUMABILITY: a partially-completed migration is resumable/idempotent (re-runnable without double-applying). "
   "(d) DUAL-WRITE WINDOW: rows written DURING the migration are handled (no lost writes between backfill and cutover). "
   "(e) ROLLBACK / DATA-LOSS: there is a back-out path and data loss is impossible on partial failure. "
   "SEVERITY: an irreversible single-shot migration with no rollback, or a long blocking lock on a large table = MAJOR. ANTI-FP: not-applicable for non-persisted/in-memory changes.")},
 {"id":"T12","exec":"1-TURN","facet":"overlay-rollout","routing":"overlay",
  "trigger":"changes runtime behavior of a deployed/long-running system (service, pipeline, agent) (LLM-routed; PASS-N/A for libraries/CLIs)",
  "name":"Rollout / rollback / reversibility [overlay]",
  "scenario":("OVERLAY — apply only when the plan changes the runtime behavior of a deployed or long-running system; else PASS not-applicable (e.g. a library/CLI with no deploy surface). Binary checks: "
   "(a) STAGED ROLLOUT: a behavior change reaches production via a flag / canary / staged rollout, not a single 100%-traffic flip. "
   "(b) ROLLBACK: there is an explicit, cheap, tested way to undo the change quickly without data cleanup. "
   "(c) DEPLOY ORDERING: if producers/consumers or coordinated services change, the deploy order (and coexistence of old+new during rollout) is specified. "
   "SEVERITY: a one-shot behavior change to all traffic with no flag and no rollback path = MAJOR. ANTI-FP: not-applicable for non-deployed code; an internal-only change with trivial revert is fine.")},
]
for n in new:
    if n['id'] not in byid:
        c.append(n)

json.dump(c, open(os.path.join(TMP, 'criteria_v6.json'), 'w'), indent=1)
print('criteria_v6:', len(c), 'descriptors')
print('  new:', [n['id'] for n in new])
print('  rolled-in (scenario extended):', list(rollins.keys()) + ['G5'])
print('  ids:', [x['id'] for x in c])
