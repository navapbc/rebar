"""Public evidence-only facade over dependency, ctags, and detector lanes.

refute_absence routes dependencies to T0 and other references to T1; applies
queries the closed dimension vocabulary; scan filters normalized detector
records. DIMENSIONS is the registry's source of truth. Policy belongs to
consumers, and optional tools stay behind fail-open boundaries."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Mapping, Sequence
from typing import Any

from . import deps, engine_b, resolve, semantic
from . import evidence as ev
from .detectors import BACKENDS as _ENGINE_B_BACKENDS
from .detectors import Registry, load_registry

# ── The closed dimension-ID vocabulary (OWNED HERE; versioned) ────────────────

#: Monotonic version of the closed dimension vocabulary below. Bump on any
#: add/remove so a consumer pinning a dimension set can detect a vocabulary shift.
DIMENSIONS_VERSION = 1

#: Canonical closed dimension IDs shared with the detector registry. Unknown IDs
#: are flagged, not accepted: web_frontend, has_iac, touches_auth,
#: has_migrations, has_tests, and catch-all smell_generic.
DIMENSIONS: frozenset[str] = frozenset(
    {
        "web_frontend",
        "has_iac",
        "touches_auth",
        "has_migrations",
        "has_tests",
        "smell_generic",
    }
)

#: Re-exported from S2 — the closed reference-in ``kind`` set the facade routes on.
#: EXPOSED here (not redefined): the authority is :mod:`.resolve`.
REFERENCE_KINDS = resolve.REFERENCE_KINDS

#: The available detector backends (from Engine B).
BACKENDS = _ENGINE_B_BACKENDS


def is_known_dimension(dimension: str) -> bool:
    """True iff ``dimension`` is in the closed vocabulary :data:`DIMENSIONS`."""
    return dimension in DIMENSIONS


# ── Surface 1: refutation (job 1) — route by reference kind ───────────────────


def refute_absence(
    reference: Mapping[str, Any],
    *,
    repo_root: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Refute an absent reference through the T0 or T1 lane.

    Dependencies use the registry gauntlet; symbol, import, file, and member kinds
    use the T1 resolver, which checks paths, ctags symbols, and installed
    references as applicable. Forward lane-specific kwargs. Return one normalized
    refuted or closed-reason abstain record, never an absence claim or raise."""
    # Malformed or unknown references abstain at this fail-open API boundary.
    try:
        ref = resolve.validate_reference(reference)
    except resolve.ReferenceError as exc:
        return ev.normalize_evidence(
            ev.abstain(
                "invalid_detector",
                job=ev.JOB_REFUTE,
                provenance_tier=ev.TIER_T1,
                backend="oracle",
                detail=f"malformed reference: {exc}",
            )
        )
    if ref["kind"] == "dependency":
        rec = deps.refute_package(ref, workspace_members=kwargs.get("workspace_members"))
    else:
        rec = resolve.refute_absence(
            ref,
            repo_root=repo_root,
            index=kwargs.get("index"),
            config=kwargs.get("config"),
            timeout=kwargs.get("timeout"),
        )
        rec = _maybe_escalate_t2(ref, rec, repo_root=repo_root, **kwargs)
    return ev.normalize_evidence(rec)


def _maybe_escalate_t2(
    ref: Mapping[str, Any], rec: dict[str, Any], *, repo_root: str, **kwargs: Any
) -> dict[str, Any]:
    """Escalate eligible T1 abstentions to confirm-only semantic resolution.

    Only member, dotted, or not-found bare references qualify. Keep the T1 result
    unless T2 refutes it; disabled T2 leaves the T0+T1 result unchanged."""
    if rec.get("outcome") != ev.OUTCOME_ABSTAIN or not semantic.is_t2_territory(ref):
        return rec
    config = kwargs.get("config") or resolve.load_config(repo_root)
    if not getattr(config, "t2_enabled", False):
        return rec
    t2 = semantic.refute_semantic(
        ref,
        repo_root=repo_root,
        config=config,
        timeout=kwargs.get("timeout"),
        cache=kwargs.get("cache"),
    )
    if t2 is not None and t2.get("outcome") == ev.OUTCOME_REFUTED:
        return t2
    return rec


# ── Surface 2: applicability (job 2) — does a dimension apply to the repo? ─────


def applies(
    dimension: str,
    repo_root: str,
    *,
    registry: Registry | None = None,
) -> dict[str, Any]:
    """Return the first applicability match or a covered abstention.

    Only detectors declaring a known dimension qualify. Unknown dimensions and
    missing engines abstain rather than raising."""
    if dimension not in DIMENSIONS:
        return ev.normalize_evidence(
            ev.abstain(
                "invalid_detector",
                job=ev.JOB_APPLIES,
                provenance_tier=ev.TIER_T1,
                backend="oracle",
                detail=f"dimension {dimension!r} is not in the closed vocabulary "
                f"(v{DIMENSIONS_VERSION}): {sorted(DIMENSIONS)}",
            )
        )

    result = engine_b.scan(repo_root, registry=registry)
    # Resolve detector-declared dimensions from the scan's registry snapshot.
    reg = registry if registry is not None else load_registry(repo_root)
    dim_detectors = {d.id for d in reg if d.dimension == dimension and _is_applies(d)}

    matched: dict[str, Any] | None = None
    skips: list[dict[str, Any]] = []
    for rec in result.records:
        if rec.get("detector_id") not in dim_detectors:
            continue
        if rec.get("outcome") == ev.OUTCOME_MATCH:
            matched = rec
            break
        skips.append(rec)

    if matched is not None:
        return ev.normalize_evidence(matched)

    # No applicability detector fired. Surface the most informative skip if any
    # ran, else a clean no-match abstain with coverage.
    if skips:
        return ev.normalize_evidence(skips[0])
    return ev.normalize_evidence(
        ev.abstain(
            ev.DEFAULT_REASON,
            job=ev.JOB_APPLIES,
            provenance_tier=ev.TIER_T1,
            backend="oracle",
            detail=f"no applicability detector for dimension {dimension!r} matched "
            f"{repo_root!r} (it does not apply, or no detector declares it)",
        )
    )


def _is_applies(detector: Any) -> bool:
    return (detector.job or ev.JOB_APPLIES) == ev.JOB_APPLIES


# ── Surface 3: smell/metric scan (job 3) — filtered Engine B records ───────────


def scan(
    repo_root: str,
    *,
    detectors: Sequence[str] | None = None,
    dimensions: Sequence[str] | None = None,
    path_globs: Sequence[str] | None = None,
    registry: Registry | None = None,
) -> list[dict[str, Any]]:
    """Filter Engine B evidence by detector, dimension, and file globs.

    None means no restriction. Unknown dimensions match nothing. Location-free
    coverage records survive file filtering; matches and fail-open skips remain
    normalized evidence."""
    result = engine_b.scan(repo_root, registry=registry)
    records = list(result.records)

    if detectors is not None:
        wanted = set(detectors)
        records = [r for r in records if r.get("detector_id") in wanted]

    if dimensions is not None:
        reg = registry if registry is not None else load_registry(repo_root)
        wanted_dims = set(dimensions)
        dim_detector_ids = {d.id for d in reg if d.dimension in wanted_dims}
        records = [r for r in records if r.get("detector_id") in dim_detector_ids]

    if path_globs is not None:
        records = [r for r in records if _location_matches(r, path_globs)]

    return [ev.normalize_evidence(r) for r in records]


def _location_matches(record: Mapping[str, Any], globs: Sequence[str]) -> bool:
    """True iff the record has no file location (a non-file-scoped skip) OR its
    ``location.file`` matches any of ``globs`` (POSIX-normalized)."""
    loc = record.get("location")
    path = loc.get("file") if isinstance(loc, Mapping) else None
    if not path:
        return True  # a coverage-only skip is not file-scoped — never filtered out
    norm = str(path).replace(os.sep, "/")
    return any(fnmatch.fnmatch(norm, g) for g in globs)


# ── The static integration contract (for the grounding-info read tool) ────────


def contract() -> dict[str, Any]:
    """Return the static oracle integration contract without scanning a repo.

    Includes closed reasons, versioned dimensions, reference kinds, and detected
    backend availability/version; tool probes fail open."""
    return {
        "dimensions_version": DIMENSIONS_VERSION,
        "dimensions": sorted(DIMENSIONS),
        "reference_kinds": sorted(REFERENCE_KINDS),
        "abstain_reasons": sorted(ev.ABSTAIN_REASONS),
        "outcomes": sorted(ev.OUTCOMES),
        "jobs": sorted(ev.JOBS),
        "provenance_tiers": sorted(ev.TIERS),
        "backends": _backend_availability(),
    }


def _backend_availability() -> list[dict[str, Any]]:
    """Return each backend's name, availability, and version.

    Missing or failed version probes report unavailable with a null version."""
    out: list[dict[str, Any]] = []

    # T1 ctags lane (Engine A).
    ctags_ver = _safe(resolve.ctags_version)
    out.append(
        {"name": resolve.BACKEND_CTAGS, "available": ctags_ver is not None, "version": ctags_ver}
    )
    # The plain-filesystem refute backend is always available (no external tool).
    out.append({"name": resolve.BACKEND_FS, "available": True, "version": None})

    # Engine B backends (opengrep/ast-grep/metric/sarif) — resolved by PATH probe.
    for backend, candidates in (
        (engine_b.BACKEND_OPENGREP, engine_b._OPENGREP_CANDIDATES),
        (engine_b.BACKEND_ASTGREP, engine_b._ASTGREP_CANDIDATES),
        (engine_b.BACKEND_METRIC, engine_b._METRIC_CANDIDATES),
        (engine_b.BACKEND_SARIF, engine_b._GITLEAKS_CANDIDATES),
    ):
        binary = _safe(lambda c=candidates: engine_b._resolve_binary(c))
        version = _safe(lambda b=binary: engine_b._binary_version(b)) if binary else None
        out.append({"name": backend, "available": binary is not None, "version": version})

    # T0 deps lane: a network registry oracle (deps.dev). Reachability is not
    # probed here (it would make a network call); it is reported as a backend whose
    # availability is "best-effort at run time".
    out.append({"name": "registry", "available": True, "version": None})

    # T2 semantic lane (epic 850f): one entry per registered, opt-in backend. Empty
    # until a backend registers (story S3 adds pyright); each probe is fail-open.
    out.extend(semantic.available_backends())
    return out


def _safe(fn: Any) -> Any:
    """Call ``fn`` and swallow any exception to None (the contract probe never raises)."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a contract probe must never fail the read tool
        return None


__all__ = [
    "BACKENDS",
    "DIMENSIONS",
    "DIMENSIONS_VERSION",
    "REFERENCE_KINDS",
    "applies",
    "contract",
    "is_known_dimension",
    "refute_absence",
    "scan",
]
