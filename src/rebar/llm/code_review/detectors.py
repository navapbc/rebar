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
    """The legacy hardcoded map (kept for reference / the deprecated alias's parity). The live
    routing is now data-driven — see :func:`registry.criterion_for_detector`."""
    return SECRET_DETECTION if detector_id == SECRET_DETECTION_ID else HIGH_CRITICAL_SECURITY


def run_detectors(*, changed_files: list[str], repo_root: Any = None) -> dict[str, dict]:
    """Run the DET-criteria detectors (a registry slice — not the whole grounding suite) over
    ``repo_root`` and bucket their evidence per criterion: ``{criterion: {abstained: [...],
    matches: [...]}}``.

    The detector→criterion routing is DATA-DRIVEN (story 7f0d): the set of detectors run + the
    criterion each maps to are read from the `exec: "DET"` routing entries' `detector` selectors
    (:func:`registry.det_criteria` / :func:`registry.criterion_for_detector`), not a hardcoded
    id prefix. MATCHES are diff-scoped (post-filtered to ``changed_files``); ABSTAINS are
    whole-scan (kept regardless — they mean we could not verify)."""
    import os

    from rebar.grounding import engine_b
    from rebar.grounding.detectors import Registry, load_registry
    from rebar.llm.code_review import registry

    # A missing root must NOT silently skip the scan (that would be a fail-OPEN, defeating the
    # fail-CLOSED posture). Default to the cwd — the gate runs from the repo it is reviewing.
    repo_root = repo_root if repo_root is not None else os.getcwd()
    det_map = registry.det_criteria()
    reg = load_registry(repo_root)
    # Slice the registry to the detectors ANY DET criterion selects (exact id or id-prefix).
    selected = tuple(d for d in reg if registry.criterion_for_detector(d.id, det_map) is not None)
    if not selected:
        return {}
    result = engine_b.scan(repo_root, registry=Registry(detectors=selected))
    changed = set(changed_files or [])
    out: dict[str, dict] = {}
    for rec in result.records:
        did = rec.get("detector_id") or ""
        crit = registry.criterion_for_detector(did, det_map)
        if crit is None:
            continue
        bucket = out.setdefault(crit, {"abstained": [], "matches": []})
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


def run_security_detectors(*, changed_files: list[str], repo_root: Any = None) -> dict[str, dict]:
    """Deprecated alias for :func:`run_detectors` (story 7f0d renamed it once the detector→
    criterion routing became data-driven — the "security" framing is now just one class of DET
    criterion). Delegates verbatim; kept for the existing WS5 call sites/tests."""
    return run_detectors(changed_files=changed_files, repo_root=repo_root)


def apply_failclosed(
    verdict: dict[str, Any], *, changed_files: list[str], repo_root: Any = None
) -> dict[str, Any]:
    """Apply the consumer-side fail-CLOSED / fail-OPEN rule to ``verdict`` in place.

    Iterates the DET criteria from the routing index (:func:`registry.det_criteria`, no longer a
    hardcoded pair) and, per criterion, reads its ``fail_mode``:

    * a MATCH (on a changed file) → force ``verdict["verdict"] = "BLOCK"`` per the criterion's
      ``blocking_enabled`` (unchanged from WS5) + a ``detector-finding`` note;
    * an ABSTAIN → block per ``blocking_enabled`` **only when** ``fail_mode == "closed"``
      (coverage we could not establish must not silently pass). A ``fail_mode: "open"`` criterion
      records the abstain in coverage but does NOT block — the generalization: project invariants
      default to fail-OPEN, while the security class stays fail-CLOSED.

    No DET signal → the verdict is unchanged (the oracle's fail-OPEN posture is untouched)."""
    from rebar.llm.code_review import registry

    # Call via the alias so a monkeypatch of EITHER `run_detectors` (the alias delegates through
    # the module global) or `run_security_detectors` is honored by the existing WS5 test suite.
    det = run_security_detectors(changed_files=changed_files, repo_root=repo_root)
    det_map = registry.det_criteria()
    notes: list[dict[str, Any]] = []
    block = False
    for crit, spec in det_map.items():
        bucket = det.get(crit) or {"abstained": [], "matches": []}
        # The forced-BLOCK is gated by the criterion's `blocking_enabled` (criteria_routing.json,
        # via threshold_for) — so disabling a detector criterion in config makes it advisory
        # (recorded in coverage, not blocking).
        blocking_enabled = registry.threshold_for([crit])[1]
        fail_mode = str(spec.get("fail_mode", "open")).lower()
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
            # fail-CLOSED criteria block on an abstain (coverage we could not establish);
            # fail-OPEN criteria record it as coverage only (never block on absence).
            fail_closed = fail_mode == "closed"
            block = block or (blocking_enabled and fail_closed)
            reasons = sorted({a.get("reason") for a in bucket["abstained"] if a.get("reason")})
            notes.append(
                {
                    "criterion": crit,
                    "reason": "fail-closed-abstain" if fail_closed else "fail-open-abstain",
                    "abstain_reasons": reasons,
                    "blocking": blocking_enabled and fail_closed,
                }
            )
    if notes:
        verdict.setdefault("coverage", {})["security_detectors"] = notes
        if block:
            verdict["verdict"] = "BLOCK"
    return verdict
