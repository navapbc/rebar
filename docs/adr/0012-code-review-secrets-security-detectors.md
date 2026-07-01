# ADR 0012: Code-review secrets/security detectors — SARIF backend + consumer-side fail-closed

- **Status:** Accepted
- **Context:** Epic *Agentic code-review capability* (`b744-4fb9-2d05-4b49` / dowdy-swear-bird),
  story WS5 (`bba6-69b9-b9d4-4ac0` / corn-pub-oat). Builds on the grounding oracle / Engine B
  (epic 8f6c) + the code-review gate (WS1–WS4). Integration shape derisked in session_log
  a7e2-381c-6dc6-4d0c (EXPERIMENT 1).

## Context

The gate needs a high-precision HARD auto-block for secrets + High/Critical security, fed by
deterministic scanners (the LLM triages, never invents). Two tools: **semgrep** (High/Critical
security) and **gitleaks** (secrets). Two questions: how do they fit Engine B, and where does
the fail-CLOSED posture live (Engine B's cardinal invariant is fail-OPEN — a missing/errored tool
ABSTAINS, never blocks; forcing the oracle closed would break every other consumer).

## Decisions

### 1. semgrep = native opengrep detectors; gitleaks = a generic SARIF-ingest backend
- **semgrep** is the opengrep rule format (opengrep is a semgrep fork). High/Critical rules ship
  as native opengrep detector YAML (`detectors/builtin/security_owasp_cwe.yaml`, ids
  `rebar.builtin.security.*`) on the EXISTING `opengrep` backend — no new backend.
- **gitleaks** has rules compiled into the binary (TOML, no per-rule YAML), so it cannot be a
  per-rule detector. It emits SARIF 2.1.0, so it is integrated via a NEW generic SARIF-ingest
  backend `BACKEND_SARIF` (`_run_sarif` → `sarif.from_sarif(trust_rebar_bag=False)`), routed by a
  **SENTINEL** detector descriptor (`security_secrets_gitleaks.yaml`: `backend: sarif`, no
  matcher body). **Chosen over a gitleaks-only 4th backend** because semgrep also emits SARIF —
  one SARIF seam serves both and any future SARIF tool. `_run_sarif`/`BACKEND_SARIF` is new
  engine code (a 4th `_BACKEND_RUNNERS` entry), with its own test.

### 2. Fail-CLOSED lives in the CONSUMER (the gate's verdict assembly), NOT the oracle
Engine B stays fail-OPEN (a missing/errored secrets/security tool ABSTAINS). The code-review
gate's verdict assembly (`code_review.detectors.apply_failclosed`, called from
`produce_code_review_verdict`) makes the fail-CLOSED decision: a secrets/High-Critical detector
that **ABSTAINS** (coverage we cannot establish) OR **MATCHES** (a real finding on a changed
file) forces `verdict=BLOCK` with a `coverage.security_detectors` annotation that DISTINGUISHES a
fail-closed abstain (`reason: fail-closed-abstain`) from a real-finding block (`reason:
detector-finding`). The two criteria (`secret-detection`, `high-critical-security`) are the only
`blocking_enabled: true` keys in `criteria_routing.json` (WS2 shipped them `false`; WS5 flipped
them — the WS2→WS5 handoff).

**Bridge design — verdict-assembly forced-BLOCK, NOT a synthetic Pass-1 finding.** A synthetic
finding with no real Pass-2 verification hits `pass3_decide(None) → INDETERMINATE`, which cannot
block; the deterministic verdict-assembly short-circuit avoids fighting Pass-3.

### 3. Diff-scoped as a post-filter
Engine B `scan()` takes only `repo_root` (no scan-time changed-files param), so diff-scoping is a
POST-FILTER: a MATCH counts only when its location is a changed file. An ABSTAIN is whole-scan
(no location) → always fail-closed (we could not verify the change at all).

### 4. Vendored, pinned rules + refresh cadence + a (time-based) CI freshness gate
The security rules are VENDORED + pinned (not a live registry pull) for reproducible/offline
scanning. They must not silently rot, enforced by two committed pieces:
- `make vendor-security-rules` documents the refresh procedure + the pinned families; the cadence
  is quarterly (or when a relevant CVE/rule family lands), refreshed via a deliberate, reviewed PR.
- A **CI freshness gate** (`python -m rebar.grounding.detectors.security_pin`, the
  "Security-rules freshness gate" step in `.github/workflows/test.yml`) reads the pin manifest
  `detectors/builtin/security_rules_pin.json` and **WARNS** (a GitHub Actions `::warning::`, never
  a hard fail) when the recorded `vendored_at` date is older than `cadence_days`. It is
  **time-based + network-free by design** — it compares the recorded refresh date against today,
  so it needs no upstream access; warn-only so a lapsed cadence never blocks unrelated PRs, it
  just prompts a refresh PR (which re-pins the families and bumps `vendored_at`). A
  `vendored_at`-vs-upstream-VERSION diff (does an upstream rule actually exist that we haven't
  vendored?) genuinely needs network and remains the documented **follow-on**.

## Consequences

- Engine B gains a reusable SARIF-ingest backend; the fail-OPEN invariant is preserved for every
  other grounding consumer.
- The hard secrets/security block is high-precision (deterministic scanners) and fail-CLOSED only
  in the gate, so an unavailable scanner blocks rather than silently passing.
- Diff-scoping bounds per-patchset cost + noise (unchanged-file findings don't block).
- The security rule subset is small in v1 (a representative owasp/cwe set + gitleaks); broadening
  it is a `make vendor-security-rules` refresh, not a code change.

### Operator note — the scanners are a RUNTIME dependency of the enabled gate
Because the fail-CLOSED posture treats an abstaining scanner as a BLOCK, enabling the code-review
gate (`verify.enable_code_review = true`) makes `gitleaks` (secrets) and `opengrep` (High/Critical
security) **runtime prerequisites on the gate host**: if either binary is absent the detector
ABSTAINS and the gate fail-closes every successful review to `BLOCK` (with a `fail-closed-abstain`
coverage note, distinct from a real finding). This is intended — coverage we cannot establish must
not silently pass — but it means the gate environment (the Gerrit/MCP voter host in WS6, CI, or a
local dogfood) must install both tools before enabling the gate, or every change is vetoed on
infra grounds. Provision them alongside the gate; the `fail-closed-abstain` note in the verdict is
the operator's signal that the block was a coverage gap, not a finding.
