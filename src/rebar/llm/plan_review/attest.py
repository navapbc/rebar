"""Plan-review signing and fast local claim-gate validity checks."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any

# Re-export manifest helpers so historical ``attest.<name>`` imports remain stable.
from .manifest import (
    _ABSENT_HASH,
    _DEP_PREFIX,
    _DISABLED_PREFIX,
    _MANIFEST_PREFIX,
    _REFRESHED_PREFIX,
    _REGVER_PREFIX,
    ManifestFormatError,
    _cited_paths,
    _hash_basis,
    _hash_file,
    build_manifest,
    dependency_hashes,
    is_plan_review_manifest,
    manifest_deps,
    manifest_disabled_builtins,
    manifest_material,
    manifest_pins,
    manifest_priority_floor,
    manifest_rebar_version,
    manifest_regver,
    manifest_review_phase,
    registry_version,
    validate_review_phase_metadata,
)
from .pin_health import DerivedPlanMaterialPinHealth, PlanValidityProfile
from .relation_snapshot import PlanMaterialPin

logger = logging.getLogger(__name__)


def _read_enforce_plan_material_pins(repo_root=None) -> bool:
    from .pin_health import read_enforcement

    return read_enforcement(repo_root)


def derive_plan_material_pin_health(
    pin_records: Sequence[PlanMaterialPin] | None, *, repo_root, enforced: bool
) -> DerivedPlanMaterialPinHealth:
    """Return additive related-material health using the public fingerprint seam."""
    from .pin_health import derive_health

    return derive_health(
        pin_records,
        repo_root=repo_root,
        enforced=enforced,
        fingerprint=current_material_fingerprint,
    )


__all__ = [
    "_ABSENT_HASH",
    "_DEP_PREFIX",
    "_DISABLED_PREFIX",
    "_MANIFEST_PREFIX",
    "_REFRESHED_PREFIX",
    "_REGVER_PREFIX",
    "_cited_paths",
    "_hash_basis",
    "_hash_file",
    "build_manifest",
    "dependency_hashes",
    "is_plan_review_manifest",
    "manifest_deps",
    "manifest_disabled_builtins",
    "manifest_material",
    "manifest_pins",
    "manifest_rebar_version",
    "manifest_regver",
    "manifest_review_phase",
    "manifest_priority_floor",
    "registry_version",
    "validate_review_phase_metadata",
    "ManifestFormatError",
]


def sign_plan_review(
    verdict: dict[str, Any],
    *,
    material: str,
    review_phase: object = "planning",
    priority_floor: object = None,
    repo_root=None,
    relation_snapshot=None,
    initial_generation=None,
) -> dict[str, Any]:
    """Sign a non-degraded PASS; refuse every non-certifiable verdict."""
    from rebar.signing import SigningError

    _cov = verdict.get("coverage") or {}
    if str(verdict.get("verdict", "")).upper() != "PASS" or _cov.get("resolution_class"):
        raise SigningError(
            "refusing to sign a non-PASS / degraded plan-review verdict "
            f"(verdict={verdict.get('verdict')!r}, "
            f"resolution_class={_cov.get('resolution_class')!r})"
        )

    from rebar import signing
    from rebar.llm.config import current_code_sha

    from . import registry
    from . import relation_snapshot as relation_snapshot_module

    snapshot = (
        initial_generation.relation_snapshot
        if initial_generation is not None
        else relation_snapshot
        or relation_snapshot_module.collect_plan_relation_snapshot(
            verdict["ticket_id"], repo_root=repo_root
        )
    )

    deps = dependency_hashes(verdict, repo_root=repo_root)
    # Stamp disabled built-ins authoritatively at the sign boundary.
    disabled = registry.disabled_builtins(repo_root)
    if disabled:
        verdict.setdefault("coverage", {})["disabled_builtins"] = disabled
    # Bind the dependency snapshot SHA and overlay-aware registry version.
    manifest = build_manifest(
        verdict,
        material=material,
        deps=deps,
        regver=registry_version(repo_root),
        verified_at_sha=current_code_sha(),
        pins=snapshot.related_material,
        review_phase=review_phase,
        priority_floor=priority_floor,
    )
    if initial_generation is not None:
        from . import generation

        if material != initial_generation.own_material:
            raise generation.PlanReviewGenerationChanged("review material changed before signing")
        sig = generation.sign_manifest(
            verdict["ticket_id"], manifest, initial_generation, repo_root=repo_root
        )
    else:
        sig = signing.sign_manifest(
            verdict["ticket_id"], manifest, kind=_MANIFEST_PREFIX, repo_root=repo_root
        )
    # Certification triggers the best-effort overlap-enrichment soak queue.
    try:
        from rebar.llm.config import LLMConfig
        from rebar.llm.overlap import queue as _enqueue_queue

        _enqueue_queue.enqueue(
            verdict["ticket_id"],
            soak_min=LLMConfig.from_env(repo_root=repo_root).overlap_soak_min,
            repo_root=repo_root,
        )
    except Exception:  # noqa: BLE001 — enqueue is best-effort; a failure never fails the sign
        logging.getLogger(__name__).warning(
            "enrichment enqueue on certification failed; continuing", exc_info=True
        )
    return sig


def _rehash(paths, *, repo_root=None, pinned_sha: str | None = None) -> dict[str, str]:
    """Re-hash the given dependency paths through the shared :func:`_hash_basis` boundary
    (the pinned-SHA snapshot when given, else the active snapshot / working tree)."""
    base = _hash_basis(repo_root, pinned_sha=pinned_sha)
    return {p: _hash_file(p, base=base) for p in sorted(paths)}


def drift_refresh_candidate(ticket_id: str, *, repo_root=None) -> dict[str, Any] | None:
    """Return a validity-approved, dependency-drifted progressive-refresh candidate."""
    from rebar import _reads, signing

    try:
        state = _reads.show_ticket(ticket_id, repo_root=repo_root)
        result = signing.verify_signature(ticket_id, kind=_MANIFEST_PREFIX, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — unavailable state/signature → full review
        return None
    validity = compute_validity(
        result,
        state,
        _MANIFEST_PREFIX,
        repo_root=repo_root,
        profile=PlanValidityProfile.DRIFT_REFRESH,
    )
    if not validity.get("valid"):
        return None
    manifest = _authoritative_manifest(result)
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
    """Return fail-safe remediation-floor eligibility and per-precondition reasons.

    Signature baselines are preferred; unsigned BLOCK loops fall back to the latest sidecar.
    Eligibility requires changed plan material, unchanged code/registry, prior finding text,
    and a review inside the configured freshness window. Read failures simply deny the mode.
    """
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
    # A broken precondition signal can only deny remediation mode.
    try:
        # Baseline resolution (story a850): the SIGNATURE branch is authoritative when a valid
        # certified plan-review manifest exists. BOTH no-usable-signature paths (verification
        # error; non-plan-review manifest) fall through to the SIDECAR branch — a BLOCK never
        # signs, so without the fallback the floor was inert in exactly the BLOCK-loop regime.
        manifest = None
        try:
            result = signing.verify_signature(ticket_id, repo_root=repo_root)
            manifest = result.get("manifest") if result.get("verified") else None
        except Exception:  # noqa: BLE001 — a broken signature read falls through to the sidecar branch
            manifest = None
        if not is_plan_review_manifest(manifest):
            return _sidecar_branch_decision(
                ticket_id,
                window_minutes=window_minutes,
                now_ns=now_ns,
                repo_root=repo_root,
            )
        reasons["signed"] = True

        # plan CHANGED: the current material fingerprint differs from the prior signed one.
        signed_material = manifest_material(manifest)
        current_material = current_material_fingerprint(ticket_id, repo_root=repo_root)
        reasons["plan_changed"] = (
            signed_material is not None
            and current_material is not None
            and current_material != signed_material
        )

        # code UNCHANGED: current verified_at_sha equals the prior signed one (deterministic,
        # reusing the signed snapshot ref). Both must be present and equal — a local-mode (None)
        # review on either side is not a reliable signal, so it is treated as changed.
        signed_sha = signing.verified_at_sha_from_manifest(manifest)
        current_sha = current_code_sha()
        reasons["code_unchanged"] = bool(signed_sha) and signed_sha == current_sha

        # registry UNCHANGED: the criteria-routing version equals the prior signed one
        # (overlay-aware — an activated/edited/disabled criterion is a registry change).
        reasons["registry_unchanged"] = manifest_regver(manifest) == registry_version(repo_root)

        # prior REVIEW_RESULT sidecar WITH finding text available (child e344). NOTE: this reads
        # the newest USABLE v1 payload (walk-back over malformed/foreign-schema files), whereas
        # the window below reads the newest FILE's timestamp; they can differ if the newest file
        # is unusable — benign here (both only gate eligibility, conservatively).
        # AUDIT (bug old-frilly-plankton): this is an EXISTENCE gate ("did a substantive prior
        # review run?"), NOT a novelty prior set — it never feeds findings into novelty scoring, so
        # it deliberately reads ALL findings (a review that floored everything still ran and is a
        # valid convergence anchor). Do NOT narrow this to ``surfaced_findings`` — that would change
        # eligibility semantics. The surfaced-only filter belongs only where prior findings become a
        # novelty SIGNAL (``_maybe_apply_rising_floor`` / ``prior_concerns``).
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
    except Exception:  # noqa: BLE001 — fail-safe: any read error → not eligible → full review, never crash
        logger.warning(
            "remediation-mode candidate check failed; treating as not eligible", exc_info=True
        )
        return {"eligible": False, "reasons": reasons, "baseline": "signature"}

    return {"eligible": all(reasons.values()), "reasons": reasons, "baseline": "signature"}


def _sidecar_branch_decision(
    ticket_id: str, *, window_minutes: int, now_ns: int | None, repo_root=None
) -> dict[str, Any]:
    """The SIDECAR-baseline eligibility branch (story a850), used only when no valid certified
    plan-review manifest exists (a BLOCK loop — a BLOCK never signs). Baselines come from the
    most recent ``REVIEW_RESULT`` payload (stamped since a850). The reasons dict has EXACTLY
    the five keys below — ``sidecar_baseline`` subsumes prior-sidecar existence, no ``signed``
    key — so ``eligible = all(reasons.values())`` cannot be structurally inert. Fail-safe:
    any read error → that precondition stays False → full review."""
    from . import sidecar

    reasons: dict[str, bool] = {
        "sidecar_baseline": False,
        "plan_changed": False,
        "code_unchanged": False,
        "registry_unchanged": False,
        "within_window": False,
    }
    try:
        prior = sidecar.latest_review_result(ticket_id, repo_root=repo_root)
        base_material = (prior or {}).get("material_fingerprint")
        base_sha = (prior or {}).get("verified_at_sha")
        base_regver = (prior or {}).get("regver")
        reasons["sidecar_baseline"] = bool(base_material and base_sha and base_regver)
        current_material = current_material_fingerprint(ticket_id, repo_root=repo_root)
        reasons["plan_changed"] = (
            base_material is not None
            and current_material is not None
            and current_material != base_material
        )
        # Both sides come from ONE rule (review_code_sha: snapshot SHA else git HEAD).
        reasons["code_unchanged"] = bool(base_sha) and base_sha == sidecar.review_code_sha(
            repo_root
        )
        reasons["registry_unchanged"] = base_regver is not None and base_regver == registry_version(
            repo_root
        )
        last_ts = sidecar.latest_review_timestamp(ticket_id, repo_root=repo_root)
        if last_ts is not None:
            current_ns = now_ns if now_ns is not None else time.time_ns()
            reasons["within_window"] = (
                0 <= (current_ns - last_ts) <= window_minutes * 60 * 1_000_000_000
            )
    except Exception:  # noqa: BLE001 — fail-safe: any read error → not eligible → full review, never crash
        logger.warning("remediation sidecar-branch check failed; not eligible", exc_info=True)
        return {"eligible": False, "reasons": reasons, "baseline": "sidecar"}
    return {"eligible": all(reasons.values()), "reasons": reasons, "baseline": "sidecar"}


def refresh_attestation(
    ticket_id: str,
    prior_manifest: list[str],
    *,
    probe: str,
    repo_root=None,
    relation_snapshot_value=None,
    initial_generation=None,
) -> dict[str, Any]:
    """Re-sign a drift-refreshed attestation: the PRIOR verdict (verdict/material/
    model/runner/counts) re-bound to the CURRENT hashes of the SAME dependency paths,
    with a ``refreshed-from`` provenance line + the current registry stamp. Reuses the
    prior signed paths (authoritative) rather than re-deriving the set."""
    from rebar import signing

    from . import registry, relation_snapshot

    snapshot = (
        initial_generation.relation_snapshot
        if initial_generation is not None
        else relation_snapshot_value
        or relation_snapshot.collect_plan_relation_snapshot(ticket_id, repo_root=repo_root)
    )

    fields: dict[str, Any] = {
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
    disabled = registry.disabled_builtins(repo_root)
    if disabled:
        fields["coverage"]["disabled_builtins"] = disabled
    prior_digest = signing.verify_signature(ticket_id, repo_root=repo_root).get("key_id", "?")
    new_deps = _rehash(manifest_deps(prior_manifest).keys(), repo_root=repo_root)
    manifest = build_manifest(
        fields,
        material=manifest_material(prior_manifest) or "",
        deps=new_deps,
        regver=registry_version(repo_root),
        refreshed_from=f"{prior_digest} probe={probe}",
        pins=snapshot.related_material,
        review_phase=manifest_review_phase(prior_manifest),
        priority_floor=manifest_priority_floor(prior_manifest),
    )
    if initial_generation is not None:
        from . import generation

        if manifest_material(prior_manifest) != initial_generation.own_material:
            raise generation.PlanReviewGenerationChanged(
                "review material changed before drift refresh signing"
            )
        return generation.sign_manifest(
            ticket_id, manifest, initial_generation, repo_root=repo_root
        )
    return signing.sign_manifest(ticket_id, manifest, kind=_MANIFEST_PREFIX, repo_root=repo_root)


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


# ── authoritative (signed) field sourcing for validity checks ─────────────────────
def _is_opcert(attestation: Mapping[str, Any]) -> bool:
    """True when the verify-result came from the op-cert (envelope) verifier. Keyed on the
    unspoofable ``opcert`` marker :func:`_opcert_signing.verify_opcert_record` sets (chosen on
    ``record.envelope`` presence), NOT the attacker-writable ``algorithm`` field."""
    return attestation.get("opcert") is True


def _authoritative_material(attestation: Mapping[str, Any]) -> str | None:
    """Read material from the signed op-cert payload or HMAC-covered legacy manifest."""
    if _is_opcert(attestation):
        return attestation.get("material_fingerprint") or None
    return manifest_material(attestation.get("manifest") or [])


def _authoritative_manifest(attestation: Mapping[str, Any]) -> list:
    """Read the signed DSSE manifest, with plaintext fallback for legacy op-certs/HMAC."""
    if _is_opcert(attestation):
        signed = attestation.get("signed_manifest")
        if isinstance(signed, list):
            return signed
    return attestation.get("manifest") or []


def _authoritative_head(attestation: Mapping[str, Any]) -> str | None:
    """The AUTHENTICATED code-anchor commit for unscoped whole-HEAD freshness.

    SECURITY (finding B): for an op-cert record use the SIGNED ``merged_log_commit`` (the code state
    bound into the cert's subject) rather than the plaintext ``head_sha`` mirror. For a local review
    ``merged_log_commit`` equals the head at signing time, so legit records are unaffected; a
    tampered plaintext ``head_sha`` can no longer make a stale attestation read as fresh. A legacy
    HMAC record keeps its ``head_sha`` mirror (behavior unchanged)."""
    if _is_opcert(attestation):
        return attestation.get("merged_log_commit")
    return attestation.get("head_sha")


# ── the fast claim-gate check (no LLM, no heavy reads) ────────────────────────────
def compute_validity(
    attestation: Mapping[str, Any] | None,
    ticket_state: dict[str, Any],
    kind: str,
    *,
    repo_root=None,
    profile: PlanValidityProfile = PlanValidityProfile.DEFAULT,
) -> dict[str, Any]:
    """Compute lifecycle/freshness validity without mutating the certified record.

    Plan-review profiles differ only on code freshness; completion ignores the profile.
    """
    from rebar import config as _config
    from rebar import signing

    if not isinstance(attestation, dict):
        return {"valid": False, "reason": f"no certified {kind} attestation", "verdict": "unsigned"}
    signed_at = attestation.get("signed_at")

    plan_health: Any = None
    auth_manifest = None
    if kind == _MANIFEST_PREFIX:
        if attestation.get("verified") is False:
            return {
                "valid": False,
                "reason": "no certified plan-review attestation",
                "verdict": "unsigned",
            }
        auth_manifest = _authoritative_manifest(attestation)
        if not is_plan_review_manifest(auth_manifest):
            return {
                "valid": False,
                "reason": "the certified attestation is not a plan review",
                "verdict": "wrong-kind",
            }
        enforced = _read_enforce_plan_material_pins(repo_root)
        try:
            pins = manifest_pins(auth_manifest)
            plan_health = derive_plan_material_pin_health(
                pins, repo_root=repo_root, enforced=enforced
            )
        except ManifestFormatError:
            plan_health = {"pin_status": "malformed-pin", "enforced": enforced, "targets": []}

        try:
            signed_phase = manifest_review_phase(auth_manifest)
            signed_floor = manifest_priority_floor(auth_manifest)
            current_phase = ticket_state.get("plan_review_phase")
            if current_phase is None:
                current_phase = (
                    "planning" if ticket_state.get("status") in ("open", "idea") else "execution"
                )
            from .pin_health import review_phase_status

            plan_health["phase_status"] = review_phase_status(
                current_phase, signed_phase, signed_floor
            )
        except ManifestFormatError:
            plan_health["phase_status"] = "malformed"

        if plan_health["pin_status"] == "malformed-pin" and enforced:
            return {
                "valid": False,
                "reason": "the plan-review attestation has malformed related-material pins",
                "verdict": "malformed-pin",
                "health": plan_health,
            }
        if plan_health["phase_status"] == "malformed":
            return {
                "valid": False,
                "reason": "malformed plan-review phase metadata",
                "verdict": "malformed-phase",
                "health": plan_health,
            }

    def _result(valid: bool, reason: str, verdict: str) -> dict[str, Any]:
        result = {"valid": valid, "reason": reason, "verdict": verdict}
        if plan_health is not None:
            result["health"] = plan_health
        return result

    # A signature at/before the latest reopen no longer describes the reactivated ticket.
    last_reopened = ticket_state.get("last_reopened_at")
    if last_reopened is not None and (signed_at is None or signed_at <= last_reopened):
        return _result(
            False,
            f"the {kind} attestation predates the latest reopen (stale)",
            "stale-reopened",
        )

    if kind == "completion-verifier":
        if ticket_state.get("status") != "closed":
            return {
                "valid": False,
                "reason": "the ticket is not closed (completion verdict no longer applies)",
                "verdict": "not-closed",
            }
        signed_material = _authoritative_material(attestation)
        if signed_material is not None:
            current = current_material_fingerprint(
                ticket_state.get("ticket_id", ""), repo_root=repo_root
            )
            if current is None or current != signed_material:
                return {
                    "valid": False,
                    "reason": "the ticket was materially edited since the completion verdict",
                    "verdict": "stale-material",
                }
        return {
            "valid": True,
            "reason": "certified completion-verifier attestation",
            "verdict": "certified",
        }

    if kind == _MANIFEST_PREFIX:  # plan-review
        assert auth_manifest is not None
        # Every freshness input comes from the authenticated manifest, never its plaintext mirror.
        signed_regver = manifest_regver(auth_manifest)
        if signed_regver is None or signed_regver != registry_version(repo_root):
            return _result(
                False,
                (
                    "the criteria registry changed since the plan review "
                    "(overlay activated/edited/disabled)"
                ),
                "stale-regver",
            )
        # DEFAULT re-hashes scoped dependencies; unscoped records use whole-HEAD freshness.
        if profile is PlanValidityProfile.DEFAULT:
            deps = manifest_deps(auth_manifest)
            if deps:
                pinned = signing.verified_at_sha_from_manifest(auth_manifest)
                base = _hash_basis(repo_root, pinned_sha=pinned)
                drifted = [
                    p for p, digest in sorted(deps.items()) if _hash_file(p, base=base) != digest
                ]
                if drifted:
                    shown = ", ".join(drifted[:5]) + (" …" if len(drifted) > 5 else "")
                    return _result(
                        False,
                        f"the code the plan was reviewed against drifted: "
                        f"{len(drifted)} dependency file(s) changed ({shown})",
                        "stale-code",
                    )
            else:
                head = signing.head_sha(_config.repo_root(repo_root))
                signed_head = _authoritative_head(attestation)
                if head == "unknown" or signed_head != head:
                    return _result(
                        False,
                        f"attestation is stale (unscoped; signed at {signed_head}, HEAD is {head})",
                        "stale-head",
                    )
        # Material-edit invalidation (fail closed if the fingerprint can't be recomputed).
        signed = _authoritative_material(attestation)
        if signed is not None:
            current = current_material_fingerprint(
                ticket_state.get("ticket_id", ""), repo_root=repo_root
            )
            if current is None:
                return _result(
                    False,
                    "could not recompute the plan's material fingerprint",
                    "unverifiable-material",
                )
            if signed != current:
                return _result(
                    False,
                    (
                        "the plan was materially edited since review "
                        "(description/AC/file_impact/children changed)"
                    ),
                    "stale-material",
                )
        assert plan_health is not None
        if plan_health["phase_status"] != "compatible":
            return _result(False, "plan-review phase is incompatible", "incompatible-phase")
        if plan_health["enforced"] and plan_health["pin_status"] not in (
            "current",
            "legacy-unpinned",
        ):
            pin_status = plan_health["pin_status"]
            return _result(False, "reviewed related-ticket material is stale", pin_status)
        return _result(True, "certified plan-review attestation", "certified")

    return {"valid": True, "reason": f"certified {kind} attestation", "verdict": "certified"}


# ── completion-awareness: is a container's child "delivered" right now? ───────────
def _attested_delivered(ticket: dict[str, Any], *, repo_root=None) -> bool:
    """Require closed status plus a completion attestation valid on this ticket's state."""
    import rebar

    if ticket.get("status") != "closed":
        return False
    tid = ticket.get("ticket_id")
    if not tid:
        return False
    try:
        sig = rebar.verify_signature(tid, kind="completion-verifier", repo_root=repo_root)
        if sig.get("verdict") != "certified":
            return False
        return bool(
            compute_validity(sig, ticket, "completion-verifier", repo_root=repo_root).get("valid")
        )
    except Exception:  # noqa: BLE001 — never let a signature read crash the predicate; fail closed
        logger.warning("delivered_now: attestation read failed for %s", tid, exc_info=True)
        return False


def _supersedes_child(candidate: dict[str, Any], child_id: str) -> bool:
    """True when ``candidate`` carries a ``candidate -supersedes-> child`` link. A ``supersedes``
    link is stored on the SOURCE ticket's ``deps`` as ``{"relation": "supersedes",
    "target_id": <child>}`` (``add_dependency`` writes to the source dir; ``supersedes`` is never
    hierarchy-promoted), so "A supersedes child" is A's dep whose ``target_id`` is the child."""
    for dep in candidate.get("deps") or []:
        if (
            isinstance(dep, dict)
            and dep.get("relation") == "supersedes"
            and dep.get("target_id") == child_id
        ):
            return True
    return False


def delivered_now(child: dict[str, Any], siblings: list[dict[str, Any]], *, repo_root=None) -> bool:
    """Return verified delivery, directly or through a live in-container superseder.

    Bare closed status never suffices; completion attestations are checked on read. The
    superseder branch is deliberately non-recursive and only considers supplied siblings.
    """
    if _attested_delivered(child, repo_root=repo_root):
        return True

    child_id = child.get("ticket_id")
    if not child_id:
        return False
    child_parent = child.get("parent_id")
    for a in siblings or []:
        if not isinstance(a, dict):
            continue
        a_id = a.get("ticket_id")
        if a_id is None or a_id == child_id:
            continue
        if a.get("parent_id") != child_parent:  # not an in-epic sibling
            continue
        if not _supersedes_child(a, child_id):
            continue
        # A is a LIVE in-epic vehicle: actively open/in_progress, OR closed-and-attested
        # (branch (A) on A — NON-recursive: A's own supersede chain is never followed).
        if a.get("status") in ("open", "in_progress"):
            return True
        if _attested_delivered(a, repo_root=repo_root):
            return True
    return False


def claim_gate_check(ticket_id: str, *, repo_root=None) -> dict[str, Any]:
    """The fast, local claim-path check for the PLAN-REVIEW gate. Returns
    ``{ok: bool, reason: str, verdict: str}``.

    ``ok`` is True only when a CERTIFIED plan-review attestation exists (verified strictly
    from the kind-keyed map) AND :func:`compute_validity` passes — its reviewed code has not
    drifted, it binds the current material fingerprint, and it post-dates any reopen. NO LLM
    and NO network — a pure local HMAC verify + a light fingerprint recompute + hashing a
    handful of dependency files."""
    from rebar import _reads, signing

    try:
        result = signing.verify_signature(ticket_id, kind=_MANIFEST_PREFIX, repo_root=repo_root)
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
    # We requested kind="plan-review" strictly, so a certified result IS a plan-review
    # attestation (no separate wrong-manifest check needed). Layer freshness/lifecycle.
    try:
        state = _reads.show_ticket(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — unreadable state → fail closed below via compute_validity's material/None paths
        state = {}
    validity = compute_validity(
        result,
        state,
        _MANIFEST_PREFIX,
        repo_root=repo_root,
        profile=PlanValidityProfile.DEFAULT,
    )
    if not validity["valid"]:
        return {
            "ok": False,
            "reason": validity["reason"],
            "verdict": validity.get("verdict", "stale"),
        }
    return {"ok": True, "reason": "certified plan-review attestation", "verdict": "certified"}


def current_material_fingerprint(ticket_id: str, *, repo_root=None) -> str | None:
    """Recompute the ticket's material fingerprint from a LIGHT read (the ticket +
    its child ids only — no full child fetch, no LLM), matching
    :func:`orchestrator.material_fingerprint`. Returns None on any read error
    (so a read failure never wrongly invalidates — the head_sha + certified checks
    still gate)."""
    from rebar import _reads

    from .det_floor import PlanContext
    from .orchestrator import material_fingerprint

    try:
        state = _reads.show_ticket(ticket_id, repo_root=repo_root)
        canonical = state.get("ticket_id", ticket_id)
        try:
            kids = _reads.list_tickets(parent=canonical, repo_root=repo_root) or []
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
