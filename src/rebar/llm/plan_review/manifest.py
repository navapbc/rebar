"""Plan-review manifest construction + dependency-hashing (extracted from ``attest``).

This module holds the PURE manifest/hashing seam of the plan-review attestation: the
criteria-registry version stamp, the code-drift dependency-hashing helpers, the
deterministic manifest builder, and the manifest field-parsers. It is deliberately
dependency-light (only ``rebar.*`` + the sibling ``registry`` module) and must NEVER
import :mod:`rebar.llm.plan_review.attest` — ``attest`` imports (and re-exports) from
here, so the dependency edge points one way (attest → manifest) with no cycle.

Every public name here is re-exported from ``attest`` so existing import paths
(``rebar.llm.plan_review.attest.build_manifest`` etc.) keep working unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_MANIFEST_PREFIX = "plan-review"
_DEP_PREFIX = "dep"
_REGVER_PREFIX = "regver:"  # criteria-registry version stamp (progressive drift-refresh, ADR 0002)
_REFRESHED_PREFIX = "refreshed-from:"  # provenance on a drift-refreshed attestation
_DISABLED_PREFIX = "disabled_builtins:"  # built-in ids the project overlay disabled (story 08af)
_ABSENT_HASH = "absent"  # sentinel for a dependency path that does not exist on disk


def registry_version(repo_root=None) -> str:
    """A short, deterministic stamp of the criteria registry the review ran against
    (the canonical DET + LLM id sets + the routing index). Bound into the manifest so
    a progressive drift-refresh can detect that the registry changed since signing
    (version skew) and fall back to a FULL re-review instead of reusing the verdict.

    OVERLAY-AWARE (story 08af): with ``repo_root`` given, the stamp hashes the repo's
    EFFECTIVE routing (packaged ⊕ the ``.rebar/criteria_routing.json`` overlay) plus the
    overlay's activated-project ids and disabled-built-in set — so activating / re-tuning /
    disabling a project criterion changes the stamp, which the claim gate reads as
    ``stale-regver`` (invalidating a prior plan-review attestation). With ``repo_root=None``,
    OR a repo with NO overlay, the basis is BYTE-IDENTICAL to the historical packaged stamp
    (``activated`` / ``disabled`` are only added when non-empty), so existing attestations —
    signed before this change — stay valid (zero churn)."""
    from . import registry

    activated: list[str] = []
    disabled: list[str] = []
    try:
        if repo_root is None:
            routing_obj: dict = registry._routing_index()
        else:
            routing_obj = registry.effective_routing(repo_root)
            disabled = registry.disabled_builtins(repo_root)
            activated = sorted(
                c for c in registry.effective_criteria(repo_root) if c.startswith("project.")
            )
        routing = json.dumps(routing_obj, sort_keys=True)
    except Exception:  # noqa: BLE001 — routing unreadable → stamp the id sets alone; still detects drift
        routing = ""
        activated = []
        disabled = []
    # The overlay dimensions are added ONLY when non-empty so an overlay-absent repo hashes
    # to the SAME basis as the packaged (repo_root=None) stamp — preserving back-compat.
    basis_obj: dict[str, Any] = {
        "det": sorted(registry.CANONICAL_DET),
        "llm": sorted(registry.CANONICAL_LLM),
        "grounded": sorted(registry.CODEBASE_GROUNDED),
        "routing": routing,
    }
    if activated:
        basis_obj["activated"] = activated
    if disabled:
        basis_obj["disabled"] = disabled
    basis = json.dumps(basis_obj, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# ── code-drift dependency set (epic boil-golem-veto / ADR 0002) ───────────────────
def _hash_file(path: str, *, base: str) -> str:
    """SHA-256 of the WORKING-TREE file's raw bytes (no normalization) — the bytes the
    review actually grounds against. A missing/unreadable path hashes to ``_ABSENT_HASH``
    so a later create/delete is itself a detectable change. ``base`` is the repo root a
    relative ``path`` is resolved against."""
    full = path if os.path.isabs(path) else os.path.join(base, path)
    try:
        with open(full, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return _ABSENT_HASH


def _cited_paths(verdict: dict[str, Any]) -> set[str]:
    """The ``kind == "file"`` citation paths across every finding bucket of the
    IN-MEMORY verdict (the persisted REVIEW_RESULT sidecar slims paths out, so the
    verdict is the only complete source). Free-text citations with no ``path`` are
    ignored, never guessed."""
    out: set[str] = set()
    for bucket in ("blocking", "advisory", "coaching", "indeterminate", "dropped", "overflow"):
        for finding in verdict.get(bucket) or []:
            if not isinstance(finding, dict):
                continue
            for cit in finding.get("citations") or []:
                if isinstance(cit, dict) and cit.get("kind") == "file" and cit.get("path"):
                    out.add(str(cit["path"]))
    return out


def _hash_basis(repo_root=None, *, pinned_sha: str | None = None) -> str:
    """The ONE shared ref-resolution boundary (epic raze-vet-ditch S4b) that BOTH the
    plan-review signing-time hashing AND the claim-gate freshness re-check resolve through,
    so they cannot diverge (whole-HEAD vs pinned-SHA) and re-introduce the staleness
    false-positive ADR 0002 prevents.

    Resolution (single source):
      * ``pinned_sha`` given (the claim gate, reading the signature's ``verified_at_sha``) →
        the materialized snapshot at that SHA (a cache hit when the review's snapshot is
        still warm; a local ``read-tree`` otherwise — no network when the objects are
        present). If it cannot be materialized, degrade to the working tree (the gate then
        fails CLOSED on any drift — the conservative direction).
      * else the active attested gate snapshot (``current_code_root``, set during an attested
        ``review_plan``) → the same snapshot the signature was produced against.
      * else the in-place checkout (``_config.repo_root``) — the local / back-out basis.

    BACK-OUT: a plan-review signed in local mode (or pre-S4b) carries no ``verified_at_sha``;
    both sides then resolve to the working tree exactly as before this consolidation."""
    from rebar import config as _config

    if pinned_sha:
        try:
            from rebar._snapshot import cache as _cache

            handle = _cache.acquire(
                pinned_sha, source_mode="attested", repo_root=repo_root, fetch=False
            )
            return str(handle.path)
        except Exception:  # noqa: BLE001 — snapshot unavailable → degrade to the working tree (never crash the gate)
            logger.warning(
                "snapshot for pinned sha %s unavailable; hashing the working tree", pinned_sha
            )
    from rebar.llm.config import current_code_root

    active = current_code_root()
    return active if active else str(_config.repo_root(repo_root))


def dependency_hashes(verdict: dict[str, Any], *, repo_root=None) -> dict[str, str]:
    """The signed dependency set: ``{path: sha256}`` for the union of the ticket's
    declared ``file_impact`` and the files the review CITED (``kind=file``), hashed
    from the working tree. Sorted for reproducible signing. Empty when nothing is
    declared/cited — the claim gate then falls back to whole-HEAD freshness."""
    import rebar

    ticket_id = verdict.get("ticket_id", "")
    paths: set[str] = set(_cited_paths(verdict))
    try:
        for entry in rebar.get_file_impact(ticket_id, repo_root=repo_root) or []:
            p = entry.get("path") if isinstance(entry, dict) else None
            if p:
                paths.add(str(p))
    except Exception:  # noqa: BLE001 — file_impact read is best-effort; broad-but-logged below
        logger.warning("file_impact read failed for %s; scoping to citations only", ticket_id)
    # Hash through the shared boundary: during an attested review this is the pinned-SHA
    # snapshot (the claim gate re-hashes the SAME basis); in local mode it is the checkout.
    base = _hash_basis(repo_root)
    return {p: _hash_file(p, base=base) for p in sorted(paths)}


# ── manifest ─────────────────────────────────────────────────────────────────────
def build_manifest(
    verdict: dict[str, Any],
    *,
    material: str,
    deps: dict[str, str] | None = None,
    regver: str | None = None,
    refreshed_from: str | None = None,
    verified_at_sha: str | None = None,
) -> list[str]:
    """The deterministic manifest signed for a passing plan-review verdict. The
    signature binds ``(ticket_id, manifest)``; the manifest records the verdict, the
    material fingerprint (for material-edit invalidation), the per-path code-drift
    dependency map (for code-drift invalidation, ADR 0002), the criteria-registry
    version stamp (for progressive-refresh skew detection), and provenance (including a
    ``rebar-version:`` stamp of the gate code that signed — audit-only, stable for a given
    rebar build). No timestamps, so re-signing the same verified state is reproducible."""
    from rebar import signing as _signing

    counts = (verdict.get("coverage", {}) or {}).get("counts", {}) or {}
    lines = [
        f"{_MANIFEST_PREFIX}: {verdict.get('verdict', 'PASS')}",
        f"ticket: {verdict.get('ticket_id', '')}",
        f"material: {material}",
        f"model: {verdict.get('model') or 'n/a'}",
        f"runner: {verdict.get('runner') or 'n/a'}",
        f"blocking: {counts.get('blocking', 0)}",
        f"advisory: {counts.get('advisory_surfaced', 0)}",
        # Which rebar gate code produced this attestation (audit/provenance, epic
        # jira-reb-596). NEVER read by compute_validity.
        _signing.rebar_version_step(_signing.gate_code_version()),
    ]
    if regver:
        lines.append(f"{_REGVER_PREFIX} {regver}")
    # Record the built-in criteria the project overlay DISABLED for this review (sorted,
    # deterministic). Additive — absent on a clean run, so the manifest is byte-identical to
    # a pre-08af manifest when the overlay disables nothing (story 08af).
    disabled = sorted((verdict.get("coverage", {}) or {}).get("disabled_builtins") or [])
    if disabled:
        lines.append(f"{_DISABLED_PREFIX} {','.join(disabled)}")
    if refreshed_from:
        lines.append(f"{_REFRESHED_PREFIX} {refreshed_from}")
    # Pin the snapshot SHA the dep hashes were computed against (epic raze-vet-ditch S4b),
    # so the claim gate re-hashes at the SAME basis via the shared boundary. Only present
    # for an attested review; a local review omits it (both sides then use the checkout).
    if verified_at_sha:
        from rebar import signing as _signing

        lines.append(_signing.verified_at_sha_step(verified_at_sha))
    # Per-path dependency hashes (sorted), one line each: ``dep <sha256> <path>``.
    # The hash is fixed-width so the path (which may contain spaces) is an unambiguous
    # remainder. A per-path map (not a rolled-up root) is the contract Story 2 builds on.
    for path, digest in sorted((deps or {}).items()):
        lines.append(f"{_DEP_PREFIX} {digest} {path}")
    return lines


def manifest_deps(manifest: list[str] | None) -> dict[str, str]:
    """Parse the signed ``{path: sha256}`` dependency map back out of a manifest
    ({} when none — e.g. an attestation signed before ADR 0002)."""
    out: dict[str, str] = {}
    for line in manifest or []:
        s = str(line)
        if s.startswith(_DEP_PREFIX + " "):
            _, _, rest = s.partition(" ")
            digest, _, path = rest.partition(" ")
            if path:
                out[path] = digest
    return out


def manifest_regver(manifest: list[str] | None) -> str | None:
    """The criteria-registry version stamp from a manifest (None if pre-stamp)."""
    for line in manifest or []:
        if str(line).startswith(_REGVER_PREFIX):
            return str(line).split(":", 1)[1].strip()
    return None


def manifest_rebar_version(manifest: list[str] | None) -> str | None:
    """The gate-code version+SHA provenance stamp from a manifest, or ``None`` when the
    manifest predates the stamp (epic jira-reb-596). Audit-only — thin re-export of
    :func:`rebar.signing.rebar_version_from_manifest` co-located with the other manifest
    parsers."""
    from rebar import signing as _signing

    return _signing.rebar_version_from_manifest(manifest)


def manifest_disabled_builtins(manifest: list[str] | None) -> list[str]:
    """The sorted built-in ids the overlay disabled at signing time, parsed from a manifest
    (``[]`` when the line is absent — a clean run or a pre-08af attestation)."""
    for line in manifest or []:
        s = str(line)
        if s.startswith(_DISABLED_PREFIX):
            rest = s.split(":", 1)[1].strip()
            return sorted(x for x in (p.strip() for p in rest.split(",")) if x)
    return []


def is_plan_review_manifest(manifest: list[str] | None) -> bool:
    if not manifest:
        return False
    return str(manifest[0]).startswith(_MANIFEST_PREFIX + ":")


def manifest_material(manifest: list[str] | None) -> str | None:
    """Extract the bound material fingerprint from a signed manifest, if present."""
    for line in manifest or []:
        if str(line).startswith("material:"):
            return str(line).split(":", 1)[1].strip()
    return None
