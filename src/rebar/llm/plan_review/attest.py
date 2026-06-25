"""Plan-review attestation + the fast claim-gate check (children 4bb7, 092b).

The attestation reuses the close-gate signing machinery verbatim (HMAC-SHA256 under
the environment key; the ``SIGNATURE`` event; ``head_sha`` git-state binding) — no
new key custody. A plan-review signature is distinguished from a completion
signature by its MANIFEST (the first line is ``plan-review: …``), and it additionally
binds the ticket's MATERIAL fingerprint so a material edit
(description / AC / file_impact / decomposition) invalidates it — exactly the
invalidation-on-material-edit the epic requires, layered on top of the code-HEAD
freshness binding.

The claim path is a FAST, LOCAL check only — no LLM, no network beyond a couple of
local reads — so it stays well within the ~50ms target. The heavy four-pass review
runs OUT-OF-BAND via ``rebar review-plan`` (which signs on a non-blocking result);
``claim`` only verifies a fresh, non-stale, material-matching plan-review signature
exists. ``--force`` (with a justification) bypasses it and is audit-logged.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_MANIFEST_PREFIX = "plan-review"
_DEP_PREFIX = "dep"
_ABSENT_HASH = "absent"  # sentinel for a dependency path that does not exist on disk


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


def dependency_hashes(verdict: dict[str, Any], *, repo_root=None) -> dict[str, str]:
    """The signed dependency set: ``{path: sha256}`` for the union of the ticket's
    declared ``file_impact`` and the files the review CITED (``kind=file``), hashed
    from the working tree. Sorted for reproducible signing. Empty when nothing is
    declared/cited — the claim gate then falls back to whole-HEAD freshness."""
    import rebar
    from rebar import config as _config

    ticket_id = verdict.get("ticket_id", "")
    paths: set[str] = set(_cited_paths(verdict))
    try:
        for entry in rebar.get_file_impact(ticket_id, repo_root=repo_root) or []:
            p = entry.get("path") if isinstance(entry, dict) else None
            if p:
                paths.add(str(p))
    except Exception:  # noqa: BLE001 — file_impact read is best-effort; broad-but-logged below
        logger.warning("file_impact read failed for %s; scoping to citations only", ticket_id)
    base = str(_config.repo_root(repo_root))
    return {p: _hash_file(p, base=base) for p in sorted(paths)}


# ── manifest ─────────────────────────────────────────────────────────────────────
def build_manifest(
    verdict: dict[str, Any], *, material: str, deps: dict[str, str] | None = None
) -> list[str]:
    """The deterministic manifest signed for a passing plan-review verdict. The
    signature binds ``(ticket_id, manifest)``; the manifest records the verdict, the
    material fingerprint (for material-edit invalidation), the per-path code-drift
    dependency map (for code-drift invalidation, ADR 0002), and provenance. No
    timestamps, so re-signing the same verified state is reproducible."""
    counts = (verdict.get("coverage", {}) or {}).get("counts", {}) or {}
    lines = [
        f"{_MANIFEST_PREFIX}: {verdict.get('verdict', 'PASS')}",
        f"ticket: {verdict.get('ticket_id', '')}",
        f"material: {material}",
        f"model: {verdict.get('model') or 'n/a'}",
        f"runner: {verdict.get('runner') or 'n/a'}",
        f"blocking: {counts.get('blocking', 0)}",
        f"advisory: {counts.get('advisory_surfaced', 0)}",
    ]
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


def is_plan_review_manifest(manifest: list[str] | None) -> bool:
    return bool(manifest) and str(manifest[0]).startswith(_MANIFEST_PREFIX + ":")


def manifest_material(manifest: list[str] | None) -> str | None:
    """Extract the bound material fingerprint from a signed manifest, if present."""
    for line in manifest or []:
        if str(line).startswith("material:"):
            return str(line).split(":", 1)[1].strip()
    return None


def sign_plan_review(verdict: dict[str, Any], *, material: str, repo_root=None) -> dict[str, Any]:
    """Sign a passing plan-review verdict (append a ``SIGNATURE`` event). Returns the
    signature record. Raises if signing fails (the caller decides how to surface it)."""
    from rebar import signing

    deps = dependency_hashes(verdict, repo_root=repo_root)
    manifest = build_manifest(verdict, material=material, deps=deps)
    return signing.sign_manifest(verdict["ticket_id"], manifest, repo_root=repo_root)


# ── the fast claim-gate check (no LLM, no heavy reads) ────────────────────────────
def claim_gate_check(ticket_id: str, *, repo_root=None) -> dict[str, Any]:
    """The fast, local claim-path check. Returns
    ``{ok: bool, reason: str, verdict: str, ...}``.

    ``ok`` is True only when a CERTIFIED plan-review signature exists, its reviewed
    code has not drifted (the signed per-path dependency hashes still match the working
    tree — or, when unscoped, the code is still at the signed HEAD), and it binds the
    CURRENT material fingerprint (no material edit since the review). This makes NO LLM
    call and NO network call — a pure local HMAC verify + a light fingerprint recompute
    + hashing a handful of dependency files."""
    from rebar import config as _config
    from rebar import signing

    try:
        result = signing.verify_signature(ticket_id, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 — signing subsystem unavailable → fail-closed at the gate; broad-but-logged
        # Fail closed (the gate denies the claim) but log: a broken signing subsystem
        # is an operator-actionable failure, not a routine denial.
        logger.warning("signing unavailable; failing the claim gate closed", exc_info=True)
        return {"ok": False, "reason": f"signing-unavailable: {exc}", "verdict": "error"}

    if not result.get("verified"):
        return {
            "ok": False,
            "reason": f"no certified plan-review attestation (signature: {result.get('verdict')})",
            "verdict": result.get("verdict", "unsigned"),
        }
    manifest = result.get("manifest")
    if not is_plan_review_manifest(manifest):
        return {
            "ok": False,
            "reason": "the ticket's signature is not a plan-review attestation",
            "verdict": "wrong-manifest",
        }
    # Code-drift freshness (ADR 0002). Scope to the review's dependency files when the
    # attestation carries a signed {path: hash} map: re-hash exactly those paths (from
    # the SIGNED map — never re-derived from current ticket state, which a post-sign
    # file_impact shrink could shrink to evade detection) and invalidate iff any drifted.
    # When the map is empty (nothing declared/cited, or an attestation predating ADR
    # 0002) we cannot scope, so fall back to conservative whole-HEAD freshness.
    deps = manifest_deps(manifest)
    if deps:
        base = str(_config.repo_root(repo_root))
        drifted = [p for p, digest in sorted(deps.items()) if _hash_file(p, base=base) != digest]
        if drifted:
            shown = ", ".join(drifted[:5]) + (" …" if len(drifted) > 5 else "")
            return {
                "ok": False,
                "reason": (
                    f"the code the plan was reviewed against drifted: "
                    f"{len(drifted)} dependency file(s) changed ({shown})"
                ),
                "verdict": "stale-code",
            }
    else:
        head = signing.head_sha(_config.repo_root(repo_root))
        if head == "unknown" or result.get("head_sha") != head:
            return {
                "ok": False,
                "reason": (
                    f"attestation is stale (unscoped; signed at {result.get('head_sha')}, "
                    f"HEAD is {head})"
                ),
                "verdict": "stale-head",
            }
    # Material-edit invalidation. The gate is ENABLED here, so an inability to
    # recompute the current fingerprint must FAIL CLOSED (we cannot certify the plan
    # is unchanged) — never silently pass. --force is the operator's escape.
    signed = manifest_material(manifest)
    if signed is not None:
        current = current_material_fingerprint(ticket_id, repo_root=repo_root)
        if current is None:
            return {
                "ok": False,
                "reason": "could not recompute the plan's material fingerprint to check for edits",
                "verdict": "unverifiable-material",
            }
        if signed != current:
            return {
                "ok": False,
                "reason": (
                    "the plan was materially edited since review "
                    "(description/AC/file_impact/children changed)"
                ),
                "verdict": "stale-material",
            }
    return {"ok": True, "reason": "certified plan-review attestation", "verdict": "certified"}


def current_material_fingerprint(ticket_id: str, *, repo_root=None) -> str | None:
    """Recompute the ticket's material fingerprint from a LIGHT read (the ticket +
    its child ids only — no full child fetch, no LLM), matching
    :func:`orchestrator.material_fingerprint`. Returns None on any read error
    (so a read failure never wrongly invalidates — the head_sha + certified checks
    still gate)."""
    import rebar

    from .det_floor import PlanContext
    from .orchestrator import material_fingerprint

    try:
        state = rebar.show_ticket(ticket_id, repo_root=repo_root)
        canonical = state.get("ticket_id", ticket_id)
        try:
            kids = rebar.list_tickets(parent=canonical, repo_root=repo_root) or []
        except Exception:  # noqa: BLE001 — children enumeration is best-effort for the fingerprint
            kids = []
        ctx = PlanContext(
            ticket_id=canonical,
            ticket_type=state.get("ticket_type", ""),
            title=state.get("title", ""),
            description=state.get("description", ""),
            state=state,
            children=[{"ticket_id": k.get("ticket_id")} for k in kids],
        )
        return material_fingerprint(ctx)
    except Exception:  # noqa: BLE001 — fingerprint computation best-effort; broad-but-logged below, caller treats material as unknown
        # Cannot compute the current fingerprint → caller treats material as unknown
        # (the gate fails closed / re-review). Log so the cause is observable.
        logger.warning("could not compute material fingerprint for %s", ticket_id, exc_info=True)
        return None
