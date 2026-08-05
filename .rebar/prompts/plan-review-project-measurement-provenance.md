---
schema_version: 1
title: Measurement provenance
description: Judge an [operator-attested] criterion's declared measurement provenance against repo facts — the environment it was measured in, the privilege it ran under, and the kind of instrument that produced it.
execution_mode: agentic
category: plan-review-criterion
---

Evidence produced under broader privilege, in a different environment, or by a different KIND of
instrument than the thing being tested is not evidence.

`[operator-attested]` (ADR-0043) is a deliberate trust escape hatch: the completion verifier accepts
an attestation instead of code proof. The contract demands provenance of the ATTESTATION EVENT (a
change id / vote outcome / timestamp) but nothing about provenance of the MEASUREMENT. This
criterion judges the measurement's declared provenance — which is now recorded as text on the ticket
by the deterministic floor, and is therefore something you can check against what the repository
actually says.

SCOPE. Apply ONLY to acceptance-criteria items tagged `[operator-attested]`. An untagged criterion
proves itself in the codebase and owes no measurement provenance — do not flag it. If the plan has
no `[operator-attested]` item, PASS not-applicable.

INPUT SHAPE. Each in-scope item should carry an indented continuation line:

    - [ ] [operator-attested] <criterion text>
          provenance: environment=<v>; principal=<v>; privilege_posture=<production-equivalent|broader|narrower>; instrument=<live-call|simulation|static-analysis> — <justification>

PRESENCE AND SHAPE ARE NOT YOUR JOB. A deterministic Layer-1 check already reports a missing key, a
placeholder value (`TBD`, `<account>`, empty), a non-member enum value, or a missing justification.
Do not re-report those. You judge the DECLARATION AGAINST REPOSITORY FACTS. Apply these four rules.

RULE (a) — ENVIRONMENT vs THE IN-REPO ANCHOR. Read the repository for the environment this project
actually targets: the Terraform backend and the IaC under `infra/`, deployment workflow config, and
any account/cluster/tenant identifier committed there. If the declared `environment` CONTRADICTS
that anchor, that is a finding — quote both sides (the declared value and the file+line that names
the other one). This is the escape that shipped: measurements taken in account `579718921998` /
`us-east-2` were recorded as settled fact for work whose Terraform backend names `896586841071`.
A human caught it by asking "is that this project's account?" — you are that reader.

RULE (b) — THE NO-ANCHOR FALLBACK. If the repository offers NO comparable anchor — no IaC, no
committed environment identifier, or a declared `environment` that is a non-AWS identifier (cluster,
tenant, endpoint host) with no in-repo counterpart — then produce NO finding. Absence of an anchor
is NOT a contradiction. Fire only on an actual declared-vs-repo MISMATCH; a criterion that nags
every repository without infrastructure-as-code is noise and will be turned off.

RULE (c) — PRIVILEGE POSTURE. If the criterion verifies a SCOPED permission or a restricted
capability while declaring `privilege_posture: broader`, that is a finding: evidence generated under
broader privilege than production is structurally unable to fail, so its success carries zero
information. `production-equivalent` and `narrower` are not findings. Live `bedrock:Converse` calls
were once offered as validation of a scoped IAM grant while running under an identity holding
`bedrock:*` — the call could not have failed, so it proved nothing.

RULE (d) — INSTRUMENT vs THE KIND OF CLAIM. If the criterion makes an AUTHORIZATION claim (a
permission, grant, policy, role, or access decision) and declares `instrument=simulation` or
`instrument=static-analysis`, that is a finding EVEN WHEN `privilege_posture=production-equivalent`.
A simulator answers "would this policy permit the action NAMED?" — not "which action does the API
actually CHECK?". `aws iam simulate-principal-policy` once returned a confident and WRONG conclusion
(that `bedrock:Converse` was the action to grant) which only a real from-instance call refuted, by
returning `AccessDeniedException ... not authorized to perform: bedrock:InvokeModel`. Privilege
posture cannot distinguish these two instruments — both may honestly be production-equivalent — so
this rule is the only thing that separates them. `instrument=live-call` is not a finding.

WHAT YOU CANNOT DO, AND MUST NOT PRETEND TO. You cannot observe a live cloud account, and you must
not ask for one to be observed. A declaration that is FALSE BUT PLAUSIBLE — an author who ran under
`bedrock:*` and wrote `privilege_posture: production-equivalent` — is outside your reach and outside
this contract's reach; that class is scoped out by ADR-0043's trust boundary. Judge what the
declaration says against what the repository says. Do not speculate about what was "probably" run.

ANTI-FALSE-POSITIVE. Do not flag: a declaration you merely find terse; an `environment` you cannot
match to any anchor (rule b); `privilege_posture: broader` on a criterion that is NOT verifying a
scoped capability; or `instrument=simulation` on a criterion making a NON-authorization claim
(a rendering check, a performance measurement, a schema validation). Evaluate the plan AS WRITTEN.

SEVERITY. A declared-vs-repo environment contradiction (rule a) is MAJOR. A privilege-posture or
instrument mismatch (rules c, d) is MAJOR. Anything you are unsure of is MINOR or not raised at all
— this criterion ships ADVISORY under ADR-0054 and its promotion depends on a low false-positive
rate, so precision matters more than recall. PASS when every in-scope declaration is consistent with
repository facts.
