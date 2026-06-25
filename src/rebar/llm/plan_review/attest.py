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

from typing import Any

_MANIFEST_PREFIX = "plan-review"


# ── manifest ─────────────────────────────────────────────────────────────────────
def build_manifest(verdict: dict[str, Any], *, material: str) -> list[str]:
    """The deterministic manifest signed for a passing plan-review verdict. The
    signature binds ``(ticket_id, manifest)``; the manifest records the verdict, the
    material fingerprint (for material-edit invalidation), and provenance. No
    timestamps, so re-signing the same verified state is reproducible."""
    counts = (verdict.get("coverage", {}) or {}).get("counts", {}) or {}
    return [
        f"{_MANIFEST_PREFIX}: {verdict.get('verdict', 'PASS')}",
        f"ticket: {verdict.get('ticket_id', '')}",
        f"material: {material}",
        f"model: {verdict.get('model') or 'n/a'}",
        f"runner: {verdict.get('runner') or 'n/a'}",
        f"blocking: {counts.get('blocking', 0)}",
        f"advisory: {counts.get('advisory_surfaced', 0)}",
    ]


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

    manifest = build_manifest(verdict, material=material)
    return signing.sign_manifest(verdict["ticket_id"], manifest, repo_root=repo_root)


# ── the fast claim-gate check (no LLM, no heavy reads) ────────────────────────────
def claim_gate_check(ticket_id: str, *, repo_root=None) -> dict[str, Any]:
    """The fast, local claim-path check. Returns
    ``{ok: bool, reason: str, verdict: str, ...}``.

    ``ok`` is True only when a CERTIFIED plan-review signature exists, is at the
    current code HEAD (not stale), and binds the CURRENT material fingerprint (no
    material edit since the review). This makes NO LLM call and NO network call —
    it is a pure local HMAC verify + a light fingerprint recompute."""
    from rebar import config as _config
    from rebar import signing

    try:
        result = signing.verify_signature(ticket_id, repo_root=repo_root)
    except Exception as exc:  # signing subsystem unavailable → fail-closed at the gate
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
    # Code-HEAD freshness (same binding the close gate uses).
    head = signing.head_sha(_config.repo_root(repo_root))
    if head == "unknown" or result.get("head_sha") != head:
        return {
            "ok": False,
            "reason": f"attestation is stale (signed at {result.get('head_sha')}, HEAD is {head})",
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
        except Exception:
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
    except Exception:
        return None
