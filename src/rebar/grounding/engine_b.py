"""Engine B — the detector evaluator (story 48d7 / epic 8f6c).

Three backends run declarative detectors over a repo and normalize every match (or
fail-open skip) to S1's evidence model:

* **OpenGrep** (``opengrep``/``semgrep``) — the PRIMARY matcher and first vertical
  slice. Pre-validated engine-faithfully (``--validate``, spike E1), then run with
  ``scan --sarif``; the SARIF is parsed by the shared :mod:`rebar.grounding.sarif`.
* **ast-grep** — a lighter structural secondary; validated by its own per-rule
  check, run with ``scan --json``, normalized in-module.
* **metric** (``scc``/``lizard``) — a size/complexity matcher with configurable
  thresholds. The tools are not installed here, so the path is real but fails open
  to ``abstain(no_tool)``.

Every backend runs inside the fail-open :mod:`rebar.grounding.harness` boundary:
a missing binary / timeout / version-skew / crash / invalid-detector becomes a
recorded ``abstain``, never a raise. Applicability is *self-declared* on the
detector (languages / file globs); a detector whose language or files are absent
is **skipped with a coverage record** (not run). Output is normalized to the
evidence model and validated by the caller via :func:`rebar.grounding.evidence.validate`.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import evidence as ev
from . import harness
from . import sarif
from .detectors import (
    BACKEND_ASTGREP,
    BACKEND_METRIC,
    BACKEND_OPENGREP,
    Detector,
    Registry,
    load_registry,
)

# ── Backend binary resolution + version pinning ──────────────────────────────

#: OpenGrep is the canonical engine; ``opengrep`` is a CLI-compatible fork of
#: semgrep, so we prefer an ``opengrep`` binary and fall back to ``semgrep``.
_OPENGREP_CANDIDATES = ("opengrep", "semgrep")
_ASTGREP_CANDIDATES = ("ast-grep", "sg")
#: Metric tools (size/complexity). Neither ships with rebar; absent -> abstain(no_tool).
_METRIC_CANDIDATES = ("scc", "lizard")

#: Language → file extensions, for self-declared file-presence routing.
_LANG_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "typescript": (".ts", ".tsx"),
    "python": (".py",),
    "go": (".go",),
    "ruby": (".rb",),
    "java": (".java",),
    "rust": (".rs",),
    "json": (".json",),
}


def _resolve_binary(candidates: Iterable[str]) -> str | None:
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return None


@dataclass(frozen=True)
class ScanResult:
    """The outcome of one Engine B scan: every emitted evidence record.

    ``records`` holds matches AND fail-open abstains (a skipped detector is a
    visible coverage record, never a silent no-op), so the list is the complete,
    self-describing account of what ran and what did not.
    """

    records: tuple[dict[str, Any], ...]

    def matches(self) -> list[dict[str, Any]]:
        return [r for r in self.records if r.get("outcome") == ev.OUTCOME_MATCH]

    def abstains(self) -> list[dict[str, Any]]:
        return [r for r in self.records if r.get("outcome") == ev.OUTCOME_ABSTAIN]


# ── Applicability routing (self-declared; absent lang/files -> skipped) ───────


def _repo_extensions(repo_root: Path) -> set[str]:
    exts: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Skip VCS + common heavy dirs so routing is cheap and deterministic.
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", ".venv", "__pycache__")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext:
                exts.add(ext)
    return exts


def _is_applicable(det: Detector, repo_exts: set[str], repo_root: Path) -> tuple[bool, str | None]:
    """Decide whether ``det`` applies to this repo.

    Returns ``(applicable, reason_if_not)``. A detector whose declared languages
    have no matching file extension in the repo is NOT applicable
    (``unsupported_lang``); declared ``paths`` globs that match nothing are also a
    skip. A detector that declares neither always applies (e.g. metric detectors).
    """
    langs = det.languages
    if langs:
        wanted: set[str] = set()
        for lang in langs:
            wanted.update(_LANG_EXTENSIONS.get(lang, ()))
        if wanted and wanted.isdisjoint(repo_exts):
            return False, "unsupported_lang"
        if not wanted:
            # A language we don't know how to route -> conservatively skip.
            return False, "unsupported_lang"
    globs = det.file_globs
    if globs:
        try:
            matched = any(any(repo_root.glob(g)) for g in globs)
        except (ValueError, NotImplementedError, OSError):
            # A malformed glob (absolute/non-relative pattern, bad syntax) must NOT
            # crash the scan — quarantine the detector as invalid, fail-open.
            return False, "invalid_detector"
        if not matched:
            return False, "unsupported_lang"
    return True, None


def _skip_record(det: Detector, backend: str, reason: str) -> dict[str, Any]:
    """A self-declared-inapplicable detector -> abstain with skipped coverage."""
    return ev.abstain(
        reason,
        job=_job_for(det),
        provenance_tier=_tier_for(det),
        backend=backend,
        detector_id=det.id,
        detail=f"{det.id}: declared applicability absent in repo",
    )


def _job_for(det: Detector) -> str:
    job = det.job
    return job if job in ev.JOBS else ev.JOB_SMELL


def _tier_for(det: Detector) -> str:
    tier = det.tier
    return tier if tier in ev.TIERS else ev.TIER_T1


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


def _opengrep_scan(binary: str, configs: list[str], repo_root: Path, version: str | None) -> harness.RunResult:
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
    candidates = [det for did, det in by_id.items() if engine_id == did or engine_id.endswith("." + did)]
    if not candidates:
        return None
    return max(candidates, key=lambda d: len(d.id))


def _enrich_sarif_envelopes(sarif_doc: dict[str, Any], by_id: dict[str, Detector], repo_root: Path) -> None:
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
                    "tier": _tier_for(det),
                    "job": _job_for(det),
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
    binary = _resolve_binary(_OPENGREP_CANDIDATES)
    if binary is None:
        return [
            ev.abstain("no_tool", job=_job_for(d), provenance_tier=_tier_for(d),
                       backend=BACKEND_OPENGREP, detector_id=d.id,
                       detail="no opengrep/semgrep binary on PATH")
            for d in detectors
        ]
    version = _binary_version(binary)
    records: list[dict[str, Any]] = []
    valid: list[Detector] = []
    for det in detectors:
        vres = _opengrep_validate(binary, det)
        if vres.abstained:
            records.append(vres.as_abstain(job=_job_for(det), provenance_tier=_tier_for(det), detector_id=det.id))
            continue
        if vres.returncode != 0:
            # Engine-faithful quarantine: a schema-invalid rule -> invalid_detector.
            records.append(ev.abstain(
                "invalid_detector", job=_job_for(det), provenance_tier=_tier_for(det),
                backend=BACKEND_OPENGREP, version=version, detector_id=det.id,
                detail=f"{det.id}: opengrep --validate exit {vres.returncode}",
            ))
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
            scan.as_abstain(job=_job_for(d), provenance_tier=_tier_for(d), detector_id=d.id) for d in valid
        )
        return records
    try:
        sarif_doc = json.loads(scan.stdout) if scan.stdout.strip() else {"runs": []}
    except json.JSONDecodeError:
        records.extend(
            ev.abstain("parse_error", job=_job_for(d), provenance_tier=_tier_for(d),
                       backend=BACKEND_OPENGREP, version=version, detector_id=d.id,
                       detail=f"{d.id}: opengrep SARIF was not JSON")
            for d in valid
        )
        return records
    _enrich_sarif_envelopes(sarif_doc, by_id, repo_root)
    records.extend(sarif.from_sarif(sarif_doc, backend=BACKEND_OPENGREP, version=version))
    return records


# ── ast-grep backend (secondary; validate-probe + JSON) ──────────────────────


def _run_astgrep(detectors: list[Detector], repo_root: Path) -> list[dict[str, Any]]:
    binary = _resolve_binary(_ASTGREP_CANDIDATES)
    records: list[dict[str, Any]] = []
    if binary is None:
        return [
            ev.abstain("no_tool", job=_job_for(d), provenance_tier=_tier_for(d),
                       backend=BACKEND_ASTGREP, detector_id=d.id, detail="no ast-grep binary on PATH")
            for d in detectors
        ]
    version = _binary_version(binary)
    for det in detectors:
        # ast-grep validates a rule as part of `scan -r`: a malformed rule exits
        # nonzero BEFORE producing matches (spike E1), so a scan that errors with
        # a parse complaint is the per-backend invalid-detector signal.
        res = harness.run_tool(
            [binary, "scan", "-r", det.source_path, "--json", str(repo_root)],
            backend=BACKEND_ASTGREP, version=version,
        )
        if res.abstained:
            records.append(res.as_abstain(job=_job_for(det), provenance_tier=_tier_for(det), detector_id=det.id))
            continue
        if res.returncode != 0:
            reason = "invalid_detector" if "parse rule" in res.stderr.lower() or "not a valid" in res.stderr.lower() else "other"
            records.append(ev.abstain(
                reason, job=_job_for(det), provenance_tier=_tier_for(det),
                backend=BACKEND_ASTGREP, version=version, detector_id=det.id,
                detail=f"{det.id}: ast-grep exit {res.returncode}: {res.stderr.strip()[:120]}",
            ))
            continue
        records.extend(_astgrep_matches(res.stdout, det, repo_root, version))
    return records


def _astgrep_matches(stdout: str, det: Detector, repo_root: Path, version: str | None) -> list[dict[str, Any]]:
    try:
        hits = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        return [ev.abstain("parse_error", job=_job_for(det), provenance_tier=_tier_for(det),
                           backend=BACKEND_ASTGREP, version=version, detector_id=det.id,
                           detail=f"{det.id}: ast-grep JSON was not parseable")]
    out: list[dict[str, Any]] = []
    cov = ev.coverage(backend=BACKEND_ASTGREP, status=ev.STATUS_RAN, version=version)
    for hit in hits if isinstance(hits, list) else []:
        loc = _astgrep_location(hit, repo_root)
        out.append(ev.match(
            job=_job_for(det), provenance_tier=_tier_for(det), coverage=cov,
            detector_id=det.id, location=loc, attention_only=det.attention_only,
            detail=hit.get("message") or None,
        ))
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
    binary = _resolve_binary(_METRIC_CANDIDATES)
    records: list[dict[str, Any]] = []
    for det in detectors:
        thresholds = {"oversize_loc": 800, "max_complexity": 15}
        thresholds.update(det.thresholds)  # project/envelope override
        if binary is None:
            records.append(ev.abstain(
                "no_tool", job=_job_for(det), provenance_tier=_tier_for(det),
                backend=BACKEND_METRIC, detector_id=det.id,
                detail=f"{det.id}: no scc/lizard on PATH (thresholds={thresholds})",
            ))
            continue
        records.extend(_metric_invoke(binary, det, repo_root, thresholds))
    return records


def _metric_invoke(binary: str, det: Detector, repo_root: Path, thresholds: dict[str, Any]) -> list[dict[str, Any]]:
    """Invoke a present metric tool (scc) and normalize oversize files.

    Real but minimal: scc emits per-language/file LOC as JSON. A binary that errors
    or returns unparseable output fails open to abstain. (Lands the plumbing; the
    rich per-function complexity path is a follow-up.)
    """
    res = harness.run_tool([binary, "--format", "json", str(repo_root)], backend=BACKEND_METRIC)
    if res.abstained:
        return [res.as_abstain(job=_job_for(det), provenance_tier=_tier_for(det), detector_id=det.id)]
    if res.returncode != 0:
        return [ev.abstain("other", job=_job_for(det), provenance_tier=_tier_for(det),
                           backend=BACKEND_METRIC, detector_id=det.id,
                           detail=f"{det.id}: metric tool exit {res.returncode}")]
    try:
        data = json.loads(res.stdout) if res.stdout.strip() else []
    except json.JSONDecodeError:
        return [ev.abstain("parse_error", job=_job_for(det), provenance_tier=_tier_for(det),
                           backend=BACKEND_METRIC, detector_id=det.id,
                           detail=f"{det.id}: metric JSON not parseable")]
    cov = ev.coverage(backend=BACKEND_METRIC, status=ev.STATUS_RAN)
    cutoff = int(thresholds.get("oversize_loc", 800))
    out: list[dict[str, Any]] = []
    for entry in _scc_files(data):
        loc = entry.get("Code", 0)
        if isinstance(loc, int) and loc > cutoff:
            out.append(ev.match(
                job=_job_for(det), provenance_tier=_tier_for(det), coverage=cov,
                detector_id=det.id, location={"file": entry.get("Location", "?")},
                attention_only=det.attention_only,
                detail=f"{entry.get('Location','?')}: {loc} LOC > {cutoff}",
            ))
    return out


def _scc_files(data: Any) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    if isinstance(data, list):
        for lang in data:
            for f in (lang.get("Files") or []) if isinstance(lang, dict) else []:
                if isinstance(f, dict):
                    files.append(f)
    return files


# ── Version pinning ──────────────────────────────────────────────────────────


def _binary_version(binary: str) -> str | None:
    res = harness.run_tool([binary, "--version"], backend="version-probe")
    if res.abstained or not res.stdout:
        return None
    return res.stdout.strip().splitlines()[0].strip() or None


# ── Scan entrypoint ──────────────────────────────────────────────────────────

_BACKEND_RUNNERS = {
    BACKEND_OPENGREP: _run_opengrep,
    BACKEND_ASTGREP: _run_astgrep,
    BACKEND_METRIC: _run_metric,
}


def scan(
    repo_root: str | os.PathLike[str],
    *,
    registry: Registry | None = None,
) -> ScanResult:
    """Run every applicable detector over ``repo_root`` and return normalized evidence.

    Loads (or accepts) the detector :class:`Registry`, routes each detector by
    self-declared applicability (absent language/files -> skipped + coverage), then
    dispatches the applicable ones per backend. The OpenGrep backend pre-validates
    + quarantines (spike E1); every backend runs fail-open via the harness. Returns
    a :class:`ScanResult` whose ``records`` are the complete account (matches AND
    abstains), each one a valid evidence record per S1's schema.
    """
    root = Path(repo_root)
    reg = registry if registry is not None else load_registry(root)
    repo_exts = _repo_extensions(root)

    records: list[dict[str, Any]] = []
    applicable_by_backend: dict[str, list[Detector]] = {b: [] for b in _BACKEND_RUNNERS}
    for det in reg:
        applicable, reason = _is_applicable(det, repo_exts, root)
        if not applicable:
            records.append(_skip_record(det, det.backend, reason or "unsupported_lang"))
            continue
        applicable_by_backend.setdefault(det.backend, []).append(det)

    for backend, runner in _BACKEND_RUNNERS.items():
        dets = applicable_by_backend.get(backend) or []
        if dets:
            records.extend(runner(dets, root))

    return ScanResult(records=tuple(ev.normalize_evidence(r) for r in records))
