# ADR 0019: Deterministic public-exposure detectors are advisory native-opengrep rules, not a tfsec/Checkov bridge

- **Status:** Accepted
- **Context:** Task *Deterministic public-exposure-without-auth detectors in the code-review
  security overlay* (`830a`, `bane-tuber-marsh`), discovered from the container/leaf scrutiny
  bug (`a278`). The code-level counterpart to the plan-review T10 `endpoint_access_contract`
  check (ADR 0012 infra foundations, ADR 0016 container/leaf scrutiny). Relates to the
  data-driven DET consumer (ADR 0016 project-DET-invariants) and the WS5 secrets/security
  detectors (epic b744).

## Context

Plan-review reasons over prose and cannot do reachability analysis, so the plan-level T10
check can only ask whether a plan *states* an auth contract. The deterministic "is this
publicly exposed?" signal belongs to the **code-review** gate, which sees the actual
`*.tf` / docker-compose diff. The task is to flag a network service exposed to untrusted
networks — 0.0.0.0/0 ingress, a public IP, an internet-facing LB, a non-loopback bind.

Three design questions had to be answered (they were the plan-review BLOCK):

1. **tfsec/Checkov integration vs authored rules.** The grounding layer has no tfsec/Checkov
   seam — the `sarif` backend is hardwired to gitleaks candidates. Adding a tool bridge (a new
   candidate list + backend runner branch) is a much larger change than authoring native rules.
2. **Deterministic auth-*absence* correlation.** A single opengrep rule cannot prove "no
   authenticating gateway fronts this" — the gateway / service-mesh mTLS / network policy may
   live outside the diff. Pairing exposure with a deterministic auth-absence match is not
   achievable and would false-positive on every diff that legitimately fronts its service
   elsewhere.
3. **Blocking posture.** The AC wants FP-guarded cases to be "lower severity, not a hard block"
   and boundary controls "considered before BLOCK" — incompatible with the fail-closed
   `high-critical-security` DET criterion (any match → BLOCK).

## Decision

1. **Native opengrep rules, tfsec/Checkov-ALIGNED (cited, not integrated).** The detectors are
   authored opengrep rules in
   `src/rebar/grounding/detectors/builtin/security_iac_public_exposure.yaml`, each citing its
   equivalent tfsec / Checkov check id (e.g. tfsec `aws-vpc-no-public-ingress-sgr`, Checkov
   `CKV_AWS_260` / `CKV_AWS_88` / `CKV_AWS_150`). opengrep runs via the `semgrep` fallback
   binary already on PATH; semgrep's `terraform` and `yaml` languages were empirically confirmed
   to match the patterns.
2. **Deny-by-default EXPOSURE signal, not an auth-correlation.** Each rule fires on the
   deterministic exposure literal only, and its message names the exposure *and* the obligation
   to verify an authenticating front (the auth-absence cue). Auth-absence is the reviewer's
   judgement, deterministically cued — not a second match.
3. **Advisory, dedicated criterion.** A new `exec: "DET"` criterion
   `public-exposure-without-auth` (`blocking_enabled: false`, `fail_mode: "open"`) with its own
   detector id-prefix `rebar.builtin.iac.public-exposure.` — deliberately NOT the fail-closed
   `rebar.builtin.security.` prefix. A match surfaces as an advisory coverage finding, never an
   auto-BLOCK, so boundary controls outside the diff stay the reviewer's call.
4. **FP guards as pattern exclusions.** The rules match only unambiguous public-exposure
   literals; private CIDRs (10/8, 172.16/12, 192.168/16), loopback (127.0.0.1 / ::1), unix
   sockets, and `internal = true` LBs do not match, so they are never flagged.
5. **engine_b applicability fix.** `engine_b._LANG_EXTENSIONS` gained `terraform`/`hcl` →
   `.tf`/`.tfvars` and `yaml` → `.yml`/`.yaml`; without it a `languages: [terraform]` rule was
   skipped as `unsupported_lang` and never ran.
6. **No consumer-code change (story 7f0d).** The detector→criterion routing is data-driven, so
   `code_review/detectors.py` is untouched — the new criterion is picked up from its routing
   `detector` selector.

## Alternatives considered

- **File the rules under the existing fail-closed `high-critical-security` prefix.** Rejected:
  it would hard-BLOCK on every exposure literal, contradicting the "not a hard block / consider
  boundary controls first" AC and false-positiving on services fronted outside the diff.
- **Integrate tfsec/Checkov via a new sarif candidate.** Rejected for this task: a much larger
  change with no existing seam; the native rules deliver the same signals now and stay offline/
  reproducible. A tool bridge remains a future option if the rule set grows.
- **Attempt deterministic auth-absence pairing.** Rejected: unachievable with a single rule and
  a false-positive engine (auth frequently lives outside the diff).

## Consequences

- The code-review gate deterministically surfaces public exposure on the actual IaC diff,
  cueing the reviewer to verify authentication — closing the code-level gap under a278's
  plan-level T10 check.
- Because the criterion is advisory, it is coach-not-block: it never wedges a merge on an
  exposure that is legitimately fronted elsewhere, matching rebar's ship-advisory-first posture.
- opengrep/semgrep must be on PATH for the detectors to *execute*; when absent the criterion is
  fail-open (advisory), so no coverage is silently claimed. The unit tests skip their real-scan
  cases when neither binary is present.
