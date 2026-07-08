"""Engine B — per-backend detector runners (split from :mod:`engine_b`, story 48d7).

Each of the four backends is invoked here — OpenGrep (primary; pre-validate +
quarantine + SARIF), ast-grep (structural secondary; validate-probe + JSON), metric
(scc/lizard size/complexity), and the generic SARIF-ingest backend (gitleaks) — plus
their private normalization helpers. This module was carved off :mod:`engine_b` so
neither unit exceeds the module-size cap; the shared helpers (binary resolution,
version pinning, job/tier coercion, the candidate-binary constants) and the ``scan``
entrypoint stay in :mod:`engine_b`.

The runners reference those shared helpers via the ``engine_b.`` module attribute at
call time (``engine_b._resolve_binary`` / ``engine_b.astgrep_binary`` / …) — NOT via
name-imports — so tests that monkeypatch them on the :mod:`engine_b` module (e.g.
``monkeypatch.setattr(engine_b, "_resolve_binary", …)``) still steer these runners,
and so the two modules import cleanly despite the mutual reference.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import engine_b, harness, sarif
from . import evidence as ev
from .detectors import (
    BACKEND_ASTGREP,
    BACKEND_METRIC,
    BACKEND_OPENGREP,
    BACKEND_SARIF,
    Detector,
)

# ── OpenGrep backend (primary; pre-validate + quarantine + SARIF) ────────────


def _opengrep_validate(binary: str, det: Detector) -> harness.RunResult:
    """Engine-faithful pre-validation (spike E1): ``--validate`` needs NO target.

    A schema-invalid rule exits nonzero here; the loader DROPS it as
    ``invalid_detector`` so the scan never aborts on one bad rule (the engine would
    otherwise exit 7 on the whole run).
    """
    return harness.run_tool(
        [binary, "--validate", "--config", det.source_path, "--metrics=off"],
        backend=BACKEND_OPENGREP,
    )


def _opengrep_scan(
    binary: str, configs: list[str], repo_root: Path, version: str | None
) -> harness.RunResult:
    cmd = [binary, "scan", "--sarif", "--metrics=off", "--no-git-ignore"]
    for cfg in configs:
        cmd += ["--config", cfg]
    cmd.append(str(repo_root))
    return harness.run_tool(cmd, backend=BACKEND_OPENGREP, version=version)


def _match_declared(engine_id: str, by_id: dict[str, Detector]) -> Detector | None:
    """Resolve an engine-emitted (path-namespaced) rule id to its declared detector.

    OpenGrep/semgrep PREFIX a rule id with the config file's path components (spike:
    "rule IDs are path-NAMESPACED"), e.g. ``…builtin.rebar.builtin.smell.js-console-log``.
    The declared id is therefore a dot-suffix of the engine id; we match on that.

    When more than one declared id is a suffix of the engine id (e.g. ``log`` and
    ``console.log``), the MOST-SPECIFIC (longest) declared id wins, so suffix
    ambiguity never mis-attributes a match to the wrong detector's envelope.
    """
    if engine_id in by_id:
        return by_id[engine_id]
    candidates = [
        det for did, det in by_id.items() if engine_id == did or engine_id.endswith("." + did)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda d: len(d.id))


def _enrich_sarif_envelopes(
    sarif_doc: dict[str, Any], by_id: dict[str, Detector], repo_root: Path
) -> None:
    """Re-attach envelopes + canonicalize ids on the engine's SARIF (in place).

    Two fix-ups before the shared :func:`sarif.from_sarif` parses the doc:

    * semgrep/opengrep do NOT carry custom ``metadata`` into SARIF, so the envelope
      (tier/job/attention_only) is lost — we re-inject it from the registry into
      ``rule.properties.rebar_envelope`` (the spike E5 mapping S1 reads).
    * the engine path-NAMESPACES rule ids; we rewrite both the rule ids and the
      results' ``ruleId`` back to the DECLARED id so evidence carries our namespaced
      id, not the config-path-derived one.
    """
    for run in sarif_doc.get("runs", []) or []:
        driver = (run.get("tool") or {}).get("driver") or {}
        for rule in driver.get("rules", []) or []:
            det = _match_declared(str(rule.get("id") or ""), by_id)
            if det is None:
                continue
            rule["id"] = det.id
            props = rule.setdefault("properties", {})
            if isinstance(props, dict):
                props["rebar_envelope"] = {
                    "tier": engine_b._tier_for(det),
                    "job": engine_b._job_for(det),
                    "attention_only": det.attention_only,
                }
        for res in run.get("results", []) or []:
            det = _match_declared(str(res.get("ruleId") or res.get("check_id") or ""), by_id)
            if det is not None:
                res["ruleId"] = det.id
            _relativize_location(res, repo_root)


def _relativize_location(res: dict[str, Any], repo_root: Path) -> None:
    """Rewrite a SARIF result's artifact URI to a repo-relative path (in place)."""
    for loc in res.get("locations") or []:
        art = ((loc or {}).get("physicalLocation") or {}).get("artifactLocation") or {}
        uri = art.get("uri")
        if not isinstance(uri, str) or not uri:
            continue
        try:
            if os.path.isabs(uri):
                art["uri"] = os.path.relpath(uri, repo_root)
        except ValueError:
            pass


def _run_opengrep(detectors: list[Detector], repo_root: Path) -> list[dict[str, Any]]:
    binary = engine_b._resolve_binary(engine_b._OPENGREP_CANDIDATES)
    if binary is None:
        return [
            ev.abstain(
                "no_tool",
                job=engine_b._job_for(d),
                provenance_tier=engine_b._tier_for(d),
                backend=BACKEND_OPENGREP,
                detector_id=d.id,
                detail="no opengrep/semgrep binary on PATH",
            )
            for d in detectors
        ]
    version = engine_b._binary_version(binary)
    records: list[dict[str, Any]] = []
    valid: list[Detector] = []
    for det in detectors:
        vres = _opengrep_validate(binary, det)
        if vres.abstained:
            records.append(
                vres.as_abstain(
                    job=engine_b._job_for(det),
                    provenance_tier=engine_b._tier_for(det),
                    detector_id=det.id,
                )
            )
            continue
        if vres.returncode != 0:
            # Engine-faithful quarantine: a schema-invalid rule -> invalid_detector.
            records.append(
                ev.abstain(
                    "invalid_detector",
                    job=engine_b._job_for(det),
                    provenance_tier=engine_b._tier_for(det),
                    backend=BACKEND_OPENGREP,
                    version=version,
                    detector_id=det.id,
                    detail=f"{det.id}: opengrep --validate exit {vres.returncode}",
                )
            )
            continue
        valid.append(det)

    if not valid:
        return records

    by_id = {d.id: d for d in valid}
    configs = sorted({d.source_path for d in valid})
    scan = _opengrep_scan(binary, configs, repo_root, version)
    if scan.abstained:
        # Whole-scan fail-open: every valid detector gets a coverage skip.
        records.extend(
            scan.as_abstain(
                job=engine_b._job_for(d),
                provenance_tier=engine_b._tier_for(d),
                detector_id=d.id,
            )
            for d in valid
        )
        return records
    try:
        sarif_doc = json.loads(scan.stdout) if scan.stdout.strip() else {"runs": []}
    except json.JSONDecodeError:
        records.extend(
            ev.abstain(
                "parse_error",
                job=engine_b._job_for(d),
                provenance_tier=engine_b._tier_for(d),
                backend=BACKEND_OPENGREP,
                version=version,
                detector_id=d.id,
                detail=f"{d.id}: opengrep SARIF was not JSON",
            )
            for d in valid
        )
        return records
    _enrich_sarif_envelopes(sarif_doc, by_id, repo_root)
    records.extend(sarif.from_sarif(sarif_doc, backend=BACKEND_OPENGREP, version=version))
    return records


# ── ast-grep backend (secondary; validate-probe + JSON) ──────────────────────


def _run_astgrep(
    detectors: list[Detector], repo_root: Path, sgconfig: str | None = None
) -> list[dict[str, Any]]:
    binary = engine_b.astgrep_binary()
    records: list[dict[str, Any]] = []
    if binary is None:
        return [
            ev.abstain(
                "no_tool",
                job=engine_b._job_for(d),
                provenance_tier=engine_b._tier_for(d),
                backend=BACKEND_ASTGREP,
                detector_id=d.id,
                detail="no ast-grep binary on PATH",
            )
            for d in detectors
        ]
    version = engine_b._binary_version(binary)
    # A project sgconfig.yml (custom tree-sitter grammars) is threaded to ast-grep via
    # --config so structural detectors in a project-declared language are honored.
    config_args = ["--config", sgconfig] if sgconfig else []
    for det in detectors:
        # ast-grep validates a rule as part of `scan -r`: a malformed rule exits
        # nonzero BEFORE producing matches (spike E1), so a scan that errors with
        # a parse complaint is the per-backend invalid-detector signal.
        res = harness.run_tool(
            [binary, "scan", "-r", det.source_path, *config_args, "--json", str(repo_root)],
            backend=BACKEND_ASTGREP,
            version=version,
        )
        if res.abstained:
            records.append(
                res.as_abstain(
                    job=engine_b._job_for(det),
                    provenance_tier=engine_b._tier_for(det),
                    detector_id=det.id,
                )
            )
            continue
        if res.returncode != 0:
            reason = (
                "invalid_detector"
                if "parse rule" in res.stderr.lower() or "not a valid" in res.stderr.lower()
                else "other"
            )
            records.append(
                ev.abstain(
                    reason,
                    job=engine_b._job_for(det),
                    provenance_tier=engine_b._tier_for(det),
                    backend=BACKEND_ASTGREP,
                    version=version,
                    detector_id=det.id,
                    detail=f"{det.id}: ast-grep exit {res.returncode}: {res.stderr.strip()[:120]}",
                )
            )
            continue
        records.extend(_astgrep_matches(res.stdout, det, repo_root, version))
    return records


def _astgrep_matches(
    stdout: str, det: Detector, repo_root: Path, version: str | None
) -> list[dict[str, Any]]:
    try:
        hits = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        return [
            ev.abstain(
                "parse_error",
                job=engine_b._job_for(det),
                provenance_tier=engine_b._tier_for(det),
                backend=BACKEND_ASTGREP,
                version=version,
                detector_id=det.id,
                detail=f"{det.id}: ast-grep JSON was not parseable",
            )
        ]
    out: list[dict[str, Any]] = []
    cov = ev.coverage(backend=BACKEND_ASTGREP, status=ev.STATUS_RAN, version=version)
    for hit in hits if isinstance(hits, list) else []:
        loc = _astgrep_location(hit, repo_root)
        out.append(
            ev.match(
                job=engine_b._job_for(det),
                provenance_tier=engine_b._tier_for(det),
                coverage=cov,
                detector_id=det.id,
                location=loc,
                attention_only=det.attention_only,
                detail=hit.get("message") or None,
            )
        )
    return out


def _astgrep_location(hit: dict[str, Any], repo_root: Path) -> dict[str, Any] | None:
    f = hit.get("file")
    if not f:
        return None
    try:
        rel = os.path.relpath(f, repo_root) if os.path.isabs(f) else f
    except ValueError:
        rel = f
    out: dict[str, Any] = {"file": rel}
    rng = hit.get("range") or {}
    start = (rng.get("start") or {}).get("line")
    end = (rng.get("end") or {}).get("line")
    # ast-grep lines are 0-based; the evidence model is 1-based.
    if isinstance(start, int):
        out["line_start"] = start + 1
    if isinstance(end, int):
        out["line_end"] = end + 1
    return out


# ── Metric backend (scc/lizard; thresholds; absent -> abstain(no_tool)) ──────


def _run_metric(detectors: list[Detector], repo_root: Path) -> list[dict[str, Any]]:
    """Metric matcher. scc/lizard are NOT installed here, so this fails open.

    The threshold plumbing is real: each detector's envelope carries
    ``oversize_loc`` / ``max_complexity`` cutoffs (shipped defaults, project
    overridable via ``.rebar/detectors/``). When a metric binary IS present this
    would run it and emit a ``match`` per file/function over the cutoff; absent, a
    metric detector records ``abstain(no_tool)`` (it has no rule schema, so
    ``invalid_detector`` is N/A for this backend — spike E1).
    """
    binary = engine_b._resolve_binary(engine_b._METRIC_CANDIDATES)
    records: list[dict[str, Any]] = []
    for det in detectors:
        thresholds = {"oversize_loc": 800, "max_complexity": 15}
        thresholds.update(det.thresholds)  # project/envelope override
        if binary is None:
            records.append(
                ev.abstain(
                    "no_tool",
                    job=engine_b._job_for(det),
                    provenance_tier=engine_b._tier_for(det),
                    backend=BACKEND_METRIC,
                    detector_id=det.id,
                    detail=f"{det.id}: no scc/lizard on PATH (thresholds={thresholds})",
                )
            )
            continue
        records.extend(_metric_invoke(binary, det, repo_root, thresholds))
    return records


def _metric_invoke(
    binary: str, det: Detector, repo_root: Path, thresholds: dict[str, Any]
) -> list[dict[str, Any]]:
    """Invoke a present metric tool (scc) and normalize oversize files.

    Real but minimal: scc emits per-language/file LOC as JSON. A binary that errors
    or returns unparseable output fails open to abstain. (Lands the plumbing; the
    rich per-function complexity path is a follow-up.)
    """
    res = harness.run_tool([binary, "--format", "json", str(repo_root)], backend=BACKEND_METRIC)
    if res.abstained:
        return [
            res.as_abstain(
                job=engine_b._job_for(det),
                provenance_tier=engine_b._tier_for(det),
                detector_id=det.id,
            )
        ]
    if res.returncode != 0:
        return [
            ev.abstain(
                "other",
                job=engine_b._job_for(det),
                provenance_tier=engine_b._tier_for(det),
                backend=BACKEND_METRIC,
                detector_id=det.id,
                detail=f"{det.id}: metric tool exit {res.returncode}",
            )
        ]
    try:
        data = json.loads(res.stdout) if res.stdout.strip() else []
    except json.JSONDecodeError:
        return [
            ev.abstain(
                "parse_error",
                job=engine_b._job_for(det),
                provenance_tier=engine_b._tier_for(det),
                backend=BACKEND_METRIC,
                detector_id=det.id,
                detail=f"{det.id}: metric JSON not parseable",
            )
        ]
    cov = ev.coverage(backend=BACKEND_METRIC, status=ev.STATUS_RAN)
    cutoff = int(thresholds.get("oversize_loc", 800))
    out: list[dict[str, Any]] = []
    for entry in _scc_files(data):
        loc = entry.get("Code", 0)
        if isinstance(loc, int) and loc > cutoff:
            out.append(
                ev.match(
                    job=engine_b._job_for(det),
                    provenance_tier=engine_b._tier_for(det),
                    coverage=cov,
                    detector_id=det.id,
                    location={"file": entry.get("Location", "?")},
                    attention_only=det.attention_only,
                    detail=f"{entry.get('Location', '?')}: {loc} LOC > {cutoff}",
                )
            )
    return out


def _scc_files(data: Any) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    if isinstance(data, list):
        for lang in data:
            for f in (lang.get("Files") or []) if isinstance(lang, dict) else []:
                if isinstance(f, dict):
                    files.append(f)
    return files


# ── Generic SARIF-ingest backend (WS5; gitleaks secrets) ─────────────────────


def _run_sarif(detectors: list[Detector], repo_root: Path) -> list[dict[str, Any]]:
    """Generic SARIF-ingest backend (WS5): subprocess-invoke a SARIF-emitting tool (gitleaks for
    secrets) and ingest its SARIF via ``sarif.from_sarif(trust_rebar_bag=False)``. Fail-OPEN like
    every backend — a missing/errored/non-JSON tool abstains (the code-review gate's verdict
    assembly turns a secrets/security abstain into a fail-CLOSED BLOCK; the oracle stays fail-open
    for all other consumers). Each detector is a SENTINEL descriptor (no matcher rules — the tool
    carries its own); the tool runs ONCE and its findings are attributed to the sentinel id."""
    binary = engine_b._resolve_binary(engine_b._GITLEAKS_CANDIDATES)
    if binary is None:
        return [
            ev.abstain(
                "no_tool",
                job=engine_b._job_for(d),
                provenance_tier=engine_b._tier_for(d),
                backend=BACKEND_SARIF,
                detector_id=d.id,
                detail="no gitleaks binary on PATH",
            )
            for d in detectors
        ]
    version = engine_b._binary_version(binary)
    sentinel_id = detectors[0].id if detectors else "rebar.builtin.security.secrets-gitleaks"
    # gitleaks writes SARIF to a FILE (it refuses an unwritable report path like /dev/stdout) and
    # `--exit-code 0` keeps a leaks-found run exit-0 (the SARIF carries the findings). We read the
    # report back. A run that produced NO parseable SARIF (errored / wrote nothing) ABSTAINS — it
    # is NEVER read as "0 findings" (which would be the silent fail-OPEN the gate's fail-CLOSED
    # forbids).
    with tempfile.TemporaryDirectory() as _td:
        report = os.path.join(_td, "gitleaks.sarif")
        cmd = [
            binary, "detect", "--no-banner", "--no-git",
            "--source", str(repo_root),
            "--report-format", "sarif", "--report-path", report,
            "--exit-code", "0",
        ]  # fmt: skip
        res = harness.run_tool(cmd, backend=BACKEND_SARIF, version=version)
        if res.abstained:
            return [
                res.as_abstain(
                    job=engine_b._job_for(d),
                    provenance_tier=engine_b._tier_for(d),
                    detector_id=d.id,
                )
                for d in detectors
            ]
        sarif_text = ""
        try:
            if os.path.exists(report):
                sarif_text = Path(report).read_text(encoding="utf-8")
        except OSError:
            sarif_text = ""
        try:
            sarif_doc = json.loads(sarif_text) if sarif_text.strip() else None
        except json.JSONDecodeError:
            sarif_doc = None
    if sarif_doc is None:
        # gitleaks ran but produced no parseable SARIF (e.g. a fatal error, non-zero exit) →
        # ABSTAIN (coverage we could not establish), never a silent zero-finding pass.
        return [
            ev.abstain(
                "parse_error",
                job=engine_b._job_for(d),
                provenance_tier=engine_b._tier_for(d),
                backend=BACKEND_SARIF,
                version=version,
                detector_id=d.id,
                detail=f"{d.id}: gitleaks produced no parseable SARIF (exit {res.returncode})",
            )
            for d in detectors
        ]
    # gitleaks emits ABSOLUTE artifact URIs; relativize them to repo_root so the gate's
    # diff-scope (repo-relative changed_files) can match.
    for run in sarif_doc.get("runs", []) or []:
        for r in run.get("results", []) or []:
            _relativize_location(r, repo_root)
    records = sarif.from_sarif(
        sarif_doc, backend=BACKEND_SARIF, version=version, trust_rebar_bag=False
    )
    # Re-attribute to the SENTINEL id: gitleaks' own ruleId (e.g. "github-pat") is not a
    # rebar.builtin.security.* id, so the consumer would drop it. The sentinel ran ONE tool, so
    # all its findings belong to the sentinel.
    for rec in records:
        rec["detector_id"] = sentinel_id
    return records
