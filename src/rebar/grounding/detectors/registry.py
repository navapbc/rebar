"""Detector discovery, parse, cache and the in-memory snapshot (story 48d7).

This module is the **registry substrate**: it finds detector files (built-in +
project-local), parses their thin rebar envelope, and hands :mod:`engine_b` an
immutable, process-local, mtime-cached :class:`Registry` snapshot. It does NOT run
any engine and does NOT decide applicability — those are evaluator concerns.

Discovery (unioned at load, project last-wins):

1. **Built-in** detectors shipped under ``detectors/builtin/`` (this package).
2. **Project-local** detectors under ``.rebar/detectors/`` of the scanned repo.

An absent project dir is *not* an error (fail-open). Two detectors with the same
namespaced ``id`` resolve **last-wins**, and because project is unioned after
built-in, a project file transparently overrides a built-in of the same id.

The registry is **process-local, built-once-and-cached, keyed by the detector
dirs' aggregate mtime** — concurrent scans in one process share one immutable
snapshot, and the cache rebuilds only when a detector dir changes on disk.

Engine-faithful **pre-validation + quarantine** (spike E1) is deliberately NOT
here — it requires the engine binary + the fail-open harness, which live in
:mod:`engine_b`. The registry only catalogs *parseable-as-YAML* detectors; a file
that is not even YAML is dropped at parse with a recorded note (``parse_error``),
and a structurally-bad-but-YAML rule survives to be quarantined engine-faithfully
by the evaluator.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Backends + the closed dimension vocabulary ───────────────────────────────

BACKEND_OPENGREP = "opengrep"
BACKEND_ASTGREP = "ast-grep"
BACKEND_METRIC = "metric"
BACKENDS: frozenset[str] = frozenset({BACKEND_OPENGREP, BACKEND_ASTGREP, BACKEND_METRIC})

#: PROVISIONAL closed dimension vocabulary. **S5 (ticket family 55de) owns the
#: canonical set**; this is a tiny placeholder so S4 can route applicability
#: detectors today. A detector whose ``dimension`` is outside this set still
#: loads (we do not gate on it here) but is flagged via :attr:`Detector.unknown_dimension`
#: so the integration with S5 is a one-line vocabulary swap, not a rewrite.
#: TODO(S5/55de): replace with the canonical dimension-ID registry and gate on it.
DIMENSIONS: frozenset[str] = frozenset(
    {
        "web_frontend",
        "has_iac",
        "touches_auth",
        "smell_generic",
    }
)

#: File extensions we treat as detector files in a detector dir.
_OPENGREP_SUFFIXES = (".yaml", ".yml")

#: The project-local detector directory, relative to the scanned repo root.
PROJECT_DETECTOR_DIR = os.path.join(".rebar", "detectors")


@dataclass(frozen=True)
class Detector:
    """One parsed detector: its rebar envelope + a handle to the verbatim payload.

    The native matcher payload is NEVER rewritten — :attr:`source_path` points at
    the on-disk file the engine is pointed at, and :attr:`rule` is the parsed dict
    only so we can read the envelope + applicability. ``opengrep`` detector files
    may carry MULTIPLE rules; :attr:`rule` is the specific rule this Detector wraps.
    """

    id: str
    backend: str
    namespace: str
    source_path: str
    rule: dict[str, Any] = field(default_factory=dict)
    envelope: dict[str, Any] = field(default_factory=dict)

    @property
    def languages(self) -> tuple[str, ...]:
        langs = self.rule.get("languages")
        if isinstance(langs, list):
            return tuple(str(x).lower() for x in langs)
        one = self.rule.get("language")
        return (str(one).lower(),) if one else ()

    @property
    def file_globs(self) -> tuple[str, ...]:
        """Optional file-presence triggers, declared on the envelope (``paths``)."""
        paths = self.envelope.get("paths")
        if isinstance(paths, list):
            return tuple(str(x) for x in paths)
        return ()

    @property
    def job(self) -> str | None:
        j = self.envelope.get("job")
        return str(j) if j is not None else None

    @property
    def tier(self) -> str | None:
        t = self.envelope.get("tier")
        return str(t) if t is not None else None

    @property
    def dimension(self) -> str | None:
        d = self.envelope.get("dimension")
        return str(d) if d is not None else None

    @property
    def attention_only(self) -> bool:
        return bool(self.envelope.get("attention_only", False))

    @property
    def unknown_dimension(self) -> bool:
        """True iff a declared dimension is outside the provisional vocabulary."""
        d = self.dimension
        return d is not None and d not in DIMENSIONS

    @property
    def thresholds(self) -> dict[str, Any]:
        """Metric cutoffs (oversize/complexity) declared on the envelope, if any."""
        t = self.envelope.get("thresholds")
        return dict(t) if isinstance(t, dict) else {}


@dataclass(frozen=True)
class Registry:
    """An immutable snapshot of all loaded detectors, keyed by namespaced id.

    Read-only: built once per (dir, mtime) signature and shared. Iterating yields
    :class:`Detector` objects; :meth:`by_backend` slices for each evaluator.
    """

    detectors: tuple[Detector, ...]
    #: parse-time drops: (source_path, reason) — files that were not even YAML.
    parse_drops: tuple[tuple[str, str], ...] = ()

    def __iter__(self) -> Any:
        return iter(self.detectors)

    def __len__(self) -> int:
        return len(self.detectors)

    def by_backend(self, backend: str) -> list[Detector]:
        return [d for d in self.detectors if d.backend == backend]

    def get(self, detector_id: str) -> Detector | None:
        for d in self.detectors:
            if d.id == detector_id:
                return d
        return None


# ── Discovery + parse ────────────────────────────────────────────────────────


def _builtin_dir() -> Path:
    return Path(__file__).resolve().parent / "builtin"


def _infer_backend(rule: dict[str, Any], envelope: dict[str, Any]) -> str:
    """Pick the backend for a parsed rule.

    Explicit envelope ``backend`` wins. Otherwise: an ast-grep rule has a
    top-level ``rule:`` + ``language:`` (singular) and no ``rules:`` list; a metric
    detector declares ``backend: metric`` (it has no native matcher payload of its
    own — only thresholds); everything else is opengrep (the default, ``rules:``).
    """
    declared = envelope.get("backend")
    if isinstance(declared, str) and declared in BACKENDS:
        return declared
    if "rule" in rule and "language" in rule and "rules" not in rule:
        return BACKEND_ASTGREP
    return BACKEND_OPENGREP


def _parse_yaml(path: Path) -> Any:
    import yaml  # PyYAML ships with rebar (reconciler config); import-lazy here.

    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _detectors_from_file(path: Path) -> tuple[list[Detector], str | None]:
    """Parse one detector file → (detectors, parse_error_reason | None).

    An opengrep file may carry several rules (each becomes a Detector); an ast-grep
    file is a single rule document. A file that is not valid YAML, or whose shape
    is unrecognizable, yields ``([], "parse_error")`` — it is dropped, never raised.
    """
    try:
        doc = _parse_yaml(path)
    except Exception:
        return [], "parse_error"
    if not isinstance(doc, dict):
        return [], "parse_error"

    out: list[Detector] = []
    rules = doc.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            det = _detector_from_rule(rule, path)
            if det is not None:
                out.append(det)
        return out, (None if out else "parse_error")

    # ast-grep / metric single-document form.
    det = _detector_from_rule(doc, path)
    if det is None:
        return [], "parse_error"
    return [det], None


def _detector_from_rule(rule: dict[str, Any], path: Path) -> Detector | None:
    rid = rule.get("id")
    if not isinstance(rid, str) or not rid.strip():
        return None
    metadata = rule.get("metadata")
    envelope_raw = metadata.get("rebar_envelope") if isinstance(metadata, dict) else None
    envelope: dict[str, Any] = dict(envelope_raw) if isinstance(envelope_raw, dict) else {}
    backend = _infer_backend(rule, envelope)
    namespace = str(envelope.get("namespace") or rid.split(".")[1] if "." in rid else "builtin")
    return Detector(
        id=rid.strip(),
        backend=backend,
        namespace=namespace,
        source_path=str(path),
        rule=rule,
        envelope=envelope,
    )


def _discover_dir(directory: Path) -> tuple[list[Detector], list[tuple[str, str]]]:
    detectors: list[Detector] = []
    drops: list[tuple[str, str]] = []
    if not directory.is_dir():
        return detectors, drops  # absent dir = fail-open, no error
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in _OPENGREP_SUFFIXES or not path.is_file():
            continue
        dets, reason = _detectors_from_file(path)
        detectors.extend(dets)
        if reason is not None:
            drops.append((str(path), reason))
    return detectors, drops


def _build(dirs: list[Path]) -> Registry:
    """Union the dirs (later dirs override earlier ids — last-wins)."""
    by_id: dict[str, Detector] = {}
    drops: list[tuple[str, str]] = []
    for directory in dirs:
        dets, dir_drops = _discover_dir(directory)
        drops.extend(dir_drops)
        for det in dets:
            by_id[det.id] = det  # last-wins override
    return Registry(detectors=tuple(by_id.values()), parse_drops=tuple(drops))


# ── Process-local, mtime-keyed cache ─────────────────────────────────────────


def _dir_signature(dirs: list[Path]) -> tuple[Any, ...]:
    """A cheap change-signature over the detector dirs (mtime + entry set).

    Keys the cache: any add/remove/modify of a detector file bumps the directory's
    mtime (and file mtimes), so the next :func:`load_registry` rebuilds.
    """
    sig: list[Any] = []
    for directory in dirs:
        try:
            st = directory.stat()
            entries = []
            for p in sorted(directory.iterdir()):
                if p.suffix.lower() in _OPENGREP_SUFFIXES:
                    entries.append((p.name, p.stat().st_mtime_ns, p.stat().st_size))
            sig.append((str(directory), st.st_mtime_ns, tuple(entries)))
        except OSError:
            sig.append((str(directory), None, ()))
    return tuple(sig)


_cache_lock = threading.Lock()
_cache: dict[tuple[Any, ...], Registry] = {}


def load_registry(repo_root: str | os.PathLike[str] | None = None) -> Registry:
    """Load (or return the cached) detector registry for ``repo_root``.

    Built-in detectors are always included; project-local detectors under
    ``<repo_root>/.rebar/detectors/`` are unioned on top (last-wins). The result is
    cached per (built-in dir, project dir, mtime) signature — process-local and
    immutable, so concurrent scans share one snapshot and a detector-dir change
    rebuilds on the next call.
    """
    builtin = _builtin_dir()
    dirs = [builtin]
    if repo_root is not None:
        dirs.append(Path(repo_root) / PROJECT_DETECTOR_DIR)

    key = (tuple(str(d) for d in dirs), _dir_signature(dirs))
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
        registry = _build(dirs)
        _cache[key] = registry
        return registry


def clear_cache() -> None:
    """Drop the process-local registry cache (test/maintenance helper)."""
    with _cache_lock:
        _cache.clear()
