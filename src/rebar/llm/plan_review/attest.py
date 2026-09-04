"""Plan-review signing and fast local claim-gate validity checks."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast

# Re-export manifest helpers so historical ``attest.<name>`` imports remain stable.
from .manifest import (
    _ABSENT_HASH,
    _DEP_PREFIX,
    _DISABLED_PREFIX,
    _MANIFEST_PREFIX,
    _REFRESHED_PREFIX,
    _REGVER_PREFIX,
    CURRENCY_BASIS_FAIL_SAFE,
    CURRENCY_BASIS_FILE_IMPACT,
    ManifestFormatError,
    _cited_paths,
    _hash_basis,
    _hash_file,
    _inherited_child_impact,
    build_manifest,
    classify_file_scope,
    dependency_hashes,
    gate_ref_hash_basis,
    is_plan_review_manifest,
    manifest_currency_basis,
    manifest_deps,
    manifest_disabled_builtins,
    manifest_file_scope,
    manifest_material,
    manifest_pins,
    manifest_priority_floor,
    manifest_read_set,
    manifest_rebar_version,
    manifest_regver,
    manifest_review_phase,
    registry_version,
    validate_review_phase_metadata,
)
from .pin_health import DerivedPlanMaterialPinHealth, DerivedPlanReviewHealth, PlanValidityProfile
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
    from .relation_snapshot import material_child_index

    # bug 3d57: one lazily-built child-index snapshot shared across every pin
    # fingerprint (and the in-frame legacy recomputes) instead of a full-store
    # scan per pin per fingerprint generation.
    with material_child_index(repo_root=repo_root):
        return derive_health(
            pin_records,
            repo_root=repo_root,
            enforced=enforced,
            fingerprint=current_material_fingerprint,
            compatible_fingerprint=_legacy_material_ok,
        )


__all__ = [
    "_ABSENT_HASH",
    "_DEP_PREFIX",
    "_DISABLED_PREFIX",
    "_MANIFEST_PREFIX",
    "_REFRESHED_PREFIX",
    "_REGVER_PREFIX",
    "ManifestFormatError",
    # re-exported from attest_gate (kept importable as attest.<name>; see the foot of this file)
    "_attested_delivered",
    "_cited_paths",
    "_hash_basis",
    "_hash_file",
    "_supersedes_child",
    "build_manifest",
    "claim_gate_check",
    "classify_file_scope",
    "delivered_now",
    "dependency_hashes",
    "gate_ref_hash_basis",
    "is_plan_review_manifest",
    "manifest_currency_basis",
    "manifest_deps",
    "manifest_disabled_builtins",
    "manifest_file_scope",
    "manifest_material",
    "manifest_pins",
    "manifest_priority_floor",
    "manifest_read_set",
    "manifest_rebar_version",
    "manifest_regver",
    "manifest_review_phase",
    "plan_review_status",
    "registry_version",
    "validate_review_phase_metadata",
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
    children: Sequence[dict[str, Any]] | None = None,
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
    from rebar.llm.gate_context import current_code_sha

    if not current_code_sha():
        raise SigningError(
            "refusing to sign a plan-review attestation with no attested snapshot "
            "(verified_at_sha would be None): only an attested, committed basis is "
            "certifiable — a local-source review never signs (ADR 0005)"
        )

    from . import registry
    from . import relation_snapshot as relation_snapshot_module

    snapshot = (
        initial_generation.relation_snapshot
        if initial_generation is not None
        else relation_snapshot
        or relation_snapshot_module.collect_plan_relation_snapshot(
            verdict["ticket_id"], repo_root=repo_root, ignore_untracked=True
        )
    )

    child_impact = _inherited_child_impact(children)
    deps = dependency_hashes(verdict, repo_root=repo_root, child_impact=child_impact)
    try:
        import rebar

        own_scope = rebar.get_file_impact_scope(verdict["ticket_id"], repo_root=repo_root).get(
            "kind", "undeclared"
        )
    except Exception:  # noqa: BLE001 — unreadable scope keeps conservative whole-HEAD freshness
        own_scope = "undeclared"
    disabled = registry.disabled_builtins(repo_root)
    if disabled:
        verdict.setdefault("coverage", {})["disabled_builtins"] = disabled
    from .material_diff import reviewed_material_parts

    # Ticket 81ca: the agentic passes' read-set rides INSIDE the signed material (so tampering
    # fails verification and resolves to the fail-safe), alongside the basis the dependency set
    # was actually composed on. ``read_set=None`` — no agentic pass ran, or telemetry collection
    # failed — records no marker and leaves the pre-change whole-HEAD fallback in force.
    _raw_coverage = verdict.get("coverage")
    _coverage: dict[str, Any] = _raw_coverage if isinstance(_raw_coverage, dict) else {}
    signed_read_set = (
        list(_coverage.get("read_set") or []) if _coverage.get("read_set_recorded") else None
    )
    currency_basis = _coverage.get("currency_basis") or (
        CURRENCY_BASIS_FILE_IMPACT if deps else CURRENCY_BASIS_FAIL_SAFE
    )

    manifest = build_manifest(
        verdict,
        read_set=signed_read_set,
        currency_basis=currency_basis,
        material=material,
        # Per-component fingerprints of the SAME basis (bug 94a3) — additive, deterministic,
        # and omitted entirely when they cannot be proven to reproduce ``material``, so the
        # manifest never carries an attribution the gate did not actually decide on.
        material_parts=reviewed_material_parts(snapshot, material),
        deps=deps,
        regver=registry_version(repo_root),
        verified_at_sha=current_code_sha(),
        pins=snapshot.related_material,
        review_phase=review_phase,
        priority_floor=priority_floor,
        file_scope=classify_file_scope(
            deps.keys(), own_scope, container_all_none=child_impact.all_none
        ),
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
    try:
        from rebar import config as _root_config
        from rebar.llm.config import resolve_gate_config
        from rebar.llm.overlap import queue as _enqueue_queue

        # Gate the PRODUCER on the same config pair the drain consumer reads
        # (enrich_drain.maybe_drain), so certification never appends ENQUEUE_ENRICH
        # into a queue nothing consumes (bug 4eae-c207-7d7b-41f3).
        cfg = resolve_gate_config(repo_root)
        feature_on = _root_config.compose_config(repo_root).verify.suggest_duplicate_tickets
        if feature_on and cfg.overlap_drain != "off":
            _enqueue_queue.enqueue(
                verdict["ticket_id"], soak_min=cfg.overlap_soak_min, repo_root=repo_root
            )
    except Exception:
        logging.getLogger(__name__).warning(
            "enrichment enqueue on certification failed; continuing", exc_info=True
        )
    return sig


def _rehash(paths, *, repo_root=None) -> dict[str, str]:
    """Re-hash the given dependency entries through the shared :func:`_hash_basis` boundary
    (the active snapshot during a gate run, else the working tree) and the shared
    :func:`read_set.hash_dep_entry` dispatcher — so a glob entry's membership digest is
    computed exactly one way on both the signing and the re-check side (ticket 81ca)."""
    from .read_set import hash_dep_entry

    base = _hash_basis(repo_root)
    return {p: hash_dep_entry(p, base=base) for p in sorted(paths)}


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
    # Registry drift is grandfathered at the CLAIM gate (ADR 0053) but must still deny the
    # progressive-refresh REUSE path: a probe carries the prior verdict forward, and that verdict
    # was reached under the older criteria. Denying here only costs a full re-review, never a
    # blocked claim — so this dimension stays conservative, matching ``registry_unchanged`` in
    # ``remediation_mode_candidate`` / ``drift_floor``. Before ADR 0053 the ``stale-regver``
    # verdict enforced this implicitly; it is now explicit.
    if validity.get("registry_drift") is not None:
        return None
    manifest = _authoritative_manifest(result)
    deps = manifest_deps(manifest)
    if not deps:  # unscoped attestation — nothing to probe against; full review
        return None
    current = _rehash(deps.keys(), repo_root=repo_root)
    if current == deps:  # no drift → not a drift re-review at all
        return None
    return {"manifest": manifest, "deps": deps, "key_id": result.get("key_id")}


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

    from . import registry
    from .relation_snapshot import collect_plan_relation_snapshot

    snapshot = (
        initial_generation.relation_snapshot
        if initial_generation is not None
        else relation_snapshot_value
        or collect_plan_relation_snapshot(ticket_id, repo_root=repo_root, ignore_untracked=True)
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
    # Same no-null-pin invariant as sign_plan_review (bug 5128-0856): a refresh re-signs
    # against CURRENT hashes, so refuse outside an attested session (no unpinned mints).
    from rebar.llm.gate_context import current_code_sha as _current_code_sha

    refreshed_at_sha = _current_code_sha()
    if not refreshed_at_sha:
        raise signing.SigningError(
            "refusing to drift-refresh a plan-review attestation with no attested snapshot "
            "(verified_at_sha would be None) — run `rebar review-plan` from an attested gate"
        )
    manifest = build_manifest(
        fields,
        material=manifest_material(prior_manifest) or "",
        deps=new_deps,
        regver=registry_version(repo_root),
        refreshed_from=f"{prior_digest} probe={probe}",
        verified_at_sha=refreshed_at_sha,
        pins=snapshot.related_material,
        review_phase=manifest_review_phase(prior_manifest),
        priority_floor=manifest_priority_floor(prior_manifest),
        # Carry the prior read-set/basis forward verbatim (ticket 81ca): a refresh re-binds the
        # SAME paths to current hashes, so silently dropping the scoping record would demote a
        # read-set-scoped attestation to the whole-HEAD fail-safe on its first refresh.
        read_set=manifest_read_set(prior_manifest),
        currency_basis=manifest_currency_basis(prior_manifest),
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
def _scoped_code_drift(deps: Mapping[str, str], auth_manifest: list[str], repo_root) -> str | None:
    """The scoped (``dep``-line) half of the plan-review code-drift check: ``None`` when the
    signed dependency hashes still match, else the ``stale-code`` reason to fail on.

    Re-hashes at the CURRENT gate ref, NOT the signature's own pinned SHA — that tautology
    made scoped drift undetectable in attested mode (72d9).

    FAILS CLOSED when the attested basis could not be obtained (bug 505d-b2c5-734f-47d9).
    The signed hashes
    of an attestation carrying a ``verified_at_sha`` were produced against a COMMITTED
    snapshot; hashing the in-place working tree instead is a SUBSTITUTION, and when that tree
    happens to still hold the reviewed bytes the comparison reports "no drift" for an
    attestation the moved gate ref has already invalidated — a fail-OPEN claim gate, which
    the claim gate is documented never to be (docs/plan-review-gate.md; ADR 0002). An
    attestation with no ``verified_at_sha`` (pre-S4b / never signed against an attested
    snapshot) keeps the lenient working-tree basis it was always compared against. No new
    verdict literal is introduced: this stays ``stale-code``, with a reason naming the ref
    rather than falsely claiming the dependency files changed."""
    from rebar import signing as _signing

    from .read_set import hash_dep_entry

    basis = gate_ref_hash_basis(repo_root)
    if basis.degraded and _signing.verified_at_sha_from_manifest(auth_manifest):
        named = f"'{basis.ref}'" if basis.ref else "the configured gate ref"
        return (
            "cannot confirm the code the plan was reviewed against is current: the gate ref "
            f"{named} could not be resolved to a snapshot, so the attestation cannot be "
            "re-checked (refusing to certify against the working tree)"
        )
    drifted = [
        path
        for path, digest in sorted(deps.items())
        if hash_dep_entry(path, base=basis.path) != digest
    ]
    if not drifted:
        return None
    shown = ", ".join(drifted[:5]) + (" …" if len(drifted) > 5 else "")
    return (
        f"the code the plan was reviewed against drifted: "
        f"{len(drifted)} dependency file(s) changed ({shown})"
    )


def _unscoped_head_drift(
    attestation: Mapping[str, Any], auth_manifest: list[str], repo_root
) -> str | None:
    """The unscoped (whole-HEAD, no per-file ``deps``) half of the plan-review freshness
    check: ``None`` when fresh, else the ``stale-head`` reason. The CURRENT-head anchor comes
    from the shared ``gate_source.current_head_sha`` (an ATTESTED attestation resolves the
    gate-ref sha from the LOCAL object DB, NO fetch, not the working-tree HEAD — a stranger sha
    in a feature worktree/foreign enclosing repo reading as spuriously stale, bug 1137;
    ``source=local``/LEGACY keep the working-tree comparison). An unresolvable attested gate ref
    fails CLOSED here (never certifies against the working tree)."""
    from rebar._snapshot.repo_snapshot import SnapshotError
    from rebar.llm import gate_source

    signed_head = _authoritative_head(attestation)
    try:
        head = gate_source.current_head_sha(auth_manifest, repo_root)
    except SnapshotError:
        from rebar import config as _config

        ref = gate_source.default_ref(str(_config.repo_root(repo_root)))
        named = f"'{ref}'" if ref else "the configured gate ref"
        return (
            "cannot confirm the code the plan was reviewed against is current: the gate "
            f"ref {named} could not be resolved to a snapshot, so the attestation cannot "
            "be re-checked (refusing to certify against the working tree)"
        )
    if head == "unknown" or signed_head != head:
        return f"attestation is stale (unscoped; signed at {signed_head}, HEAD is {head})"
    return None


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

    if not isinstance(attestation, dict):
        return {"valid": False, "reason": f"no certified {kind} attestation", "verdict": "unsigned"}
    signed_at = attestation.get("signed_at")

    plan_health: DerivedPlanReviewHealth | None = None
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
            plan_health = cast(
                DerivedPlanReviewHealth,
                dict(derive_plan_material_pin_health(pins, repo_root=repo_root, enforced=enforced)),
            )
        except ManifestFormatError:
            plan_health = {
                "pin_status": "malformed-pin",
                "enforced": enforced,
                "targets": [],
                "phase_status": "malformed",
                "signed_phase": None,
                "required_phase": None,
                "effective_execution_floor": None,
                "advisory": False,
                "enforcement_status": "enabled" if enforced else "disabled",
                "related_material_status": "pinned",
            }

        signed_phase: object = None
        signed_floor: object = None
        has_phase_metadata = any(str(line).startswith("review-phase: ") for line in auth_manifest)
        # Compatibility compares the ticket's CURRENT compiled phase against the phase the
        # signed review actually ran under, via the fixed `review_phase_status` table (tickets
        # 5967 + 2bbd; docs/plan-review-gate.md §"phase_status compatibility"). At CLOSE this is
        # identical to DEFAULT/DRIFT_REFRESH: a planning (or legacy-planning) attestation is
        # compatible with an execution-phase ticket. The close gate's job is to catch a plan
        # that CHANGED during execution — enforced via own-material + pin drift, which still
        # invalidate here — NOT to compel a fresh execution-phase review when nothing changed.
        current_phase: object = ticket_state.get("plan_review_phase")
        if current_phase is None:
            current_phase = (
                "planning" if ticket_state.get("status") in ("open", "idea") else "execution"
            )
        try:
            signed_phase = manifest_review_phase(auth_manifest)
            signed_floor = manifest_priority_floor(auth_manifest)
            from .pin_health import review_phase_status

            phase_status = review_phase_status(current_phase, signed_phase, signed_floor)
            plan_health["phase_status"] = cast(Any, phase_status)
        except ManifestFormatError:
            plan_health["phase_status"] = "malformed"

        assert plan_health is not None
        # One additive projection for every detailed reader.  Legacy manifests have
        # no phase token; a current no-relationship review has valid phase metadata
        # but no target rows, so operators can distinguish the two safely.
        if plan_health["pin_status"] == "legacy-unpinned" and has_phase_metadata:
            plan_health["pin_status"] = "current-no-relationships"
        plan_health["signed_phase"] = (
            signed_phase if signed_phase in ("planning", "execution") else None
        )
        plan_health["required_phase"] = (
            current_phase if current_phase in ("planning", "execution") else None
        )
        plan_health["effective_execution_floor"] = (
            float(signed_floor)
            if isinstance(signed_floor, (int, float)) and not isinstance(signed_floor, bool)
            else None
        )
        plan_health["advisory"] = bool(
            not enforced
            and plan_health["pin_status"]
            not in ("current", "current-no-relationships", "legacy-unpinned")
        )
        plan_health["enforcement_status"] = "enabled" if enforced else "disabled"
        plan_health["related_material_status"] = (
            "no-related-material"
            if plan_health["pin_status"] == "current-no-relationships"
            else "legacy-unpinned"
            if plan_health["pin_status"] == "legacy-unpinned"
            else "pinned"
        )

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

    # Criteria-registry drift, when detected below, is GRANDFATHERED (ADR 0053): it is
    # reported on every result this call returns but never flips ``valid``.
    registry_drift: dict[str, str | None] | None = None

    def _result(valid: bool, reason: str, verdict: str) -> dict[str, Any]:
        result = {"valid": valid, "reason": reason, "verdict": verdict}
        if plan_health is not None:
            result["health"] = plan_health
        if registry_drift is not None:
            result["registry_drift"] = registry_drift
        return result

    # A signature at/before the latest reopen no longer describes the reactivated ticket.
    last_reopened = ticket_state.get("last_reopened_at")
    if last_reopened is not None and (signed_at is None or signed_at <= last_reopened):
        return _result(
            False,
            f"the {kind} attestation predates the latest reopen "
            f"(signed at {signed_at}, reopened at {last_reopened}) — reopening resets the "
            "attestation, so nothing in the plan need have changed",
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
            if (current is None or current != signed_material) and not _legacy_material_ok(
                signed_material, ticket_state.get("ticket_id", ""), repo_root
            ):
                from .material_diff import explain_material_change

                return {
                    "valid": False,
                    "reason": (
                        "the ticket was materially edited since the completion verdict — "
                        + explain_material_change(
                            attestation,
                            ticket_state.get("ticket_id", ""),
                            repo_root=repo_root,
                        )
                    ),
                    "verdict": "stale-material",
                }
        return {
            "valid": True,
            "reason": "certified completion-verifier attestation",
            "verdict": "certified",
        }

    if kind == _MANIFEST_PREFIX:  # plan-review
        assert auth_manifest is not None
        # Registry drift is authenticated, reported, and grandfathered (ADR 0053).
        signed_regver = manifest_regver(auth_manifest)
        current_regver = registry_version(repo_root)
        if signed_regver is None or signed_regver != current_regver:
            registry_drift = {"signed": signed_regver, "current": current_regver}
        # DEFAULT re-hashes paths; only an authenticated none scope skips head drift.
        if profile is PlanValidityProfile.DEFAULT:
            deps = manifest_deps(auth_manifest)
            if deps:
                scoped = _scoped_code_drift(deps, auth_manifest, repo_root)
                if scoped is not None:
                    return _result(False, scoped, "stale-code")
            elif manifest_file_scope(auth_manifest) != "none":
                unscoped = _unscoped_head_drift(attestation, auth_manifest, repo_root)
                if unscoped is not None:
                    return _result(False, unscoped, "stale-head")
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
            if signed != current and not _legacy_material_ok(
                signed, ticket_state.get("ticket_id", ""), repo_root
            ):
                from .material_diff import explain_material_change

                return _result(
                    False,
                    "the plan was materially edited since review — "
                    + explain_material_change(
                        attestation,
                        ticket_state.get("ticket_id", ""),
                        repo_root=repo_root,
                    ),
                    "stale-material",
                )
        assert plan_health is not None
        if plan_health["phase_status"] != "compatible":
            return _result(False, "plan-review phase is incompatible", "incompatible-phase")
        if plan_health["enforced"] and plan_health["pin_status"] not in (
            "current",
            "current-no-relationships",
            "legacy-unpinned",
        ):
            pin_status = plan_health["pin_status"]
            return _result(False, "reviewed related-ticket material is stale", pin_status)
        return _result(True, "certified plan-review attestation", "certified")

    return {"valid": True, "reason": f"certified {kind} attestation", "verdict": "certified"}


# ── completion-awareness: is a container's child "delivered" right now? ───────────
def current_material_fingerprint(ticket_id: str, *, repo_root=None) -> str | None:
    """Recompute the ticket's material fingerprint from a LIGHT read (no LLM). Thin
    delegator (body moved next to :func:`live_material_children`); kept here because the
    close gate and the test suite reference/patch ``attest.current_material_fingerprint``."""
    from .relation_snapshot import current_material_fingerprint_impl

    return current_material_fingerprint_impl(ticket_id, repo_root=repo_root)


def _legacy_material_ok(signed: str, ticket_id: str, repo_root) -> bool:
    """Grandfather for pre-normalization attestations (bug 96d1): manifests signed before
    checkbox-state normalization hashed the RAW description (every completion manifest
    embeds ``[x]`` — the close gate requires checked boxes), so the normalized recompute
    can never match them. A signed hash that byte-exactly matches a LEGACY recomputation
    of the CURRENT material proves nothing changed — not even box state — so it is not a
    material edit. Strictly narrower than the normalized check: a real edit matches none.

    All superseded generations are tried: pre-330c (raw description), the intermediate
    post-330c/pre-2be7 one (checkbox-normalized, whitespace raw), and post-2be7/pre-reason-
    normalization (canonical description). Each used the raw no-file-impact reason. Without
    the second, a pre-330c attestation signed over UNTICKED boxes would stop surviving a tick
    once whitespace canonicalization landed, silently regressing bug 94a3 observation 1."""
    try:
        from .relation_snapshot import current_material_fingerprint_impl

        candidates = [
            current_material_fingerprint_impl(
                ticket_id,
                repo_root=repo_root,
                normalize_checkboxes=False,
                normalize_reason=False,
            ),
            current_material_fingerprint_impl(
                ticket_id,
                repo_root=repo_root,
                normalize_whitespace=False,
                normalize_reason=False,
            ),
            current_material_fingerprint_impl(
                ticket_id,
                repo_root=repo_root,
                normalize_reason=False,
            ),
        ]
    except Exception:  # noqa: BLE001 — best-effort fallback; an uncomputable legacy hash is simply no match
        return False
    return any(c is not None and c == signed for c in candidates)


# Backward-compatible claim-gate/delivery re-exports (after their helper dependencies).
from .attest_gate import (  # noqa: E402
    _attested_delivered,
    _supersedes_child,
    claim_gate_check,
    delivered_now,
    plan_review_status,
)

# The remediation-mode eligibility cluster moved to its own module (task c6c9); this file was at
# 794/800 LOC. Re-exported so every caller keeps reaching it as
# ``attest.remediation_mode_candidate`` — ``plan_review/__init__.py`` plus the
# ``monkeypatch.setattr`` seam in the remediation-gate tests. Deliberately NOT added to
# ``__all__``: it was never in it, so ``import *`` behavior is unchanged.
from .remediation_mode import remediation_mode_candidate  # noqa: E402, F401
