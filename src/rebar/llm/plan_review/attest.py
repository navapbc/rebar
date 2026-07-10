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
from collections.abc import Mapping
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


def sign_plan_review(verdict: dict[str, Any], *, material: str, repo_root=None) -> dict[str, Any]:
    """Sign a passing plan-review verdict (append a ``SIGNATURE`` event). Returns the
    signature record. Raises if signing fails (the caller decides how to surface it)."""
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
    # NEVER raises (the docstring contract): the WHOLE body is guarded — a read error in ANY
    # precondition (signing, the manifest parsers, current_code_sha / registry_version, or the
    # sidecar reads) leaves that precondition False and yields eligible=False → a full review.
    # The gate is fail-safe: a broken signal can only DENY remediation mode, never crash the
    # plan review it gates (which runs only when verify.remediation_mode is on).
    try:
        result = signing.verify_signature(ticket_id, repo_root=repo_root)
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
        return {"eligible": False, "reasons": reasons}

    return {"eligible": all(reasons.values()), "reasons": reasons}


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
    manifest = attestation.get("manifest") or []
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
        signed_material = manifest_material(manifest)
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
        # Criteria-registry drift (story 08af): the overlay-aware stamp changes when a project
        # criterion is activated / re-tuned / disabled, so a signed regver that no longer matches
        # the current one means the criteria the plan was reviewed against changed. A MISSING
        # regver line is treated as stale too (expand-contract: every production plan-review
        # manifest carries one; an overlay-absent repo re-hashes to the SAME packaged stamp the
        # manifest was signed with, so a real unchanged attestation stays valid).
        signed_regver = manifest_regver(manifest)
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
        deps = manifest_deps(manifest)
        if deps:
            pinned = signing.verified_at_sha_from_manifest(manifest)
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
            if head == "unknown" or attestation.get("head_sha") != head:
                return {
                    "valid": False,
                    "reason": (
                        f"attestation is stale (unscoped; signed at {attestation.get('head_sha')}, "
                        f"HEAD is {head})"
                    ),
                    "verdict": "stale-head",
                }
        # Material-edit invalidation (fail closed if the fingerprint can't be recomputed).
        signed = manifest_material(manifest)
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
