# Dispute adjudication worksheet — E1 (`doctrinal-untruthful-vaquita` / `e95e`)

> **ADJUDICATION COMPLETE (2026-07-16).** All 30 disputes were adjudicated by the operator (a human) via AskUserQuestion. Tally: **16 TP / 14 FP**. Verdicts are folded into `adjudication_corpus.jsonl` (`final_label`) by `apply_adjudications.py`; `disputes.jsonl` is the authoritative record (`human_label` + `human_label_basis`). Four cluster verdicts (H005, H009, H016, S003) were inferred from the operator's prior rulings and explicitly confirmed. The filled `HUMAN VERDICT` lines below record each ruling.

The plan-review gate emits the same finding across many tickets, so Rater A (opus-4-8) and Rater B (sonnet-5) split on it the same way repeatedly. Findings below are **clustered** so you rule on each *distinct* dispute once; your verdict propagates to every member (all shown, so you can audit — and override a member whose plan genuinely differs).

**22 hard disputes → 22 decisions** (+ 8 soft → 8). For each cluster set **HUMAN VERDICT** to `TP`, `FP`, or `ambiguous`.

> TP/FP lens (it INVERTS by `source`). **surfaced:** `TP`=the finding names a real plan weakness (correct to surface); `FP`=spurious (noise). **dropped:** `TP`=no real defect, so hiding it was right (a good drop); `FP`=a real defect the gate buried (an escaped defect).

## Hard disputes (TP↔FP)

### `dropped` · criterion `E2`

#### H001  ·  covers 1 finding(s)

- **A (opus-4-8):** `FP` — The audit report is the sole source defining the ~9 anti-pattern tests and specific findings the executor must remediate, yet it's cited only by commit SHA with no path/URL — a real actionable gap; suppressing it hid a genuine defect.
- **B (sonnet-5):** `TP`
- **location:** `Context section, sentence: 'An external test-coverage audit (static, pinned to commit 2d1524ad8)'`  ·  **severity:** `minor`
- **finding:** The audit report that defines the full remediation scope is cited by commit SHA only ('external test-coverage audit, static, pinned to commit 2d1524ad8') with no path, URL, or file reference, leaving an executor unable to locate the specific list of anti-pattern tests and borderline cases without external investigation.
- **suggested fix:** Add a line such as: 'Audit report: `docs/audits/test-coverage-2d1524ad8.md` (or the equivalent path)' and, if that document is short, inline the ~9 anti-pattern test names directly in this plan.

  **HUMAN VERDICT (H001):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `dropped` · criterion `E6`

#### H002  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — The finding is largely spurious: the fixture is a one-line local write (as the suggested_fix itself shows) and WS1's `.ensure-applied` format is fully documented in the plan, so there is no real hard cross-workstream test dependency; suppressing this low-value concern was the right call for a droppe …
- **B (sonnet-5):** `FP`
- **location:** `Acceptance Criteria, AC6(b)`  ·  **severity:** `none`
- **finding:** AC6(b) requires a 'fresh auto-init'd worktree (WS1 wrote a current `.ensure-applied`)' to prove no warning is emitted, but WS1 is a sibling workstream whose output is not guaranteed available. No mock/fixture strategy is stated, creating a hard dependency that may block in-session completability.
- **suggested fix:** AC6(b) fixture: `(tmp_path / '.ensure-applied').write_text(json.dumps(list(registry_ids())))` — no WS1 dependency.

  **HUMAN VERDICT (H002):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

#### H003  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — The ACs are precise and mechanically verifiable; the missing explicit pytest invocation is a minor polish nit, so suppressing this low-severity finding was a correct drop.
- **B (sonnet-5):** `FP`
- **location:** `Acceptance Criteria section (all ACs)`  ·  **severity:** `none`
- **finding:** No proving command (e.g., `pytest tests/behavioral/ -v`, a specific test module path, or CI job name) is provided for any AC, making it impossible to verify done-ness mechanically without hunting through the repo.
- **suggested fix:** Add a 'How to verify' section: `pytest tests/behavioral/test_ws5.py -v  # must show 4 passing tests covering idempotency, hot-path, concurrency, no_mutate+absent`

  **HUMAN VERDICT (H003):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

#### H004  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — The ACs are truncated but the plan is exceptionally detailed and explicitly scopes the exhaustive hot-path/idempotency test suite OUT to WS5, so demanding concrete proving commands in this WS2 plan is a minor stylistic concern rather than a real defect; dropping this low-value finding was the correc …
- **B (sonnet-5):** `FP`
- **location:** `Acceptance Criteria (all five bullets)`  ·  **severity:** `minor`
- **finding:** None of the five ACs include a concrete proving command. An executor must invent all verification steps, creating risk of green-but-hollow sign-off: e.g., AC1 ('cached boolean; ≤1 file read per process') needs a unit test asserting the underlying file is opened exactly once across multiple hook invocations; AC2 needs a grep or import-graph check confirming no marker read on read-path entry points; AC3 needs a command that exercises the rate-limit sentinel file (e.g., set `.ensure-hinted` to an old mtime, invoke a mutation, assert WARNING is emitted; then re-invoke and assert no second WARNING) …

  **HUMAN VERDICT (H004):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `dropped` · criterion `G6`

#### H005  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — A severity-none rationale nitpick about not articulating single-source-of-truth motivation; the plan is coherent and actionable without it, so suppressing this low-value finding was the correct drop.
- **B (sonnet-5):** `FP`
- **location:** `Decomposition / Task 43c9-9aa5-290e-4900 description — 'Reference mechanism' paragraph`  ·  **severity:** `none`
- **finding:** The plan states why symlinks were rejected and why the same-directory companion-file pattern was adopted, but never states a positive rationale for having a shared canonical file at all rather than expanding each SKILL.md in-place with equivalent inline test-design prose. The core architectural choice — one shared source vs. three independent inline blocks — is asserted without a stated justification (e.g. maintainability, single-source-of-truth, future-skill extensibility).
- **suggested fix:** In the 'Reference mechanism' paragraph of task 43c9, prepend: 'A shared canonical file is preferred over equivalent inline prose in each SKILL.md because it provides a single source of truth — per-skill prose blocks drift independently over time (as the rebar-debug/rebar-implement TDD sections alrea …

  **HUMAN VERDICT (H005):** `TP` _(inferred-from-precedent, operator-confirmed)_   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `dropped` · criterion `T5b`

#### H006  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — This is a T5b operational-robustness concern the plan doesn't address, but it is a design/hardening suggestion about hypothetical failure modes rather than a concrete defect in the plan as written; dropping this speculative advisory was a defensible suppression.
- **B (sonnet-5):** `FP`
- **location:** `What section — 'SSM -> .env -> git credential helper on origin' and 'REBAR_SYNC_PUSH=always'`  ·  **severity:** `major`
- **finding:** The plan adds external failure points (SSM parameter fetch, git push to origin) with no error handling, retry, backoff, or graceful-degradation strategy specified for either. If SSM fetch fails at startup the credential is absent; if the git push fails mid-review the artifact is silently lost or the review gate hangs — no fallback or degraded mode is described.
- **suggested fix:** Add a startup probe that retries SSM fetch up to N times with exponential backoff and logs a clear fatal error if exhausted. For the push, treat push failure as non-fatal to the review gate (log error, emit a metric/alert) so a transient origin outage does not block code reviews; the locally-persist …

  **HUMAN VERDICT (H006):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `dropped` · criterion `T5c`

#### H007  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — The plan is a live-deployment dogfood proof whose acceptance criteria concern activation and recording the proof; credential storage mechanics for the Gerrit token are an operational/security detail outside this ticket's scope and not a defect in the plan as written, so suppressing this speculative  …
- **B (sonnet-5):** `FP`
- **location:** `Acceptance Criteria — AC 1: 'The WS6-a voter is deployed + activated'`  ·  **severity:** `none`
- **finding:** The deployment plan does not specify where or how the Gerrit API credential (used by the voter to post LLM-Review votes) is stored on the live AWS environment — no mention of Secrets Manager, Parameter Store, or equivalent; a plaintext credential in config/IaC is a risk on the boundary-crossing path.
- **suggested fix:** Add to AC 1: 'The Gerrit API token for the voter is provisioned via AWS Secrets Manager (or SSM Parameter Store SecureString) and injected at runtime — no plaintext credential in config files, environment variable literals, or IaC source.'

  **HUMAN VERDICT (H007):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `dropped` · criterion `T6`

#### H008  ·  covers 1 finding(s)

- **A (opus-4-8):** `FP` — The plan's version-lockstep job asserts version_input equals all manifest versions and fails before build, which handles malformed/mismatched inputs; but the finding raises a legitimately unaddressed gap (empty string, non-semver, already-published-tag) for user-facing input validation error states  …
- **B (sonnet-5):** `TP`
- **location:** `What — Primary control bullet ('a version input'); Acceptance Criteria — AC 1 and AC 2`  ·  **severity:** `none`
- **finding:** The plan does not address what happens when the version input provided at dispatch time is malformed (e.g., empty string, non-semver, or a version that already has a published tag) — these error states for the user-facing version input are absent.

  **HUMAN VERDICT (H008):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `dropped` · criterion `T8`

#### H009  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — The plan explicitly grounds `facet: project-invariants` as the established free-form grouping from test_criteria_overlay.py and AC1 checks it exactly; the finding's 'arbitrary/unverifiable' claim is thus spurious, making the drop correct.
- **B (sonnet-5):** `FP`
- **location:** `Approach section: 'Configure `exec: 1-TURN`, `facet: project-invariants`, `applies_at.scope: [container, leaf]`, `default_posture: blocking`, and `block_threshold: 0.9`'`  ·  **severity:** `none`
- **finding:** The plan specifies `facet: project-invariants` as a routing field value in the Approach section, treating it as a constrained vocabulary item, but `_validate_routing_entry()` in `overlay.py` does NOT validate the `facet` field at all — it is a free string. No enum of valid facet values is defined or cited anywhere. This means the plan's naming of `project-invariants` as the facet is arbitrary and unverifiable, and the AC1 test ('exact routing defaults') cannot mechanically confirm it is correct rather than e.g. `overlay-portability` or `misc`.

  **HUMAN VERDICT (H009):** `TP` _(inferred-from-precedent, operator-confirmed)_   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `E1`

#### H010  ·  covers 1 finding(s)

- **A (opus-4-8):** `FP` — The plan body references a 'Sanitization inverse & missing-prompt fallback (clears T8)' section, but the missing-prompt fallback behavior is subsumed under the load-time located-error validation which IS covered by an AC ('Routing validation: invalid overlay routing is a located error'); the concern …
- **B (sonnet-5):** `TP`
- **location:** `'Sanitization inverse & missing-prompt fallback (clears T8)' section; Acceptance Criteria list (all 9 ACs)`  ·  **severity:** `minor`
- **finding:** The 'missing-prompt fallback' behavior — that an activated project id whose prompt file is absent/malformed surfaces as a located RegistryError from load_criteria — is described in the plan body ('Sanitization inverse & missing-prompt fallback' section) but is not covered by any acceptance criterion. No AC requires verifying that a misconfigured overlay fails loudly rather than silently.
- **suggested fix:** Append to the AC list: '- [ ] An activated project criterion with a missing or malformed prompt file raises a located `RegistryError` from `load_criteria`; a test asserts no silent skip occurs.'

  **HUMAN VERDICT (H010):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `E4`

#### H011  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — The finding identifies a genuine oracle problem in AC3: if T5c.md is a plan-review-criterion file not included in the reviewer index.json, then the regeneration premise is either vacuously true or points to the wrong artifact, making the acceptance criterion misleading/non-verifiable as written.
- **B (sonnet-5):** `FP`
- **location:** `AC3: 'The derived prompt index is regenerated if the build requires it and the CI prompt-drift gate passes.'`  ·  **severity:** `critical`
- **finding:** AC3 asserts 'the derived prompt index is regenerated if the build requires it' and references a 'prompt-index drift check' as the relevant CI gate. However, plan_review_T5c.md has front-matter category: plan-review-criterion, and the derived prompt index (src/rebar/llm/reviewers/index.json) is built exclusively from files with category: review (confirmed in prompts.py and prompt_library.py). Editing only the body of T5c.md will never require or affect index.json regeneration. The true relevant CI gate for this change is the criteria_routing.json parity gate (validate_packaged_routing), which c …
- **suggested fix:** Replace AC3's oracle text with: 'Oracle: `make lint && make test` green locally; the `criteria_routing.json` parity gate (`python -m rebar.llm.plan_review.registry validate-routing`) passes. Note: `plan_review_T5c.md` is a `plan-review-criterion` file and does NOT appear in the reviewer `index.json` …

  **HUMAN VERDICT (H011):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

#### H012  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — The finding provides a specific, verifiable code-fact (SeverityAttrs and PlanSeverityAttrs are locally-scoped, re-declared rather than subclassed) that contradicts the plan's stated 'field-override collision' rationale; this is a real coherence/accuracy defect in the plan's justification, correctly  …
- **B (sonnet-5):** `FP`
- **location:** `What section — TRIGGER-LIKELIHOOD paragraph: 'deliberately NOT named `likelihood` — the base SeverityAttrs already declares `likelihood`=low|medium|high mapped by _LIKE01, so a distinct name avoids a field-override collision'`  ·  **severity:** `critical`
- **finding:** The plan's stated rationale for naming the new field `trigger_likelihood` (rather than `likelihood`) — 'the base SeverityAttrs already declares likelihood=low|medium|high mapped by _LIKE01, so a distinct name avoids a field-override collision' — is based on a false premise. Reading verify.py shows that SeverityAttrs is a locally-scoped class defined inside the verification_model() factory function, and PlanSeverityAttrs is likewise a locally-scoped class inside plan_review_verification_model() that RE-DECLARES all five base fields rather than subclassing SeverityAttrs. There is no Python inher …
- **suggested fix:** Replace 'the base SeverityAttrs already declares `likelihood`=low|medium|high mapped by _LIKE01, so a distinct name avoids a field-override collision' with 'a distinct name avoids ambiguity in the LLM prompt between the existing ordinal `likelihood` severity attribute (low/medium/high) and this new  …

  **HUMAN VERDICT (H012):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `G3`

#### H013  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — Parent AC6 defines a per-child advisory-disposition mechanism, but no child story's scope or AC is shown to carry it, so it is a genuinely undelegated parent requirement — a real coherence/delegation gap under G3.
- **B (sonnet-5):** `FP`
- **location:** `All five children — none covers parent AC6`  ·  **severity:** `none`
- **finding:** Parent AC6 ('Each child story's plan-review advisory findings are recorded with an explicit Accept/Reject disposition and a one-line rationale as a comment on the child ticket, and every Accepted finding is reflected in the child's description before that child's change receives its Gerrit votes') is not delegated to or reflected in any child story's scope or acceptance criteria. No child defines the mechanism for capturing advisory findings or enforces the disposition step.
- **suggested fix:** Append to each child's Acceptance Criteria: '- [ ] Plan-review advisory findings for this story are recorded with an explicit Accept/Reject disposition and one-line rationale as a comment on this ticket; every Accepted finding is reflected in this description before the Gerrit LLM-Review +1 / Verifi …

  **HUMAN VERDICT (H013):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `G6`

#### H014  ·  covers 1 finding(s)

- **A (opus-4-8):** `FP` — The plan's Section A/AC3 does not use the term 'impact-graded criterion' nor claim 'no non-advisory impact criterion'; it precisely states plan-review has 11 pre-existing blocking criteria and locks that the redesign adds no NEW one — already matching the suggested fix, so the finding is spurious.
- **B (sonnet-5):** `TP`
- **location:** `Section A — Permissive rollout / AC3: 'plan-review: no non-advisory impact criterion'`  ·  **severity:** `critical`
- **finding:** The permissive-rollout invariant in AC3 uses the undefined term 'impact-graded criterion' for plan-review, leading to an untestable or factually incorrect assertion. Plan-review routes ALL LLM criteria through `impact_plan`, and the routing file already has many `default_posture: blocking` LLM criteria (F1, E2, G5, etc.). The plan simultaneously claims 'plan-review has no non-advisory impact criterion' — which is false per the committed `criteria_routing.json` — and asks a test to assert it. A test implementing the AC literally would either fail against the existing routing or be scoped so nar …
- **suggested fix:** In AC3, replace 'plan-review: no non-advisory impact criterion' with 'plan-review: this child introduces no new blocking_enabled/default_posture:blocking criteria; the only blocking criteria in both routing configs are the pre-existing ones (enumerated by their IDs or verified against a committed ba …

  **HUMAN VERDICT (H014):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `G7`

#### H015  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — The parent epic AC genuinely imposes a disposition-recording gate before Gerrit votes, and the leaf plan's ACs make no mention of it; adding an AC to satisfy the parent contract is a real, actionable G7 gap.
- **B (sonnet-5):** `FP`
- **location:** `Parent epic AC: 'Each child story's plan-review advisory findings are recorded with an explicit Accept/Reject disposition...' / Leaf ticket: `"comments": []` / Leaf ACs (no mention of disposition gate)`  ·  **severity:** `none`
- **finding:** The parent epic's Acceptance Criteria impose a mandatory process gate on every child story: 'Each child story's plan-review advisory findings are recorded with an explicit Accept/Reject disposition and a one-line rationale (as a comment on the child ticket), and every Accepted finding is reflected in the child's description before that child's change receives its Gerrit LLM-Review +1 / Verified +1 votes.' The leaf plan's own ACs make no mention of this parent-level requirement, and the leaf ticket currently carries zero comments — meaning the advisory findings (20 advisory findings per the att …
- **suggested fix:** Add a final AC to the leaf: '[ ] Before the Gerrit change receives LLM-Review +1 or Verified +1 votes, all plan-review advisory findings (currently 20) are recorded on this ticket as a comment with explicit Accept/Reject disposition and one-line rationale; every Accepted finding is reflected in this …

  **HUMAN VERDICT (H015):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `P6`

#### H016  ·  covers 1 finding(s)

- **A (opus-4-8):** `FP` — The acceptance criteria are notably concrete and verifiable — each names an observable artifact and how it's checked (scorer registered, cannot vacuously pass, specs exist, baseline expectations pass), so the generic 'AC quality issues' finding does not identify a real weakness.
- **B (sonnet-5):** `TP`
- **location:** `—`  ·  **severity:** `minor`
- **finding:** Acceptance-criteria / definition-of-done quality issues.
- **suggested fix:** Split compound criteria, replace subjective terms with observable outcomes, and state how each is verified.

  **HUMAN VERDICT (H016):** `FP` _(inferred-from-precedent, operator-confirmed)_   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `T10`

#### H017  ·  covers 1 finding(s)

- **A (opus-4-8):** `FP` — The ticket is explicitly documentation-only (Scope: 'Out: any NEW infrastructure or code; S8 reuses and references the d251 artifacts'). The plan does not create or modify EBS/EC2 resources, so demanding `prevent_destroy` guards and sandbox testing imports concerns outside the plan's scope; the find …
- **B (sonnet-5):** `TP`
- **location:** `What section, Step 2 — 'EC2 + EBS'; Scope section; AC line 1`  ·  **severity:** `major`
- **finding:** The plan provisions an EBS volume (Gerrit's persistent data store) and an EC2 instance but makes no mention of `prevent_destroy` lifecycle guards on these stateful resources, nor of any dev/staging environment to test the Terraform before applying to a client's production account.
- **suggested fix:** Add a callout box in Step 2: 'The EBS volume holding Gerrit data MUST have `lifecycle { prevent_destroy = true }`. Verify this in `infra/main.tf` before running apply. We also recommend a dry-run in a sandbox AWS account first.'

  **HUMAN VERDICT (H017):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `T4`

#### H018  ·  covers 1 finding(s)

- **A (opus-4-8):** `FP` — The change is a reversible config flag edit in a version-controlled JSON file with no data migration or destructive action, so rollback is inherently trivial (revert the diff); demanding an explicit rollback subsection is over-application of T4.
- **B (sonnet-5):** `TP`
- **location:** `Scope section; AC line 1`  ·  **severity:** `minor`
- **finding:** The plan changes existing consumer-visible advisory behavior by down-ranking/suppressing `docs` and `llm-prompts` surfaces via `criteria_routing.json`, but provides no explicit rollback path or back-out mechanism — only a measurement step ('nit-volume drop measured on sampled diffs'), which T4 classifies as acknowledgement-only (insufficient).
- **suggested fix:** Add a sentence under Scope or a new 'Rollback' subsection: 'Before merging, snapshot the current `criteria_routing.json` values (e.g. via a git tag or a comment block in the file); rollback = restore those values and redeploy.'

  **HUMAN VERDICT (H018):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `T5c`

#### H019  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — The plan explicitly performs the private-host check as a construction-time static URL string check, which genuinely leaves a DNS-rebinding/TTL-based SSRF gap for a security boundary-crossing verifier — a real, actionable weakness the plan does not address.
- **B (sonnet-5):** `FP`
- **location:** `Security section — 'Private/link-local endpoint hosts (RFC 1918, loopback, 169.254.0.0/16) are rejected by default; an operator with a legitimate internal AS sets auth_introspection_allow_private_host=true to opt out.'`  ·  **severity:** `none`
- **finding:** The private-host/RFC 1918/link-local check is specified as happening 'at construction' (a static URL string check). This does not defend against DNS rebinding: a hostname that resolves to a public IP at construction time can later resolve to a private/internal IP (due to TTL expiry, DNS cache poisoning, or a misconfigured zone), routing subsequent introspection POSTs to internal services. The plan does not specify resolving and checking the target IP at each request or configuring the httpx transport to prevent SSRF at the network layer.
- **suggested fix:** Add a requirement that the implementation either (a) resolves the configured hostname to an IP at construction and pins the IP in the httpx transport (so DNS changes don't reroute calls), or (b) installs a custom httpx transport that checks `socket.getaddrinfo()` results against the RFC 1918 / loopb …

  **HUMAN VERDICT (H019):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

#### H020  ·  covers 1 finding(s)

- **A (opus-4-8):** `FP` — The plan section given never actually mentions an SSM slot 'reviewbot-tickets-pat', a git credential helper, or an AC bullet describing a PAT — the cited locations don't appear in the plan text, so the finding asserts a PAT-scope defect that isn't present in the reviewed plan.
- **B (sonnet-5):** `TP`
- **location:** `Section D (Infra) — SSM slot 'reviewbot-tickets-pat'; Implementation precision section — 'git credential helper'; AC bullet 4 ('reviewbot-tickets-pat' SSM slot)`  ·  **severity:** `major`
- **finding:** The plan specifies a 'RebarBotNava contents:write PAT' for the git push to GitHub but does not declare whether this is a fine-grained, repository-scoped PAT (minimum: write to refs/heads/tickets on navapbc/rebar only) or a classic OAuth PAT (account-wide write access to all repos). A classic PAT would grant far broader write access than the single-branch push requires, violating least-privilege on the new boundary-crossing credential path. The access posture is undeclared.
- **suggested fix:** In `infra/terraform/ssm.tf`, update the inline comment for `reviewbot-tickets-pat` to read: 'A GitHub **fine-grained** PAT for the RebarBotNava account, scoped to the navapbc/rebar repository with Contents: Read and write permission ONLY (not a classic PAT). Do not use a classic PAT — scope must be  …

  **HUMAN VERDICT (H020):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `T8`

#### H021  ·  covers 1 finding(s)

- **A (opus-4-8):** `FP` — The plan deliberately scopes thresholding to S6 ('S7 does no thresholding of its own') and states the tunable lives on LLMConfig per the other stories; defining that field's name/default/range is S6's responsibility, not this integration story, so it is not a defect in this plan.
- **B (sonnet-5):** `TP`
- **location:** `What section — aggregation rule: `both orderings agree + cited artifact + ≥overlap_conf_threshold``  ·  **severity:** `critical`
- **finding:** `overlap_conf_threshold` is referenced in the aggregation rule as the confidence gate (`≥overlap_conf_threshold`) but its `LLMConfig` field name, default value, and valid range are never defined in this plan — the plan only says 'all tunables live on `LLMConfig` per the other stories', without naming which story or which field.

  **HUMAN VERDICT (H021):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

#### H022  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — The INCONCLUSIVE-GATE RULE hinges on 'changed method' to decide whether dependent R-stories close deferred, but the epic never defines what constitutes an acceptable method change, leaving the re-run eligibility criterion genuinely unactionable per T8.
- **B (sonnet-5):** `FP`
- **location:** `Context section — 'INCONCLUSIVE-GATE RULE: an experiment that fails its own validity gate…gets at most ONE documented re-run with changed method; if still not clearly passing, every dependent R-story closes deferred'`  ·  **severity:** `minor`
- **finding:** The INCONCLUSIVE-GATE RULE's 'at most ONE documented re-run with changed method' is a decision protocol with an unspecified 'changed method' vocabulary. The rule controls whether a dependent R-story closes deferred or may proceed, but 'changed method' has no defined axes (e.g. sample size change, annotation protocol change, judge pool change, scoring-window change). Without this, the re-run eligibility check is unactionable.

  **HUMAN VERDICT (H022):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

## Soft disputes (one rater abstained)

### `dropped` · criterion `G1G2`

#### S001  ·  covers 1 finding(s)

- **A (opus-4-8):** `ambiguous` — If a separate write-side `EVENT_TYPES` frozenset in `event_append.py` really rejects unknown types, this is a real gap the plan omits (FP drop); but the plan does mention adding to 'the known-event-type set' and I cannot verify from the provided text that the reducer's set and this write-path set ar …
- **B (sonnet-5):** `FP`
- **location:** `file_impact section (missing entry); `src/rebar/_store/event_append.py` line 50–89`  ·  **severity:** `none`
- **finding:** `src/rebar/_store/event_append.py` (`EVENT_TYPES` frozenset) is not listed in `file_impact` but must be updated: `KEY_ADD` and `KEY_REVOKE` must be added to `EVENT_TYPES` for `write_and_push` to accept them — `_validate_event` raises `StoreError` for any type not in this set (line 120).
- **suggested fix:** Add `KEY_ADD = 'KEY_ADD'` and `KEY_REVOKE = 'KEY_REVOKE'` constants to `_version.py`, import them in `event_append.py`, and include them in `EVENT_TYPES`.

  **HUMAN VERDICT (S001):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `dropped` · criterion `ISF`

#### S002  ·  covers 1 finding(s)

- **A (opus-4-8):** `ambiguous` — The plan text provided is truncated before the docs Acceptance Criteria, so I cannot verify whether 'tiers' is actually omitted; additionally there is no 'tier model' concept visible anywhere in the provided plan, making it unclear whether the finding cites a real narrowing or a spurious concern.
- **B (sonnet-5):** `TP`
- **location:** `—`  ·  **severity:** `none`
- **finding:** The docs child ticket in the session log (4b5b) names 'tiers' as a distinct documentation topic ('model, tiers, enforcement, Jira mapping, rotation'), but the plan's docs Acceptance Criteria omit documenting the tier model as a named concept, silently narrowing the docs requirement.

  **HUMAN VERDICT (S002):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `dropped` · criterion `P6`

#### S003  ·  covers 1 finding(s)

- **A (opus-4-8):** `ambiguous` — The finding is generic boilerplate about AC quality, but the plan's actual acceptance criteria are truncated/not shown, so I cannot verify whether a real defect exists that would make suppression wrong versus right.
- **B (sonnet-5):** `TP`
- **location:** `—`  ·  **severity:** `minor`
- **finding:** Acceptance-criteria / definition-of-done quality issues.
- **suggested fix:** Split compound criteria, replace subjective terms with observable outcomes, and state how each is verified.

  **HUMAN VERDICT (S003):** `TP` _(inferred-from-precedent, operator-confirmed)_   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `COH`

#### S004  ·  covers 1 finding(s)

- **A (opus-4-8):** `ambiguous` — The plan rejects unsigned suppression on security grounds but doesn't specify a verification/signing mechanism for the sibling-supersede delivered-now path; whether this is a genuine coherence gap or a non-issue (structural graph relationship vs. gameable text field) is underdetermined by the plan t …
- **B (sonnet-5):** `TP`
- **location:** `Alternatives considered (tag-suppression rejection) vs AC4 (delivered-now definition)`  ·  **severity:** `major`
- **finding:** The Alternatives section rejects 'Tag- or progress-note-based suppression' explicitly because it is 'unsigned + gameable (prompt-injection surface into a security-sensitive gate)', yet AC4's second delivered-now path — 'superseded by a sibling under the same parent' (in-epic sibling supersede) — carries no described signing, verification, or tamper-resistance mechanism. This is a cross-section contradiction: the security rationale used to reject unsigned suppression paths is not applied to the sibling-supersede path that is accepted.
- **suggested fix:** Add to AC4: 'sibling supersede is valid only when the sibling's own completion attestation covers the AC text of the superseded child (verified by attestation hash comparison), not by any unsigned metadata field' — or similar language that closes the gap between the security rationale in Alternative …

  **HUMAN VERDICT (S004):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `E5`

#### S005  ·  covers 1 finding(s)

- **A (opus-4-8):** `ambiguous` — The plan does describe a distinct absent/garbage-file behavior path, but the referenced AC6 test spec is truncated/not visible, so I cannot verify whether the failure-mode is already covered; the parsing of garbage is also WS1-scoped, making the WS2 test gap partly out of scope.
- **B (sonnet-5):** `TP`
- **location:** `What section ('when the file is absent OR unreadable/garbage, the applied set is EMPTY') and AC6 (test spec)`  ·  **severity:** `none`
- **finding:** The plan explicitly defines behavior for an unreadable/garbage `.ensure-applied` file ('applied set is EMPTY → pending = all registry ids') but no test exercises this distinct failure-mode path.
- **suggested fix:** Extend AC6a to cover three sub-cases: absent file, empty file, and syntactically invalid JSON — each should result in the same WARNING being emitted exactly once within the interval.

  **HUMAN VERDICT (S005):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `G1G2`

#### S006  ·  covers 1 finding(s)

- **A (opus-4-8):** `ambiguous` — The plan does explicitly acknowledge the voter inherits the payload extension and is out of scope, but the finding's point—that no defaulting/backward-compat note ensures voter's existing call is unaffected—is a plausible real gap; whether it rises to a G1G2 defect is a defensible call either way.
- **B (sonnet-5):** `FP`
- **location:** `Scope section (Out of scope) / src/rebar/review_bot/voter.py`  ·  **severity:** `none`
- **finding:** If the sidecar payload shape is extended (for the deps map referenced in AC1), `review_bot/voter.py`'s call to `sidecar.emit()` / `sidecar.build_payload()` is an unenumerated consumer that would inherit the change — the plan's 'Out of scope: Gerrit voter path (already emits; only inherits the payload extension)' acknowledges this but does not enumerate voter.py as a file that needs review/verification.
- **suggested fix:** Add a note: 'Any new field added to `sidecar.build_payload()` must default to `""` or `None` so `voter.py`'s existing call is unaffected.'

  **HUMAN VERDICT (S006):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

### `surfaced` · criterion `G4`

#### S007  ·  covers 1 finding(s)

- **A (opus-4-8):** `TP` — The plan introduces a new Terraform CloudWatch alarm resource in S2 but no child (including S6's back-out) covers its removal/rollback, a genuine gap in back-out coverage that G4 (completeness/coherence) reasonably flags.
- **B (sonnet-5):** `ambiguous`
- **location:** `child 29a6-59d1-a3ba-40ee (S2) — Observability bullet; child 72b8-4cda-ccd4-414d (S6) — Back-out AC`  ·  **severity:** `minor`
- **finding:** S2 introduces a new Terraform resource (`aws_cloudwatch_metric_alarm` modelled on `monitoring_s4b.tf`) for `review_bot_merge_change_errors`, but no sibling child — including S6 (back-out) — mentions this alarm in back-out procedures, and no child specifies where this Terraform resource lives or which apply/plan CI step manages it. The parent S2 criterion mentions the CloudWatch metric via `infra/scripts/observability.sh` but does not mention a Terraform alarm resource, so this is S2-only scope that has no back-out coverage.
- **suggested fix:** Add to S6's back-out section: 'Terraform: remove the `review_bot_merge_change_errors` alarm resource introduced in S2 (or leave in place if the metric emission is also rolled back via observability.sh revert) and run terraform plan to confirm no drift.'

  **HUMAN VERDICT (S007):** `TP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

#### S008  ·  covers 1 finding(s)

- **A (opus-4-8):** `ambiguous` — The finding flags a real coordination risk if both children touch the same test file, but the plan does not actually pin either child to a specific file, so the shared-state/merge-conflict concern is speculative ('or a sibling module') and cannot be confirmed as a genuine plan defect from the text p …
- **B (sonnet-5):** `FP`
- **location:** `child 1fdc-f83b-771a-429d, child f476-1f56-48c9-487c`  ·  **severity:** `none`
- **finding:** Children 1fdc-f83b and f476-1f56 both add tests to `tests/integration/test_concurrency_regression.py` (or a sibling integration module) without cross-referencing each other. 1fdc adds remote-append-during-compaction scenarios; f476 adds same-parent/different-child race tests. Concurrent development on the same file creates implicit shared-state risk (conflicting test names, fixture reuse, ordering of test classes) and merge conflicts.
- **suggested fix:** Designate one child (e.g., f476, which defines policy) as the owner of the file structure, and have 1fdc add its scenarios in a clearly separated section or a named sibling module such as `test_remote_append_interleaving.py`.

  **HUMAN VERDICT (S008):** `FP`   (optional per-member override: `<finding_id>=TP|FP|ambiguous`)

---
When done, hand the verdicts back (chat referencing cluster ids, or fill `human_label` in `runs/disputes.jsonl`); `apply_adjudications.py` folds them into the corpus as the final gold label.
