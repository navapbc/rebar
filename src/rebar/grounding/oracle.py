"""The public oracle API — three query surfaces over the three engines (S5).

This is the **thin facade** the epic's two consumers build against (``5fd2`` the
DET floor, ``9da1`` the Pass-2 reviewers): one stable surface, three query
methods, one normalized evidence model out of every one. It UNIFIES the lanes the
earlier stories built — it adds no detection logic of its own, it only routes and
filters, keeping the package **pure-evidence** (NO block/advisory policy lives
here; that is the consumer's call).

The three surfaces (job 1 / job 2 / job 3 of the oracle):

* :func:`refute_absence` — *job 1, refutation.* Route a reference-in dict by its
  ``kind``: ``dependency`` → the T0 deps lane (:func:`deps.refute_package`),
  everything else (``symbol``/``import``/``file``/``member``) → the T1 ctags lane
  (:func:`resolve.refute_absence`). THIS is the unification: the resolve lane
  abstains-and-routes for ``dependency`` today; the facade makes the dependency
  call actually happen, so a consumer hits ONE entry point for every kind.
* :func:`applies` — *job 2, applicability.* Run the applicability detectors that
  declare a given ``dimension`` over the repo and return ONE evidence record: a
  ``match`` if any applicability detector fires, else an ``abstain`` (no-match,
  with coverage). ``dimension`` is validated against the **closed dimension-ID
  vocabulary owned here** (:data:`DIMENSIONS`); an unknown dimension abstains.
* :func:`scan` — *job 3, smell/metric scan.* Wrap :func:`engine_b.scan` and filter
  the records by ``detectors`` / ``dimensions`` / ``path_globs`` (``None`` = all
  applicable). Returns the list of evidence records (matches AND fail-open skips).

The **closed dimension-ID vocabulary** (:data:`DIMENSIONS`, versioned by
:data:`DIMENSIONS_VERSION`) is OWNED here — :mod:`.detectors.registry` imports it,
so this module is the single source of truth a consumer or a project detector
draws a dimension from. The **reference-in schema** is DEFINED + validated in S2
(:func:`resolve.validate_reference`); the oracle only EXPOSES it (re-exporting
:data:`REFERENCE_KINDS`), it never redefines it.

stdlib-only and import-clean: importing the facade pulls only the contract + the
already-import-clean lanes; the heavy tree-sitter binding stays behind the worker
boundary the harness owns.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Any, Mapping, Sequence

from . import deps, engine_b, evidence as ev, resolve
from .detectors import BACKENDS as _ENGINE_B_BACKENDS, Registry, load_registry

# ── The closed dimension-ID vocabulary (OWNED HERE; versioned) ────────────────

#: Monotonic version of the closed dimension vocabulary below. Bump on any
#: add/remove so a consumer pinning a dimension set can detect a vocabulary shift.
DIMENSIONS_VERSION = 1

#: The CANONICAL closed set of applicability/overlay dimension IDs. A consumer
#: passes one of these to :func:`applies`; a detector declares one in its
#: ``rebar_envelope.dimension``. This is the integration contract S5 owns —
#: :mod:`.detectors.registry` imports it (replacing its provisional placeholder),
#: so there is ONE source of truth and a project detector outside this set is
#: flagged (``Detector.unknown_dimension``) rather than silently accepted.
#:
#: Members:
#:
#: * ``web_frontend`` — the repo exposes a JS/TS web-frontend surface.
#: * ``has_iac`` — infrastructure-as-code (Terraform/CloudFormation/Pulumi/…)
#:   is present.
#: * ``touches_auth`` — authentication / authorization surface is present.
#: * ``has_migrations`` — database schema migrations are present.
#: * ``has_tests`` — an automated test surface is present.
#: * ``smell_generic`` — the catch-all dimension for job-3 smell/metric detectors
#:   that are not scoped to a specific applicability overlay.
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
    """Refute an asserted-absent ``reference`` — the unified entry for ALL kinds.

    Routes by the reference's ``kind`` (validated against the S2 closed set):

    * ``kind=dependency`` → the **T0 deps lane** (:func:`deps.refute_package`): a
      registry-existence probe wrapped in the abstain gauntlet (stdlib/workspace/
      mismatch/transient guards). This is the unification — the standalone T1
      resolver abstains-and-routes for ``dependency``; the facade makes the deps
      call actually happen so a consumer needs ONE entry point.
    * any other kind (``symbol``/``import``/``file``/``member``) → the **T1 ctags
      lane** (:func:`resolve.refute_absence`).

    Returns ONE normalized evidence record (``refuted`` or ``abstain`` with a
    CLOSED reason). NEVER asserts an absence; NEVER raises on a resolution failure
    (fail-open through the lanes). ``**kwargs`` forwards lane-specific options
    (``index``/``config``/``timeout`` for the ctags lane;
    ``workspace_members`` for the deps lane).
    """
    # Fail-open at the boundary: a malformed/unknown reference (bad kind, missing
    # name, non-mapping) is the untrusted-input case the oracle exists to absorb, so
    # it becomes an abstain — never a raise. Mirrors how `applies` absorbs an
    # out-of-vocab dimension, keeping the facade uniformly fail-open.
    try:
        ref = resolve.validate_reference(reference)
    except resolve.ReferenceError as exc:
        return ev.normalize_evidence(
            ev.abstain(
                "invalid_detector", job=ev.JOB_REFUTE, provenance_tier=ev.TIER_T1,
                backend="oracle", detail=f"malformed reference: {exc}",
            )
        )
    if ref["kind"] == "dependency":
        rec = deps.refute_package(
            ref, workspace_members=kwargs.get("workspace_members")
        )
    else:
        rec = resolve.refute_absence(
            ref,
            repo_root=repo_root,
            index=kwargs.get("index"),
            config=kwargs.get("config"),
            timeout=kwargs.get("timeout"),
        )
    return ev.normalize_evidence(rec)


# ── Surface 2: applicability (job 2) — does a dimension apply to the repo? ─────


def applies(
    dimension: str,
    repo_root: str,
    *,
    registry: Registry | None = None,
) -> dict[str, Any]:
    """Decide whether an applicability ``dimension`` applies to ``repo_root``.

    Runs the **job-2 applicability detectors** declaring ``dimension`` over the
    repo and returns ONE evidence record:

    * a ``match`` (the FIRST firing applicability detector's match) iff any such
      detector fires — the dimension APPLIES;
    * else an ``abstain`` (no applicability detector matched) carrying a coverage
      record, so a no-match is a visible, self-describing skip, never a silent
      no-op.

    ``dimension`` MUST be in the closed vocabulary :data:`DIMENSIONS`; an unknown
    dimension returns ``abstain(invalid_detector)`` (it is a malformed request, not
    a repo fact). Fail-open throughout (a missing engine binary → abstain).
    """
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
    # An applicability record is a job=applies record that declares this dimension.
    # Engine B carries the declared dimension on the detector, not the record, so we
    # resolve it back through the registry (the same snapshot the scan used).
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
    """Run the job-3 smell/metric scan and return the filtered evidence records.

    Wraps :func:`engine_b.scan` (every applicable detector over the repo) and
    narrows the result by any of:

    * ``detectors`` — keep only records whose ``detector_id`` is in this set;
    * ``dimensions`` — keep only records from detectors declaring one of these
      dimensions (each validated against :data:`DIMENSIONS`; an unknown one matches
      nothing). Resolved through the registry the scan used;
    * ``path_globs`` — keep only records whose ``location.file`` matches one of
      these globs (records with no location — e.g. coverage-only skips — are kept,
      since a skip is not file-scoped).

    ``None`` for a filter means "no restriction" (all applicable). Returns the
    list of normalized evidence records (matches AND fail-open skips); the list is
    the complete, self-describing account of what ran and what did not.
    """
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
    """Return the STATIC, repo-independent integration contract the oracle exposes.

    A fast, deterministic snapshot a consumer (``5fd2``/``9da1``) uses to discover
    the vocabulary it must draw from — the closed reason enum, the dimension
    vocabulary + its version, the reference kinds, and the backends with their
    DETECTED availability/version. No repo is scanned; only tool-version probes
    run (each fail-open). This is the shape the ``grounding-info`` read tool emits.
    """
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
    """One ``{name, available, version}`` entry per oracle backend, fail-open.

    Each probe is a best-effort version/PATH check; an absent tool reports
    ``available=False`` with a null version (never a raise), which is exactly the
    fail-open posture the oracle takes at run time.
    """
    out: list[dict[str, Any]] = []

    # T1 ctags lane (Engine A).
    ctags_ver = _safe(resolve.ctags_version)
    out.append(
        {"name": resolve.BACKEND_CTAGS, "available": ctags_ver is not None, "version": ctags_ver}
    )
    # The plain-filesystem refute backend is always available (no external tool).
    out.append({"name": resolve.BACKEND_FS, "available": True, "version": None})

    # Engine B backends (opengrep/ast-grep/metric) — resolved by PATH probe.
    for backend, candidates in (
        (engine_b.BACKEND_OPENGREP, engine_b._OPENGREP_CANDIDATES),
        (engine_b.BACKEND_ASTGREP, engine_b._ASTGREP_CANDIDATES),
        (engine_b.BACKEND_METRIC, engine_b._METRIC_CANDIDATES),
    ):
        binary = _safe(lambda c=candidates: engine_b._resolve_binary(c))
        version = _safe(lambda b=binary: engine_b._binary_version(b)) if binary else None
        out.append({"name": backend, "available": binary is not None, "version": version})

    # T0 deps lane: a network registry oracle (deps.dev). Reachability is not
    # probed here (it would make a network call); it is reported as a backend whose
    # availability is "best-effort at run time".
    out.append({"name": "registry", "available": True, "version": None})
    return out


def _safe(fn: Any) -> Any:
    """Call ``fn`` and swallow any exception to None (the contract probe never raises)."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a contract probe must never fail the read tool
        return None


__all__ = [
    "DIMENSIONS",
    "DIMENSIONS_VERSION",
    "REFERENCE_KINDS",
    "BACKENDS",
    "is_known_dimension",
    "refute_absence",
    "applies",
    "scan",
    "contract",
]
