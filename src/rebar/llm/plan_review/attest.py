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

import logging
import time
from collections.abc import Mapping
from typing import Any

# Manifest construction + dependency-hashing live in the sibling ``manifest`` module
# (a pure, dependency-light seam). Re-exported here so the historical import paths
# ``rebar.llm.plan_review.attest.<name>`` keep working unchanged for callers/tests.
from .manifest import (
    _ABSENT_HASH,
    _DEP_PREFIX,
    _DISABLED_PREFIX,
    _MANIFEST_PREFIX,
    _REFRESHED_PREFIX,
    _REGVER_PREFIX,
    _cited_paths,
    _hash_basis,
    _hash_file,
    build_manifest,
    dependency_hashes,
    is_plan_review_manifest,
    manifest_deps,
    manifest_disabled_builtins,
    manifest_material,
    manifest_rebar_version,
    manifest_regver,
    registry_version,
)

logger = logging.getLogger(__name__)

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
    "manifest_rebar_version",
    "manifest_regver",
    "registry_version",
]


def sign_plan_review(verdict: dict[str, Any], *, material: str, repo_root=None) -> dict[str, Any]:
    """Sign a passing plan-review verdict (append a ``SIGNATURE`` event). Returns the
    signature record. Raises if signing fails (the caller decides how to surface it).

    Never-sign structural guard (story blackbear, epic jira-reb-687): a degraded / INDETERMINATE
    verdict — or any verdict carrying a systemic-degrade ``coverage.resolution_class`` — is by
    definition NOT a certifiable result and must never be attested. The caller only reaches here
    on a clean PASS, so this is defense-in-depth: it makes the "degraded ⇒ unsigned" invariant
    STRUCTURAL rather than incidental, failing closed if a future caller mistakenly signs one."""
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

    deps = dependency_hashes(verdict, repo_root=repo_root)
    # Record the overlay's disabled built-ins on the verdict coverage so build_manifest emits
    # the `disabled_builtins:` line (story 08af). Populated here (the sign path) so the stamp is
    # authoritative even when the verdict the orchestrator produced did not carry it.
    disabled = registry.disabled_builtins(repo_root)
    if disabled:
        verdict.setdefault("coverage", {})["disabled_builtins"] = disabled
    # Pin the snapshot SHA the deps were hashed at (attested review only — current_code_sha
    # is None in local mode), so the claim gate re-hashes the SAME basis (shared boundary).
    # regver is overlay-aware (repo_root) so the stamp reflects an activated/edited/disabled
    # criterion — a change the claim gate reads as stale-regver.
    manifest = build_manifest(
        verdict,
        material=material,
        deps=deps,
        regver=registry_version(repo_root),
        verified_at_sha=current_code_sha(),
    )
    sig = signing.sign_manifest(
        verdict["ticket_id"], manifest, kind=_MANIFEST_PREFIX, repo_root=repo_root
    )
    # Enrichment queue (epic only-crave-art / e1f4): a certification is the trigger to enqueue
    # this ticket for a store-wide overlap enrichment after a soak (a re-cert bumps the soak
    # deadline forward — latest-wins). Best-effort and fully isolated: a queue failure must
    # NEVER fail signing, and this stays a no-op when the [agents] extra is absent.
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
    # Registry skew → the probe's meaning may have changed → full review (overlay-aware).
    if manifest_regver(manifest) != registry_version(repo_root):
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
      material are read from). With NO usable plan-review signature (a BLOCK loop — a BLOCK
      never signs) the decision falls through to :func:`_sidecar_branch_decision`, whose
      baseline is the prior ``REVIEW_RESULT`` payload; the decision dict carries
      ``baseline: "signature" | "sidecar"``.
    - ``plan_changed`` — the current plan material fingerprint differs from the prior signed one
      (an edited plan — else there is nothing to re-review under the floor).
    - ``code_unchanged`` — the current run's ``verified_at_sha`` equals the prior signed one
      (the reviewed code did not drift; reuses the already-signed snapshot ref).
    - ``registry_unchanged`` — the criteria-routing registry version equals the prior signed one.
    - ``prior_sidecar`` — a prior ``REVIEW_RESULT`` sidecar WITH finding text exists (child e344).
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
    # NEVER raises (the docstring contract): the WHOLE body is guarded — a read error in ANY
    # precondition (signing, the manifest parsers, current_code_sha / registry_version, or the
    # sidecar reads) leaves that precondition False and yields eligible=False → a full review.
    # The gate is fail-safe: a broken signal can only DENY remediation mode, never crash the
    # plan review it gates.
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
    ticket_id: str, prior_manifest: list[str], *, probe: str, repo_root=None
) -> dict[str, Any]:
    """Re-sign a drift-refreshed attestation: the PRIOR verdict (verdict/material/
    model/runner/counts) re-bound to the CURRENT hashes of the SAME dependency paths,
    with a ``refreshed-from`` provenance line + the current registry stamp. Reuses the
    prior signed paths (authoritative) rather than re-deriving the set."""
    from rebar import signing

    from . import registry

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
    """The AUTHENTICATED material fingerprint to gate material-edit invalidation against.

    SECURITY (finding B): for an op-cert (envelope) record the material fingerprint is sourced from
    the SIGNED payload (surfaced by ``verify_opcert_record`` as ``material_fingerprint``) — NEVER
    the plaintext ``manifest``'s ``material:`` line, which is not covered by the DSSE signature and
    lives on the attacker-writable tickets branch. A legacy HMAC record's manifest IS covered by the
    HMAC signature, so ``manifest_material`` remains authentic there (behavior unchanged).

    An op-cert minted from a manifest with no ``material:`` line binds an EMPTY material fingerprint
    (``mint_opcert_record`` uses ``_manifest_material_fingerprint(steps) or ""``), which the signed
    payload surfaces here as ``""``. That is "no bound material", so we normalise it to ``None`` —
    the "no fingerprint → drift check skipped" contract (matching ``manifest_material``'s ``None``
    for a material-less manifest). A genuine bound fingerprint is a non-empty hash, so this only
    maps the empty sentinel through; a real post-signing material edit still fails the check as
    ``stale-material``."""
    if _is_opcert(attestation):
        return attestation.get("material_fingerprint") or None
    return manifest_material(attestation.get("manifest") or [])


def _authoritative_manifest(attestation: Mapping[str, Any]) -> list:
    """The AUTHENTICATED manifest to read plan-review freshness inputs from — the per-path
    dependency-hash map (``manifest_deps`` → the ``stale-code`` check), the criteria-registry
    version stamp (``manifest_regver`` → the ``stale-regver`` check), and the pinned-SHA re-hash
    basis (``verified_at_sha_from_manifest``).

    SECURITY (stale-code / stale-regver findings): for an op-cert (envelope) record the manifest is
    sourced from the SIGNED DSSE payload (surfaced by ``verify_opcert_record`` as
    ``signed_manifest`` from the in-toto predicate) — NEVER the record's plaintext ``manifest``
    mirror, which is not
    covered by the DSSE signature and lives on the auto-pushed, attacker-writable tickets branch.
    Mirrors ``_authoritative_material`` (which reads the signed ``material_fingerprint`` rather than
    the plaintext ``material:`` line). A legacy HMAC record's manifest IS covered by the HMAC
    signature, so its plaintext ``manifest`` remains authentic (behavior unchanged).

    A legacy op-cert minted BEFORE the manifest was bound into the payload has no
    ``signed_manifest``; there is nothing authenticated to read, so we fall back to the record's
    plaintext ``manifest`` — no worse than today's behavior for those already-deployed records, and
    new op-certs carry the signed manifest."""
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
) -> dict[str, Any]:
    """Per-kind lifecycle/freshness validity for an ALREADY-CERTIFIED attestation record.

    The caller runs the HMAC verify first (``verify_signature``); this layers the gate
    semantics on top and returns ``{"valid": bool, "reason": str}``. The attestation record is
    NEVER mutated — reopen invalidation is COMPUTED here from ``ticket_state['last_reopened_at']``
    (epic dark-acme-lumen), replacing the old write-time ``retire_attested_pin``. "Computed on
    read" still permits I/O: the plan-review branch re-hashes the signed dependency files.

    Per kind:
      * BOTH — an attestation whose ``signed_at`` is at/before the most recent ``closed→open``
        reopen no longer reflects the reactivated ticket (stale).
      * ``completion-verifier`` — additionally requires the ticket to be ``closed`` and the
        material fingerprint (recorded in the manifest) to match the current ticket.
      * ``plan-review`` — the existing claim-gate freshness: scoped code-drift (the signed
        per-path hashes still match, re-hashed at the signed pinned-SHA basis) or, when
        unscoped, whole-HEAD freshness; plus material-fingerprint invariance.
    """
    from rebar import config as _config
    from rebar import signing

    if not isinstance(attestation, dict):
        return {"valid": False, "reason": f"no certified {kind} attestation", "verdict": "unsigned"}
    signed_at = attestation.get("signed_at")

    # Reopen invalidation (BOTH kinds): an attestation signed at/before the latest reopen is
    # stale — the ticket was reactivated (and possibly changed) since it was signed. A missing
    # signed_at fails closed when a reopen is on record (we cannot prove it post-dates it).
    # CLOCK NOTE: signed_at is wall-clock (signing.sign_manifest → time.time_ns()) while
    # last_reopened_at is the reopen STATUS event's HLC tick. They are different clocks, but a
    # legitimate re-close/re-review happens only AFTER real agent work (seconds+) elapses since
    # the reopen, so signed_at comfortably exceeds the reopen tick; the HLC's "+1" floor bounds
    # any skew to the ns scale and self-corrects as wall-clock advances. The `<=` therefore
    # fails closed on the (practically unreachable) same-instant tie without false positives.
    last_reopened = ticket_state.get("last_reopened_at")
    if last_reopened is not None and (signed_at is None or signed_at <= last_reopened):
        return {
            "valid": False,
            "reason": f"the {kind} attestation predates the latest reopen (stale)",
            "verdict": "stale-reopened",
        }

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
        # SECURITY (stale-code / stale-regver findings): read every manifest-derived freshness
        # input (regver stamp, per-path dep hashes, pinned re-hash basis) from the AUTHENTICATED
        # manifest — the SIGNED DSSE payload for an op-cert, the HMAC-covered record manifest for a
        # legacy record — NEVER the attacker-writable plaintext record mirror. An attacker with
        # tickets-branch write access can no longer edit the plaintext dep-hash / regver lines to
        # make a stale attestation read as fresh (the signature does not cover the plaintext).
        auth_manifest = _authoritative_manifest(attestation)
        # Criteria-registry drift (story 08af): the overlay-aware stamp changes when a project
        # criterion is activated / re-tuned / disabled, so a signed regver that no longer matches
        # the current one means the criteria the plan was reviewed against changed. A MISSING
        # regver line is treated as stale too (expand-contract: every production plan-review
        # manifest carries one; an overlay-absent repo re-hashes to the SAME packaged stamp the
        # manifest was signed with, so a real unchanged attestation stays valid).
        signed_regver = manifest_regver(auth_manifest)
        if signed_regver is None or signed_regver != registry_version(repo_root):
            return {
                "valid": False,
                "reason": (
                    "the criteria registry changed since the plan review "
                    "(overlay activated/edited/disabled)"
                ),
                "verdict": "stale-regver",
            }
        # Code-drift freshness (ADR 0002): re-hash the SIGNED per-path map at the SAME
        # pinned-SHA basis the attestation signed against (so the gate and plan-review can't
        # diverge); when unscoped, fall back to conservative whole-HEAD freshness.
        deps = manifest_deps(auth_manifest)
        if deps:
            pinned = signing.verified_at_sha_from_manifest(auth_manifest)
            base = _hash_basis(repo_root, pinned_sha=pinned)
            drifted = [
                p for p, digest in sorted(deps.items()) if _hash_file(p, base=base) != digest
            ]
            if drifted:
                shown = ", ".join(drifted[:5]) + (" …" if len(drifted) > 5 else "")
                return {
                    "valid": False,
                    "reason": (
                        f"the code the plan was reviewed against drifted: "
                        f"{len(drifted)} dependency file(s) changed ({shown})"
                    ),
                    "verdict": "stale-code",
                }
        else:
            head = signing.head_sha(_config.repo_root(repo_root))
            # SECURITY (finding B): compare against the AUTHENTICATED anchor (op-cert: the SIGNED
            # merged_log_commit; HMAC: the head_sha mirror), never a mutable plaintext mirror.
            signed_head = _authoritative_head(attestation)
            if head == "unknown" or signed_head != head:
                return {
                    "valid": False,
                    "reason": (
                        f"attestation is stale (unscoped; signed at {signed_head}, HEAD is {head})"
                    ),
                    "verdict": "stale-head",
                }
        # Material-edit invalidation (fail closed if the fingerprint can't be recomputed).
        signed = _authoritative_material(attestation)
        if signed is not None:
            current = current_material_fingerprint(
                ticket_state.get("ticket_id", ""), repo_root=repo_root
            )
            if current is None:
                return {
                    "valid": False,
                    "reason": "could not recompute the plan's material fingerprint",
                    "verdict": "unverifiable-material",
                }
            if signed != current:
                return {
                    "valid": False,
                    "reason": (
                        "the plan was materially edited since review "
                        "(description/AC/file_impact/children changed)"
                    ),
                    "verdict": "stale-material",
                }
        return {
            "valid": True,
            "reason": "certified plan-review attestation",
            "verdict": "certified",
        }

    return {"valid": True, "reason": f"certified {kind} attestation", "verdict": "certified"}


# ── completion-awareness: is a container's child "delivered" right now? ───────────
def _attested_delivered(ticket: dict[str, Any], *, repo_root=None) -> bool:
    """Branch (A) of :func:`delivered_now` for a SINGLE ticket: it is ``closed`` AND holds a
    ``completion-verifier`` attestation that is VALID ON READ.

    Reuses the EXACT per-child validity read that
    :func:`rebar.llm.completion.child_closure_findings` performs — get the ticket's
    ``completion-verifier`` signature via :func:`rebar.verify_signature` and, when it is
    ``certified``, run :func:`compute_validity` (kind ``"completion-verifier"``) against the
    ticket's OWN state. A force-closed / unsigned / drift-stale (compute_validity ``valid=False``)
    / not-closed ticket fails. Fail-closed: any read error → not delivered."""
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
    """Is a container's CHILD ticket ``child`` DELIVERED right now, for plan-review
    completion-awareness? Keys on VERIFIED delivery, NEVER bare ``closed`` status.

    Returns ``True`` IFF either:

    (A) DELIVERED-AND-ATTESTED — ``child`` is ``closed`` AND holds a ``completion-verifier``
        attestation that is valid on read (see :func:`_attested_delivered`, which reuses the
        SAME ``verify_signature`` + :func:`compute_validity` read as
        ``completion.child_closure_findings``). A force-closed / unsigned / drift-stale /
        reopened-after-signing child fails.

    (B) SUPERSEDED-BY-LIVE-IN-EPIC-SIBLING — there is a ticket ``A`` in ``siblings`` that
        SUPERSEDES ``child`` (an ``A -supersedes-> child`` link), shares ``child``'s
        ``parent_id`` (an in-epic sibling), and is a LIVE vehicle: ``A`` is ``open`` /
        ``in_progress``, OR ``A`` is itself delivered-and-attested (branch (A) applied to ``A``).
        The supersede branch does NOT recurse (only ``A``'s own status/attestation is consulted),
        so a superseded-by-non-sibling / superseded-by-force-closed(dead)-sibling ``child`` is
        NOT delivered here.

    PURE / recomputed each call — no persisted state, no caching. Reopen semantics fall out of
    :func:`compute_validity` keying on each ticket's OWN ``last_reopened_at``: a PARENT reopen
    does NOT un-deliver a child (the child's state is unchanged), only a CHILD's own reopen does.

    ``siblings`` is supplied by the caller — the container's children, e.g.
    ``rebar.list_tickets(parent=<container>, repo_root=…)`` — mirroring how
    ``completion.child_closure_findings`` enumerates a parent's children."""
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
    validity = compute_validity(result, state, _MANIFEST_PREFIX, repo_root=repo_root)
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
