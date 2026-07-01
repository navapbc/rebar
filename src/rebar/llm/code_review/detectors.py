"""Secrets / High-Critical-security detectors → the code-review gate's fail-CLOSED block
(epic b744 / WS5).

The detectors themselves are Engine B's (gitleaks via the `sarif` backend for secrets; opengrep
`security_*.yaml` rules for High/Critical security). Engine B is FAIL-OPEN by cardinal invariant
— a missing/errored tool ABSTAINS, never blocks. The fail-CLOSED decision is made HERE, in the
code-review gate's verdict assembly (the consumer), NOT in the oracle:

- a secrets/security detector that ABSTAINS (tool unavailable/errored/timed out — coverage we
  CANNOT establish) → force the verdict to BLOCK + a coverage-gap annotation, so a high-precision
  block is never silently skipped;
- a detector that MATCHES (a real secret / High-Critical finding on a changed file) → BLOCK + a
  real-finding annotation (distinct from the fail-closed one).

Everything else stays advisory at the high starting thresholds. Diff-scoped: a MATCH counts only
when it lands on a changed file (an abstain is whole-scan → always fail-closed).
"""

from __future__ import annotations

from typing import Any

#: The id prefix marking Engine B's secrets/High-Critical-security detectors (WS5).
SECURITY_DETECTOR_PREFIX = "rebar.builtin.security."
#: The gitleaks sentinel (secrets) maps to the `secret-detection` criterion; every other
#: `rebar.builtin.security.*` (the opengrep High/Critical rules) maps to `high-critical-security`.
SECRET_DETECTION_ID = "rebar.builtin.security.secrets-gitleaks"
SECRET_DETECTION = "secret-detection"
HIGH_CRITICAL_SECURITY = "high-critical-security"


def _criterion_for(detector_id: str) -> str:
    return SECRET_DETECTION if detector_id == SECRET_DETECTION_ID else HIGH_CRITICAL_SECURITY


def run_security_detectors(*, changed_files: list[str], repo_root: Any = None) -> dict[str, dict]:
    """Run ONLY the secrets/security detectors (a registry slice — not the whole grounding suite)
    over ``repo_root`` and bucket their evidence per criterion: ``{criterion: {abstained: [...],
    matches: [...]}}``. MATCHES are diff-scoped (post-filtered to ``changed_files``); ABSTAINS are
    whole-scan (kept regardless — they mean we could not verify)."""
    import os

    from rebar.grounding import engine_b
    from rebar.grounding.detectors import Registry, load_registry

    # A missing root must NOT silently skip the scan (that would be a fail-OPEN, defeating WS5's
    # fail-CLOSED). Default to the cwd — the gate runs from the repo it is reviewing.
    repo_root = repo_root if repo_root is not None else os.getcwd()
    reg = load_registry(repo_root)
    security = tuple(d for d in reg if d.id.startswith(SECURITY_DETECTOR_PREFIX))
    if not security:
        return {}
    result = engine_b.scan(repo_root, registry=Registry(detectors=security))
    changed = set(changed_files or [])
    out: dict[str, dict] = {}
    for rec in result.records:
        did = rec.get("detector_id") or ""
        if not did.startswith(SECURITY_DETECTOR_PREFIX):
            continue
        bucket = out.setdefault(_criterion_for(did), {"abstained": [], "matches": []})
        outcome = rec.get("outcome")
        if outcome == "abstain":
            bucket["abstained"].append(rec)
        elif outcome == "match":
            loc = (rec.get("location") or {}).get("file")
            # diff-scope: a MATCH counts ONLY when it lands on a changed file (the location is
            # relativized to repo_root upstream). Empty changed_files (no diff) ⇒ no match counts
            # — there is nothing changed to flag; abstains stay whole-scan (kept above).
            if loc and loc in changed:
                bucket["matches"].append(rec)
    return out


def apply_failclosed(
    verdict: dict[str, Any], *, changed_files: list[str], repo_root: Any = None
) -> dict[str, Any]:
    """Apply the consumer-side fail-CLOSED rule to ``verdict`` in place: a secrets/High-Critical
    detector that MATCHES (on a changed file) or ABSTAINS forces ``verdict["verdict"] = "BLOCK"``
    and records a ``coverage["security_detectors"]`` annotation distinguishing a fail-closed
    abstain from a real-finding block. No security signal → the verdict is unchanged (the oracle's
    fail-OPEN posture is untouched)."""
    from rebar.llm.code_review import registry

    det = run_security_detectors(changed_files=changed_files, repo_root=repo_root)
    notes: list[dict[str, Any]] = []
    block = False
    for crit in (SECRET_DETECTION, HIGH_CRITICAL_SECURITY):
        bucket = det.get(crit) or {"abstained": [], "matches": []}
        # The forced-BLOCK is gated by the criterion's `blocking_enabled` (criteria_routing.json,
        # via threshold_for) — so disabling a detector criterion in config makes it advisory
        # (recorded in coverage, not blocking), and the WS2→WS5 flag is operative for the detector
        # path, not just the LLM Pass-3 path.
        blocking_enabled = registry.threshold_for([crit])[1]
        if bucket["matches"]:
            block = block or blocking_enabled
            notes.append(
                {
                    "criterion": crit,
                    "reason": "detector-finding",
                    "count": len(bucket["matches"]),
                    "blocking": blocking_enabled,
                }
            )
        elif bucket["abstained"]:
            # fail-closed: coverage we could not establish must not silently pass (when blocking).
            block = block or blocking_enabled
            reasons = sorted({a.get("reason") for a in bucket["abstained"] if a.get("reason")})
            notes.append(
                {
                    "criterion": crit,
                    "reason": "fail-closed-abstain",
                    "abstain_reasons": reasons,
                    "blocking": blocking_enabled,
                }
            )
    if notes:
        verdict.setdefault("coverage", {})["security_detectors"] = notes
        if block:
            verdict["verdict"] = "BLOCK"
    return verdict
