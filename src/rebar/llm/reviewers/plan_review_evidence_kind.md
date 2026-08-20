---
schema_version: 1
title: Acceptance-criterion evidence kind
description: Ground every acceptance item's completion-evidence kind without weakening
  proof.
execution_mode: agentic
category: plan-review-criterion
dimension: codebase-grounding
---

Review every acceptance-criterion checkbox independently. Determine where evidence that the
criterion is complete can truthfully live, then compare that evidence kind with the criterion's
tag. Apply this rubric to both leaf and container plans.

## Evidence kinds

`codebase-verifiable` is the safe default. Use it when completion can be proved from durable
repository state: a file, symbol, method, configuration declaration, removed configuration,
test assertion, generated artifact, or other behavior encoded in the checked-out tree.

`operator-attested` (the kind an author tags `[non-codebase]`) applies when completion is an
event or live-state outcome outside the repository snapshot: a test run completed successfully,
an AWS deployment occurred, a database was modified, a Gerrit vote landed, or an operator
performed and observed a live procedure.
Ticket evidence may attest those outcomes because the codebase cannot replay them.

Classify the completion predicate, not nearby implementation nouns. The existence of test code
does not make a completed test run codebase-verifiable. The existence of deployment code does
not make a deployment outcome codebase-verifiable. The existence of migration code does not make
a database mutation codebase-verifiable. Related repository code is context, not proof that an
outside-world event occurred.

## Grounding procedure

For every checkbox:

1. Quote the criterion's completion predicate.
2. If it appears repository-verifiable, use repository tools to locate affirmative repository
   evidence. Record an exact file path and the symbol, key, declaration, test, or generated source
   that makes this a repository fact.
3. If it describes an outside-world event or live state, confirm that satisfying the criterion
   requires observing that event/state rather than merely inspecting related code.
4. If it contains both kinds, classify it as `split-required` and ask for independently
   certifiable criteria. Assign neither evidence kind to the bundled item.
5. If repository evidence is ambiguous or cannot be located, abstain. Treat the item as
   covered-but-insufficient and emit no evidence-kind finding.

Affirmative repository evidence is the threshold for an over-tagging finding. A keyword, a
plausible filename, or a repository-adjacent subject is insufficient. Cite the exact file path
and symbol/declaration in the finding so the author can verify the classification. This fail-open
rule is asymmetric by design: an uncertain case passes this criterion rather than risking a
false positive.

## Exact tag contract

ADR 0043, as amended by ADR 0101, selects outside-the-codebase evidence only with an exact
case-insensitive tag at the start of the checkbox text: the canonical `[non-codebase]`, or the
still-accepted legacy `[operator-attested]`. Untagged criteria, `[codebase]`, and near-misses
such as `[non_codebase]` or `[operator_attested]` remain codebase-verifiable.

Emit a finding in these cases:

- An outside-world criterion is untagged or malformed. The remediation must show the exact
  canonical syntax `- [ ] [non-codebase] …`.
- A codebase-verifiable criterion carries `[non-codebase]` (or the legacy
  `[operator-attested]`) and the repository grounding threshold above is met. Explain that
  ticket comments cannot substitute for repository proof, and cite the exact file path plus
  symbol/declaration.
- One checkbox combines repository proof and outside-world proof. Label the finding
  `split-required`; quote both predicates and request two independently certifiable criteria.

Accept an untagged codebase-verifiable criterion and a criterion carrying either exact tag
(`[non-codebase]` or the legacy `[operator-attested]`). Accept genuine test run, AWS, and
database outcomes when exactly tagged even when the repository contains the tests, deployment
definitions, or migrations that enabled them. Abstain on ambiguity or missing repository
grounding.

## Finding contract

Use criterion id `evidence-kind`. Set `location` to the exact acceptance checkbox. State the
observed tag, the grounded evidence kind (or `split-required`), and the mismatch. Put repository
citations in `evidence`; for missing/malformed external tags, cite the criterion's event/live-state
predicate. Make the remediation an edit the author can apply directly.
