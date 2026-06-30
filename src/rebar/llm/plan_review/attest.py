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
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_MANIFEST_PREFIX = "plan-review"
_DEP_PREFIX = "dep"
_REGVER_PREFIX = "regver:"  # criteria-registry version stamp (progressive drift-refresh, ADR 0002)
_REFRESHED_PREFIX = "refreshed-from:"  # provenance on a drift-refreshed attestation
_ABSENT_HASH = "absent"  # sentinel for a dependency path that does not exist on disk


def registry_version() -> str:
    """A short, deterministic stamp of the criteria registry the review ran against
    (the canonical DET + LLM id sets + the routing index). Bound into the manifest so
    a progressive drift-refresh can detect that the registry changed since signing
    (version skew) and fall back to a FULL re-review instead of reusing the verdict."""
    from . import registry

    try:
        routing = json.dumps(registry._routing_index(), sort_keys=True)
    except Exception:  # noqa: BLE001 — routing unreadable → stamp the id sets alone; still detects drift
        routing = ""
    basis = json.dumps(
        {
            "det": sorted(registry.CANONICAL_DET),
            "llm": sorted(registry.CANONICAL_LLM),
            "grounded": sorted(registry.CODEBASE_GROUNDED),
            "routing": routing,
        },
        sort_keys=True,
    )
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
    version stamp (for progressive-refresh skew detection), and provenance. No
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
    if regver:
        lines.append(f"{_REGVER_PREFIX} {regver}")
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


def sign_plan_review(verdict: dict[str, Any], *, material: str, repo_root=None) -> dict[str, Any]:
    """Sign a passing plan-review verdict (append a ``SIGNATURE`` event). Returns the
    signature record. Raises if signing fails (the caller decides how to surface it)."""
    from rebar import signing
    from rebar.llm.config import current_code_sha

    deps = dependency_hashes(verdict, repo_root=repo_root)
    # Pin the snapshot SHA the deps were hashed at (attested review only — current_code_sha
    # is None in local mode), so the claim gate re-hashes the SAME basis (shared boundary).
    manifest = build_manifest(
        verdict,
        material=material,
        deps=deps,
        regver=registry_version(),
        verified_at_sha=current_code_sha(),
    )
    return signing.sign_manifest(verdict["ticket_id"], manifest, repo_root=repo_root)


def _rehash(paths, *, repo_root=None, pinned_sha: str | None = None) -> dict[str, str]:
    """Re-hash the given dependency paths through the shared :func:`_hash_basis` boundary
    (the pinned-SHA snapshot when given, else the active snapshot / working tree)."""
    base = _hash_basis(repo_root, pinned_sha=pinned_sha)
    return {p: _hash_file(p, base=base) for p in sorted(paths)}


def drift_refresh_candidate(ticket_id: str, *, repo_root=None) -> dict[str, Any] | None:
    """Decide whether ``ticket_id`` is a REFRESHABLE drift-only-stale attestation: a
    certified plan-review PASS whose material fingerprint and registry stamp still match,
    but whose signed dependency files have drifted. Returns
    ``{"manifest", "deps", "key_id"}`` for the progressive path, or None (→ full review)
    when there is nothing safely reusable (no signature, wrong manifest, material edited,
    registry skew, no scoped deps, or no actual drift)."""
    from rebar import signing

    try:
        result = signing.verify_signature(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — signing unavailable → no refresh, fall back to full review
        return None
    if not result.get("verified"):
        return None
    manifest = result.get("manifest")
    if not is_plan_review_manifest(manifest):
        return None
    # Registry skew → the probe's meaning may have changed → full review.
    if manifest_regver(manifest) != registry_version():
        return None
    # Material edit is a separate invalidation (handled by the material-fingerprint gate).
    signed_material = manifest_material(manifest)
    if signed_material is None or signed_material != current_material_fingerprint(
        ticket_id, repo_root=repo_root
    ):
        return None
    deps = manifest_deps(manifest)
    if not deps:  # unscoped attestation — nothing to probe against; full review
        return None
    current = _rehash(deps.keys(), repo_root=repo_root)
    if current == deps:  # no drift → not a drift re-review at all
        return None
    return {"manifest": manifest, "deps": deps, "key_id": result.get("key_id")}


def remediation_mode_candidate(
    ticket_id: str, *, window_minutes: int, now_ns: int | None = None, repo_root=None
) -> dict[str, Any]:
    """Decide whether a re-review of ``ticket_id`` is eligible for REMEDIATION MODE (epic 7d43,
    child ec89): the freshness-window + precondition check that gates the Pass-3 rising floor
    (the drop math itself is child cc5b — this only decides eligibility). The complement of
    :func:`drift_refresh_candidate` on the material axis: drift-refresh wants the plan UNCHANGED +
    code drifted; remediation wants the plan CHANGED + code UNCHANGED.

    Returns a DECISION dict — ``{"eligible": bool, "reasons": {precondition: bool, ...}}`` — never
    raises (any read error → that precondition False → ``eligible=False`` → full review). The
    preconditions (ALL required):

    - ``signed`` — a certified prior plan-review signature exists (the baseline the SHA/regver/
      material are read from).
    - ``plan_changed`` — the current plan material fingerprint differs from the prior signed one
      (an edited plan — else there is nothing to re-review under the floor).
    - ``code_unchanged`` — the current run's ``verified_at_sha`` equals the prior signed one (the
      reviewed code did not drift; reuses the already-signed snapshot ref, no new diff machinery).
    - ``registry_unchanged`` — the criteria-routing registry version equals the prior signed one.
    - ``prior_sidecar`` — a prior ``REVIEW_RESULT`` sidecar WITH finding text is available (the
      Pass-2 novelty sub-call's prior findings — child e344).
    - ``within_window`` — the last review of ANY kind (the newest sidecar) is within
      ``window_minutes``, measured from that last review (RESET on each review).

    Code-changed / both-drifted → not eligible → the caller runs a normal full review (the
    ``drift_refresh`` path is untouched)."""
    from rebar import signing
    from rebar.llm.config import current_code_sha

    from . import sidecar

    reasons: dict[str, bool] = {
        "signed": False,
        "plan_changed": False,
        "code_unchanged": False,
        "registry_unchanged": False,
        "prior_sidecar": False,
        "within_window": False,
    }
    try:
        result = signing.verify_signature(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — signing unavailable → not eligible, full review
        return {"eligible": False, "reasons": reasons}
    manifest = result.get("manifest") if result.get("verified") else None
    if not is_plan_review_manifest(manifest):
        return {"eligible": False, "reasons": reasons}
    reasons["signed"] = True

    # plan CHANGED: the current material fingerprint differs from the prior signed one.
    signed_material = manifest_material(manifest)
    current_material = current_material_fingerprint(ticket_id, repo_root=repo_root)
    reasons["plan_changed"] = (
        signed_material is not None
        and current_material is not None
        and current_material != signed_material
    )

    # code UNCHANGED: current verified_at_sha equals the prior signed one (deterministic, reusing
    # the signed snapshot ref). Both must be present and equal — a local-mode (None) review on
    # either side is not a reliable code-unchanged signal, so it is treated as changed.
    signed_sha = signing.verified_at_sha_from_manifest(manifest)
    current_sha = current_code_sha()
    reasons["code_unchanged"] = bool(signed_sha) and signed_sha == current_sha

    # registry UNCHANGED: the criteria-routing version equals the prior signed one.
    reasons["registry_unchanged"] = manifest_regver(manifest) == registry_version()

    # prior REVIEW_RESULT sidecar WITH finding text available (child e344).
    prior = sidecar.latest_review_result(ticket_id, repo_root=repo_root)
    reasons["prior_sidecar"] = bool(
        prior and any((f.get("finding") or "").strip() for f in prior.get("findings", []) or [])
    )

    # within the freshness window, measured from the last review of ANY kind (newest sidecar);
    # each review emits a sidecar, so the window RESETS on every review.
    last_ts = sidecar.latest_review_timestamp(ticket_id, repo_root=repo_root)
    if last_ts is not None:
        current_ns = now_ns if now_ns is not None else time.time_ns()
        reasons["within_window"] = (
            0 <= (current_ns - last_ts) <= window_minutes * 60 * 1_000_000_000
        )

    return {"eligible": all(reasons.values()), "reasons": reasons}


def refresh_attestation(
    ticket_id: str, prior_manifest: list[str], *, probe: str, repo_root=None
) -> dict[str, Any]:
    """Re-sign a drift-refreshed attestation: the PRIOR verdict (verdict/material/
    model/runner/counts) re-bound to the CURRENT hashes of the SAME dependency paths,
    with a ``refreshed-from`` provenance line + the current registry stamp. Reuses the
    prior signed paths (authoritative) rather than re-deriving the set."""
    from rebar import signing

    fields = {
        "verdict": "PASS",
        "ticket_id": ticket_id,
        "model": _manifest_field(prior_manifest, "model:"),
        "runner": _manifest_field(prior_manifest, "runner:"),
        "coverage": {
            "counts": {
                "blocking": _manifest_int(prior_manifest, "blocking:"),
                "advisory_surfaced": _manifest_int(prior_manifest, "advisory:"),
            }
        },
    }
    prior_digest = signing.verify_signature(ticket_id, repo_root=repo_root).get("key_id", "?")
    new_deps = _rehash(manifest_deps(prior_manifest).keys(), repo_root=repo_root)
    manifest = build_manifest(
        fields,
        material=manifest_material(prior_manifest) or "",
        deps=new_deps,
        regver=registry_version(),
        refreshed_from=f"{prior_digest} probe={probe}",
    )
    return signing.sign_manifest(ticket_id, manifest, repo_root=repo_root)


def _manifest_field(manifest: list[str] | None, prefix: str) -> str:
    for line in manifest or []:
        if str(line).startswith(prefix):
            return str(line).split(":", 1)[1].strip()
    return "n/a"


def _manifest_int(manifest: list[str] | None, prefix: str) -> int:
    try:
        return int(_manifest_field(manifest, prefix))
    except (TypeError, ValueError):
        return 0


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
        # Re-hash through the SHARED boundary at the SAME pinned-SHA basis the attestation
        # signed against (the signature's verified_at_sha), so the claim gate and plan-review
        # cannot diverge (whole-HEAD vs pinned-SHA) — epic raze-vet-ditch S4b. A local/legacy
        # attestation has no pin → both resolve to the working tree (the back-out).
        pinned = signing.verified_at_sha_from_manifest(manifest)
        base = _hash_basis(repo_root, pinned_sha=pinned)
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
