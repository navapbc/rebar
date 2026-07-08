"""Engine B — the detector evaluator (story 48d7 / epic 8f6c).

Three backends run declarative detectors over a repo and normalize every match (or
fail-open skip) to S1's evidence model:

* **OpenGrep** (``opengrep``/``semgrep``) — the PRIMARY matcher and first vertical
  slice. Pre-validated engine-faithfully (``--validate``, spike E1), then run with
  ``scan --sarif``; the SARIF is parsed by the shared :mod:`rebar.grounding.sarif`.
* **ast-grep** — a lighter structural secondary; validated by its own per-rule
  check, run with ``scan --json``, normalized in-module. A project tree-sitter
  custom grammar (ast-grep ``customLanguages`` style) declared in the ``.rebar/``
  slot (``.rebar/sgconfig.yml``, or a path in ``.rebar/grounding.toml``) is honored
  for structural detectors via ``--config``; an unconfigured language fails open
  (skipped + coverage).
* **metric** (``scc``/``lizard``) — a size/complexity matcher with configurable
  thresholds. The tools are not installed here, so the path is real but fails open
  to ``abstain(no_tool)``.

Every backend runs inside the fail-open :mod:`rebar.grounding.harness` boundary:
a missing binary / timeout / version-skew / crash / invalid-detector becomes a
recorded ``abstain``, never a raise. Applicability is *self-declared* on the
detector (languages / file globs); a detector whose language or files are absent
is **skipped with a coverage record** (not run). Output is normalized to the
evidence model and validated by the caller via :func:`rebar.grounding.evidence.validate`.

The per-backend runners themselves live in :mod:`rebar.grounding.engine_b_backends`
(split out for the module-size cap) and are re-exported here — this module owns the
shared helpers (binary resolution, version pinning, applicability routing) and the
public :func:`scan` entrypoint the runners are dispatched from.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import evidence as ev
from . import harness
from .detectors import (
    BACKEND_ASTGREP,
    BACKEND_METRIC,
    BACKEND_OPENGREP,
    BACKEND_SARIF,
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
#: SARIF-emitting tools for the generic SARIF-ingest backend (WS5). gitleaks (secrets) is the
#: v1 tool; absent -> abstain(no_tool) (fail-open here; the gate fail-closes on the abstain).
_GITLEAKS_CANDIDATES = ("gitleaks",)

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
    # IaC: terraform/hcl (`.tf`/`.tfvars`) and yaml (compose / k8s manifests) — added for the
    # public-exposure detectors (task 830a). Without these a `languages: [terraform]` rule is
    # skipped as `unsupported_lang` and never runs.
    "terraform": (".tf", ".tfvars"),
    "hcl": (".tf", ".tfvars"),
    "yaml": (".yaml", ".yml"),
}


def _resolve_binary(candidates: Iterable[str]) -> str | None:
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return None


def astgrep_binary() -> str | None:
    """Path to a GENUINE ast-grep binary on PATH, or None. Validates each candidate's
    identity via ``--version`` so the unrelated shadow-utils ``sg`` (the Linux
    run-as-different-group command, also answered by ``which sg``) is NOT mistaken for
    ast-grep — which would otherwise run the wrong tool and yield no matches instead of
    cleanly abstaining (no_tool). Used by the scan path AND the tests' availability gate."""
    for name in _ASTGREP_CANDIDATES:
        path = shutil.which(name)
        if path:
            ver = _binary_version(path)
            if ver and "ast-grep" in ver.lower():
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
    for _dirpath, dirnames, filenames in os.walk(repo_root):
        # Skip VCS + common heavy dirs so routing is cheap and deterministic.
        dirnames[:] = [
            d for d in dirnames if d not in (".git", "node_modules", ".venv", "__pycache__")
        ]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext:
                exts.add(ext)
    return exts


#: Where a project declares an ast-grep custom-language (tree-sitter) config, in
#: the `.rebar/` slot. ast-grep's native project config file is `sgconfig.yml`; we
#: read it from `.rebar/sgconfig.yml` (or a path declared in `.rebar/grounding.toml`
#: under `[grounding] astgrep_sgconfig`). Its `customLanguages` entries register a
#: tree-sitter grammar (ast-grep `customLanguages` style) so structural detectors in
#: an otherwise-unsupported language are honored; an unconfigured language fails open.
_PROJECT_SGCONFIG_REL = ".rebar/sgconfig.yml"
_GROUNDING_TOML_REL = ".rebar/grounding.toml"


def _resolve_astgrep_sgconfig(repo_root: Path) -> tuple[str | None, dict[str, set[str]]]:
    """Resolve a project ast-grep ``sgconfig.yml`` (custom tree-sitter grammars).

    Returns ``(sgconfig_path | None, {custom_language: {".ext", …}})``. The path is
    passed to ast-grep via ``--config`` so its ``customLanguages`` grammars load; the
    extension map lets a custom-language detector route as applicable (its files are
    present) instead of being skipped as ``unsupported_lang``. Fails open to
    ``(None, {})`` on any read/parse error — a missing or malformed slot simply means
    no custom grammars, never a raise.
    """
    candidate = repo_root / _PROJECT_SGCONFIG_REL
    declared = _sgconfig_from_toml(repo_root)
    path = declared if declared is not None else (candidate if candidate.is_file() else None)
    if path is None or not Path(path).is_file():
        return None, {}
    custom_exts: dict[str, set[str]] = {}
    try:
        import yaml  # PyYAML is a core dependency

        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for lang, spec in (doc.get("customLanguages") or {}).items():
            exts = (spec or {}).get("extensions") if isinstance(spec, dict) else None
            if isinstance(exts, list):
                custom_exts[str(lang).lower()] = {
                    ("." + e.lstrip(".")).lower() for e in exts if isinstance(e, str) and e.strip()
                }
    except Exception:  # noqa: BLE001 — a malformed sgconfig must not break the scan
        return str(path), {}
    return str(path), custom_exts


def _sgconfig_from_toml(repo_root: Path) -> str | None:
    """An ``astgrep_sgconfig`` path declared in ``.rebar/grounding.toml``, resolved
    relative to the repo root, or None (fails open on any read/parse error)."""
    toml_path = repo_root / _GROUNDING_TOML_REL
    if not toml_path.is_file():
        return None
    try:
        import tomllib

        cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        rel = (cfg.get("grounding") or {}).get("astgrep_sgconfig")
        if isinstance(rel, str) and rel.strip():
            resolved = repo_root / rel
            return str(resolved) if resolved.is_file() else None
    except Exception:  # noqa: BLE001 — fail-open: any toml read/parse error → None (no sgconfig)
        return None
    return None


def _is_applicable(
    det: Detector,
    repo_exts: set[str],
    repo_root: Path,
    custom_exts: dict[str, set[str]] | None = None,
) -> tuple[bool, str | None]:
    """Decide whether ``det`` applies to this repo.

    Returns ``(applicable, reason_if_not)``. A detector whose declared languages
    have no matching file extension in the repo is NOT applicable
    (``unsupported_lang``); declared ``paths`` globs that match nothing are also a
    skip. A detector that declares neither always applies (e.g. metric detectors).
    """
    langs = det.languages
    # `generic` is opengrep/semgrep's language-agnostic mode: the rule scans EVERY file regardless
    # of extension (used by polyglot detectors like the conflict-marker rule, story da25). It has no
    # extension set to gate on, so a `generic` detector is ALWAYS applicable — the file-presence
    # check below would otherwise skip it as `unsupported_lang` and it would never run.
    if langs and "generic" in langs:
        return True, None
    if langs:
        wanted: set[str] = set()
        for lang in langs:
            wanted.update(_LANG_EXTENSIONS.get(lang, ()))
            # A project-declared custom tree-sitter grammar (ast-grep customLanguages
            # in the `.rebar/` slot) makes an otherwise-unknown language routable.
            if custom_exts:
                wanted.update(custom_exts.get(lang, ()))
        if wanted and wanted.isdisjoint(repo_exts):
            return False, "unsupported_lang"
        if not wanted:
            # A language neither we nor the project configured -> conservatively skip.
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


# ── Version pinning ──────────────────────────────────────────────────────────


def _binary_version(binary: str) -> str | None:
    res = harness.run_tool([binary, "--version"], backend="version-probe")
    if res.abstained or not res.stdout:
        return None
    return res.stdout.strip().splitlines()[0].strip() or None


# ── Scan entrypoint ──────────────────────────────────────────────────────────

# The per-backend runners live in engine_b_backends (module-size split). They are
# re-exported here so ``engine_b._run_opengrep`` / ``engine_b._match_declared`` /
# … keep resolving (external callers + tests reference them on this module, and
# monkeypatch the shared helpers above on this module — which the runners read at
# call time via the ``engine_b.`` prefix, so the patches steer them).
from .engine_b_backends import (  # noqa: E402 — after the shared helpers the runners read
    _match_declared,  # noqa: F401 — re-exported for external callers/tests (engine_b._match_declared)
    _run_astgrep,
    _run_metric,
    _run_opengrep,
    _run_sarif,
)

_BACKEND_RUNNERS = {
    BACKEND_OPENGREP: _run_opengrep,
    BACKEND_ASTGREP: _run_astgrep,
    BACKEND_METRIC: _run_metric,
    BACKEND_SARIF: _run_sarif,
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
    sgconfig, custom_exts = _resolve_astgrep_sgconfig(root)

    records: list[dict[str, Any]] = []
    applicable_by_backend: dict[str, list[Detector]] = {b: [] for b in _BACKEND_RUNNERS}
    for det in reg:
        # Custom-grammar extensions only widen routing for the structural (ast-grep) backend.
        det_custom = custom_exts if det.backend == BACKEND_ASTGREP else None
        applicable, reason = _is_applicable(det, repo_exts, root, det_custom)
        if not applicable:
            records.append(_skip_record(det, det.backend, reason or "unsupported_lang"))
            continue
        applicable_by_backend.setdefault(det.backend, []).append(det)

    for backend, runner in _BACKEND_RUNNERS.items():
        dets = applicable_by_backend.get(backend) or []
        if not dets:
            continue
        if backend == BACKEND_ASTGREP:
            records.extend(_run_astgrep(dets, root, sgconfig))
        else:
            records.extend(runner(dets, root))

    return ScanResult(records=tuple(ev.normalize_evidence(r) for r in records))
