"""Plan-review manifest construction + dependency-hashing (extracted from ``attest``).

This module holds the PURE manifest/hashing seam of the plan-review attestation: the
criteria-registry version stamp, the code-drift dependency-hashing helpers, the
deterministic manifest builder, and the manifest field-parsers. It is deliberately
dependency-light (only ``rebar.*`` + the sibling ``registry`` module) and must NEVER
import :mod:`rebar.llm.plan_review.attest` — ``attest`` imports (and re-exports) from
here, so the dependency edge points one way (attest → manifest) with no cycle.

Every public name here is re-exported from ``attest`` so existing import paths
(``rebar.llm.plan_review.attest.build_manifest`` etc.) keep working unchanged.

The manifest is UNVERSIONED and ADDITIVE-ONLY (new line kinds are appended and emitted only
when non-empty; readers ignore unknown lines), which is what keeps the signed byte-image stable
for live attestations — the per-field "additive / byte-identical / older verifier ignores"
notes below are that contract in situ; its fuller durable home is ADR 0064.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

if TYPE_CHECKING:
    from .relation_snapshot import PlanMaterialPin

logger = logging.getLogger(__name__)

_MANIFEST_PREFIX = "plan-review"
_DEP_PREFIX = "dep"
_REGVER_PREFIX = "regver:"  # criteria-registry version stamp (progressive drift-refresh, ADR 0002)
_REFRESHED_PREFIX = "refreshed-from:"  # provenance on a drift-refreshed attestation
_DISABLED_PREFIX = "disabled_builtins:"  # built-in ids the project overlay disabled (story 08af)
_ABSENT_HASH = "absent"  # sentinel for a dependency path that does not exist on disk
_PIN_PREFIX = "plan-material-pin:"
# Per-component material fingerprints (bug 94a3): ``material-part: <name> <hash16> <size>``.
# Purely DIAGNOSTIC — nothing decides on them; they exist so a staleness message can name
# WHICH basis key moved instead of reciting every possibility. Additive, so a manifest
# without them parses to ``{}`` and an older verifier ignores them. The prefix deliberately
# does not collide with :func:`manifest_material`'s ``material:`` match.
_MATERIAL_PART_PREFIX = "material-part:"
_PIN_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")
_REVIEW_PHASE_PREFIX = "review-phase:"
_PRIORITY_FLOOR_PREFIX = "priority-floor:"
_FILE_SCOPE_NONE = "file-scope: none"
# The agentic passes' READ-SET (ticket 81ca), inside the SIGNED material so a tampered or
# truncated read-set fails signature verification and resolves to the fail-safe. ``read-set: <n>``
# is the presence marker — emitted only when telemetry was actually collected, so a manifest with
# an EMPTY-but-recorded read-set (``read-set: 0``) is distinguishable from one that recorded none
# at all (no line), which is what keeps the pre-change fallback intact for legacy attestations.
_READ_SET_PREFIX = "read-set:"
_READ_PATH_PREFIX = "read-path:"
# Which basis the signed dependency set was composed on: ``file_impact`` (declared/inherited
# paths), ``read-set`` (the no-file-impact scoping) or ``fail-safe`` (empty set → whole-HEAD).
# Diagnostic and additive; ``rebar review-plan --status`` reports it.
_CURRENCY_BASIS_PREFIX = "currency-basis:"
CURRENCY_BASIS_FILE_IMPACT = "file_impact"
CURRENCY_BASIS_READ_SET = "read-set"
CURRENCY_BASIS_FAIL_SAFE = "fail-safe"


class ManifestFormatError(ValueError):
    """A signed plan-review manifest contains a malformed material-pin line."""


class ReviewPhaseMetadata(TypedDict):
    phase: Literal["planning", "execution"]
    priority_floor: float | None


def validate_review_phase_metadata(
    phase: object, floor: object, *, legacy_absent: bool
) -> ReviewPhaseMetadata:
    """Validate the shared manifest/sidecar phase grammar and policy."""
    if legacy_absent:
        if phase is not None or floor is not None:
            raise ManifestFormatError("legacy phase metadata must be wholly absent")
        return {"phase": "planning", "priority_floor": None}
    if phase not in ("planning", "execution"):
        raise ManifestFormatError(f"unknown review phase: {phase!r}")
    if floor is None:
        if phase == "execution":
            raise ManifestFormatError("execution review is missing priority floor")
        return {"phase": "planning", "priority_floor": None}
    if phase != "execution" or isinstance(floor, bool) or not isinstance(floor, (int, float)):
        raise ManifestFormatError("priority floor is invalid for the review phase")
    parsed_floor = float(floor)
    if not math.isfinite(parsed_floor) or not 0.0 <= parsed_floor <= 1.0:
        raise ManifestFormatError(f"priority floor outside [0.0, 1.0]: {floor!r}")
    return {"phase": "execution", "priority_floor": parsed_floor}


def _manifest_review_phase_metadata(manifest: list[str] | None) -> ReviewPhaseMetadata:
    phase_tokens: list[str] = []
    floor_tokens: list[str] = []
    for raw in manifest or []:
        text = str(raw)
        if text.startswith("review-phase") or text.strip().startswith("review-phase"):
            parts = text.split(" ")
            if len(parts) != 2 or parts[0] != _REVIEW_PHASE_PREFIX or not parts[1]:
                raise ManifestFormatError(f"malformed review phase: {text!r}")
            phase_tokens.append(parts[1])
        elif text.startswith("priority-floor") or text.strip().startswith("priority-floor"):
            parts = text.split(" ")
            if len(parts) != 2 or parts[0] != _PRIORITY_FLOOR_PREFIX or not parts[1]:
                raise ManifestFormatError(f"malformed priority floor: {text!r}")
            floor_tokens.append(parts[1])
    if len(phase_tokens) > 1 or len(floor_tokens) > 1:
        raise ManifestFormatError("duplicate review phase metadata")
    absent = not phase_tokens and not floor_tokens
    raw_floor: object = None
    if floor_tokens:
        try:
            raw_floor = float(floor_tokens[0])
        except ValueError:
            raise ManifestFormatError(f"invalid priority floor: {floor_tokens[0]!r}") from None
    return validate_review_phase_metadata(
        phase_tokens[0] if phase_tokens else None,
        raw_floor,
        legacy_absent=absent,
    )


def manifest_review_phase(manifest: list[str] | None) -> Literal["planning", "execution"]:
    return _manifest_review_phase_metadata(manifest)["phase"]


def manifest_priority_floor(manifest: list[str] | None) -> float | None:
    return _manifest_review_phase_metadata(manifest)["priority_floor"]


def registry_version(repo_root=None) -> str:
    """A short, deterministic stamp of the criteria registry the review ran against
    (the canonical DET + LLM id sets + the routing index). Bound into the manifest so
    a progressive drift-refresh can detect that the registry changed since signing
    (version skew) and fall back to a FULL re-review instead of reusing the verdict.

    OVERLAY-AWARE (story 08af): with ``repo_root`` given, the stamp hashes the repo's
    EFFECTIVE routing (packaged ⊕ the ``.rebar/criteria_routing.json`` overlay) plus the
    overlay's activated-project ids and disabled-built-in set — so activating / re-tuning /
    disabling a project criterion changes the stamp. Since ADR 0053 a rotated stamp is
    GRANDFATHERED at the claim gate: it is reported as non-blocking ``registry_drift`` rather
    than invalidating a prior plan-review attestation. With ``repo_root=None``,
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


@dataclass(frozen=True)
class GateRefBasis:
    """What the claim-gate freshness re-check resolved as its hashing basis, and whether it
    had to DEGRADE to get there.

    ``degraded`` is True in exactly one case: an ATTESTED gate whose configured ref (or whose
    snapshot) could not be obtained, so the in-place working tree was SUBSTITUTED for the
    committed snapshot the signed hashes were produced against. That substitution is not a
    basis — the working tree may coincidentally still hold the reviewed bytes while the gate
    ref has moved on, which reads as "no drift" for a genuinely stale attestation (fail-OPEN;
    bug 505d-b2c5-734f-47d9). A configured ``source=local`` gate is NOT degraded: there
    the checkout is the correct, documented basis, not a substitution.

    ``ref`` names the gate ref that could not be obtained (``None`` when the failure happened
    before the ref itself was resolvable) so a caller can say WHAT it could not read rather
    than falsely reporting that the dependency files drifted."""

    path: str
    ref: str | None = None
    degraded: bool = False


def gate_ref_hash_basis(repo_root=None) -> GateRefBasis:
    """Resolve the CURRENT-gate-ref hashing basis — the ONE shared ref-resolution boundary
    (ADR 0002) — reporting whether it degraded to the working tree.

    :func:`_hash_basis` delegates here for its ``at_current_gate_ref`` mode and keeps its
    lenient, never-raising contract (it returns only ``.path``); callers that must fail CLOSED
    on a substituted basis read ``.degraded``. The resolution logic lives here and ONLY here —
    it is not duplicated into the claim gate."""
    from rebar import config as _config

    working = str(_config.repo_root(repo_root))
    ref: str | None = None
    try:
        from rebar._snapshot import cache as _cache
        from rebar._snapshot.repo_snapshot import resolve_ref
        from rebar.llm import gate_source

        if gate_source.default_source(working) != gate_source.SOURCE_ATTESTED:
            return GateRefBasis(working)
        ref = gate_source.default_ref(working)
        sha = resolve_ref(ref, working, fetch=False)
        handle = _cache.acquire(sha, source_mode="attested", repo_root=working, fetch=False)
        return GateRefBasis(str(handle.path))
    except Exception:
        logger.warning(
            "current gate-ref snapshot unavailable; hashing the working tree", exc_info=True
        )
        return GateRefBasis(working, ref=ref, degraded=True)


def _hash_basis(repo_root=None, *, at_current_gate_ref: bool = False) -> str:
    """The ONE shared ref-resolution boundary (epic raze-vet-ditch S4b, amended by bug
    72d9 ``athletic-esthetical-polecat``) that BOTH the plan-review signing-time hashing
    AND the claim-gate freshness re-check resolve through. The S4b guarantee is that the
    two sides resolve with the SAME semantics (a committed snapshot under the same gate
    configuration) — NOT that they resolve to the identical tree: re-hashing at the
    signature's own pinned ``verified_at_sha`` compared the manifest against the immutable
    tree it was generated from, which always matched, making scoped drift structurally
    undetectable in attested mode (bug 72d9).

    Resolution (single source):
      * ``at_current_gate_ref`` (the claim-gate freshness re-check) → the materialized
        snapshot at the CURRENT gate ref (``REBAR_GATE_REF`` / ``[snapshot].ref`` /
        ``origin/main``), resolved from the LOCAL object DB (no network — drift visibility
        is as fresh as the last fetch). Signed hashes were produced at the review's pinned
        SHA, so a signed dependency file that changed between that SHA and the current ref
        registers as drift, while an unrelated commit still does not (the per-path scoping
        ADR 0002 exists for). A configured ``source=local`` gate — or a ref/snapshot that
        cannot be resolved — degrades to the in-place checkout (the pre-S4b behavior; the
        conservative direction, since the checkout normally contains the drift). This
        function stays LENIENT — it never raises and always yields a path. Whether the basis
        was substituted is reported separately by :func:`gate_ref_hash_basis`, which the
        claim-gate re-check reads so it can fail CLOSED on a degraded attested basis (bug
        505d-b2c5-734f-47d9) instead of certifying against the working tree.
      * else the active attested gate snapshot (``current_code_root``, set during an attested
        ``review_plan``) → the tree the signature is being produced against.
      * else the in-place checkout (``_config.repo_root``) — the local / back-out basis.

    Legacy tolerance: an attestation with no ``verified_at_sha`` (pre-S4b) still verifies —
    its signed hashes are simply compared against the same current-gate-ref basis."""
    from rebar import config as _config

    if at_current_gate_ref:
        return gate_ref_hash_basis(repo_root).path
    from rebar.llm.gate_context import current_code_root

    active = current_code_root()
    return active if active else str(_config.repo_root(repo_root))


@dataclass(frozen=True)
class _ChildImpact:
    """Direct-child impact available to a container's signed dependency set."""

    paths: frozenset[str]
    all_none: bool
    #: A live child declared NO impact, so inheritance was suppressed (the poison rule).
    #: Ticket 81ca reads this to keep a poisoned container at the whole-HEAD fail-safe: the
    #: rule exists precisely because a partial scope is fail-OPEN for the undeclared part,
    #: and read-set scoping would be exactly such a partial scope.
    poisoned: bool = False


def _normalized_child_impact(
    child: object,
) -> tuple[Literal["paths", "none", "undeclared"], frozenset[str]]:
    """Normalize one child state without trusting inconsistent scope metadata."""
    if not isinstance(child, dict):
        return "undeclared", frozenset()
    impact = child.get("file_impact")
    valid_paths = isinstance(impact, list) and all(
        isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and bool(entry["path"].strip())
        for entry in impact
    )
    paths = frozenset(entry["path"] for entry in impact) if valid_paths and impact else frozenset()
    if "file_impact_scope" not in child:
        return ("paths", paths) if paths else ("undeclared", frozenset())
    scope = child.get("file_impact_scope")
    if scope == "paths" and paths:
        return "paths", paths
    if scope == "none" and impact == []:
        return "none", frozenset()
    return "undeclared", frozenset()


def _inherited_child_impact(children: Sequence[dict[str, Any]] | None) -> _ChildImpact:
    """The container drift-scope inheritance (ticket 3e4b ``saddened-unadult-snowmonkey``):
    the union of DIRECT children's declared ``file_impact`` paths plus whether every
    live child explicitly declares no impact.

    Pure — operates only on the caller's already-fetched child states (the orchestrator's
    ``ctx.children``; no store reads, no root ambiguity). Rules:

    * no children / ``None`` (a leaf, or a caller with no assembled context) → no paths and
      ``all_none=False`` — the dep set stays the pre-change own ∪ citations;
    * live ``paths`` children contribute their paths; live ``none`` children are neutral;
    * POISON RULE: a live ``undeclared`` child clears inherited paths, since a partial union
      would flip the empty-set whole-HEAD fallback from fail-closed to fail-open;
    * CLOSED children neither contribute nor poison: their work is delivered (ADR 0024's
      completion floor stops re-litigating it) and their files' later churn belongs to
      other tickets.

    One-level-deep by design: container reviews pin each direct child's material
    fingerprint (which covers the child's ``file_impact``), so a child impact edit already
    invalidates the container attestation and forces a re-review that recomputes this
    union — the self-healing does NOT extend to grandchildren, so neither does the union."""
    out: set[str] = set()
    live_count = 0
    all_none = True
    for child in children or []:
        if isinstance(child, dict) and child.get("status") == "closed":
            continue
        live_count += 1
        scope, child_paths = _normalized_child_impact(child)
        if scope == "none":
            continue
        if scope == "undeclared":
            return _ChildImpact(frozenset(), False, poisoned=True)
        all_none = False
        out.update(child_paths)
    return _ChildImpact(frozenset(out), live_count > 0 and all_none)


def _declares_no_impact(ticket_id: str, impact: _ChildImpact, *, repo_root=None) -> bool:
    """True when this ticket's signed scope would classify as ``none`` (ticket 81ca).

    Mirrors :func:`classify_file_scope`'s ``none`` arm on an EMPTY dependency set: an own scope
    of ``none``, or an ``undeclared`` container whose live children all declare none. Such an
    attestation is exempt from the code-drift check today, and read-set scoping must not take
    that exemption away. Unreadable scope answers False — the conservative direction here, since
    False only means the read-set MIGHT scope a ticket that turns out to be ``undeclared``."""
    try:
        import rebar

        own_scope = rebar.get_file_impact_scope(ticket_id, repo_root=repo_root).get(
            "kind", "undeclared"
        )
    except Exception:  # noqa: BLE001 — an unreadable scope must not raise inside the gate
        logger.warning("file_impact scope read failed for %s; treating as undeclared", ticket_id)
        return False
    return own_scope == "none" or (own_scope == "undeclared" and impact.all_none)


def dependency_hashes(
    verdict: dict[str, Any],
    *,
    repo_root=None,
    children: Sequence[dict[str, Any]] | None = None,
    child_impact: _ChildImpact | None = None,
) -> dict[str, str]:
    """The signed dependency set: ``{path: sha256}`` for the union of the ticket's
    declared ``file_impact``, the files the review CITED (``kind=file``), and the paths from
    a container's declared direct children (:func:`_inherited_child_impact`; ticket 3e4b).
    Sorted for reproducible signing.

    Ticket 81ca: when the ticket declares NO ``file_impact`` and the review recorded a
    read-set (``coverage['read_set_recorded']``), the set is additionally scoped to the
    normalized read-set ∪ the blast-radius entries (:mod:`read_set`) instead of collapsing to
    empty. Every other case is byte-identical to before. Empty when nothing is
    declared/cited/inherited/read — the claim gate then falls back to whole-HEAD freshness
    (any commit invalidates), the fail-closed direction."""
    import rebar

    from . import read_set as _read_set

    ticket_id = verdict.get("ticket_id", "")
    impact = child_impact or _inherited_child_impact(children)
    paths: set[str] = set(_cited_paths(verdict))
    paths.update(impact.paths)
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
    raw_coverage = verdict.get("coverage")
    coverage: dict[str, Any] = raw_coverage if isinstance(raw_coverage, dict) else {}
    basis = CURRENCY_BASIS_FILE_IMPACT
    # Scope ONLY the case ADR 0002 leaves unscoped: a set that would otherwise be EMPTY, on a
    # container that did not poison, for a ticket whose scope is UNDECLARED. All three guards
    # are load-bearing:
    #
    #   * adding the blast radius to an already-scoped set would re-introduce the very false
    #     positive this ticket removes (an unrelated commit touching e.g. rebar.toml would
    #     invalidate a container correctly scoped to its child's files);
    #   * a poisoned container is deliberately fail-CLOSED at whole-HEAD, because any partial
    #     scope is fail-OPEN for the child's undeclared impact;
    #   * an AUTHENTICATED no-impact scope (``file-scope: none``) is today fully EXEMPT from
    #     code drift — ``compute_validity`` skips the head check for it entirely. Scoping it
    #     would hand it a non-empty dep set, which :func:`classify_file_scope` reclassifies
    #     ``none`` → ``paths``, so a previously exempt attestation would START invalidating on
    #     read-set/blast-radius drift. That is strictly MORE invalidation — the opposite of
    #     this change's purpose — so a declared ``none`` keeps its exemption untouched.
    if (
        not paths
        and not impact.poisoned
        and not _declares_no_impact(ticket_id, impact, repo_root=repo_root)
        and coverage.get("read_set_recorded")
    ):
        try:
            paths.update(
                _read_set.read_set_dependency_paths(coverage.get("read_set") or [], base=base)
            )
            basis = CURRENCY_BASIS_READ_SET
        except Exception:  # noqa: BLE001 — a failed expansion must never scope; fail-safe
            logger.warning(
                "read-set scoping failed for %s; falling back to whole-HEAD freshness", ticket_id
            )
    deps = {p: _read_set.hash_dep_entry(p, base=base) for p in sorted(paths)}
    if isinstance(raw_coverage, dict):
        # Stash the basis the set was ACTUALLY composed on so the signer records the truth
        # rather than re-deriving it from a second (possibly divergent) file_impact read.
        coverage["currency_basis"] = basis if deps else CURRENCY_BASIS_FAIL_SAFE
    return deps


def classify_file_scope(
    dependency_paths: Iterable[object], own_scope: object, *, container_all_none: bool = False
) -> Literal["paths", "none", "unscoped"]:
    """Classify signed code freshness without weakening legacy empty scopes."""
    if any(dependency_paths):
        return "paths"
    if own_scope == "none" or (own_scope == "undeclared" and container_all_none):
        return "none"
    return "unscoped"


# ── manifest ─────────────────────────────────────────────────────────────────────
def build_manifest(
    verdict: dict[str, Any],
    *,
    material: str,
    deps: dict[str, str] | None = None,
    regver: str | None = None,
    refreshed_from: str | None = None,
    verified_at_sha: str | None = None,
    pins: Sequence[PlanMaterialPin] = (),
    material_parts: Mapping[str, tuple[str, int]] | None = None,
    review_phase: object = "planning",
    priority_floor: object = None,
    file_scope: object = "unscoped",
    read_set: Sequence[str] | None = None,
    currency_basis: str | None = None,
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
    phase_metadata = validate_review_phase_metadata(
        review_phase, priority_floor, legacy_absent=False
    )
    lines = [
        f"{_MANIFEST_PREFIX}: {verdict.get('verdict', 'PASS')}",
        f"ticket: {verdict.get('ticket_id', '')}",
        f"material: {material}",
        *(
            f"{_MATERIAL_PART_PREFIX} {name} {digest} {size}"
            for name, (digest, size) in sorted((material_parts or {}).items())
        ),
        f"model: {verdict.get('model') or 'n/a'}",
        f"runner: {verdict.get('runner') or 'n/a'}",
        f"blocking: {counts.get('blocking', 0)}",
        f"advisory: {counts.get('advisory_surfaced', 0)}",
        f"{_REVIEW_PHASE_PREFIX} {phase_metadata['phase']}",
    ]
    if phase_metadata["priority_floor"] is not None:
        lines.append(f"{_PRIORITY_FLOOR_PREFIX} {phase_metadata['priority_floor']:.2f}")
    lines.append(_signing.rebar_version_step(_signing.gate_code_version()))
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
    for pin in sorted(pins, key=lambda item: (item.role, item.canonical_id)):
        lines.append(f"{_PIN_PREFIX} {pin.role} {pin.canonical_id} {pin.material_fingerprint}")
    if pins:
        # Never mint a manifest the strict reader would later reject (including
        # duplicate role+id records supplied by a non-snapshot caller).
        manifest_pins(lines)
    # Per-path dependency hashes (sorted), one line each: ``dep <sha256> <path>``.
    # The hash is fixed-width so the path (which may contain spaces) is an unambiguous
    # remainder. A per-path map (not a rolled-up root) is the contract Story 2 builds on.
    for path, digest in sorted((deps or {}).items()):
        lines.append(f"{_DEP_PREFIX} {digest} {path}")
    if file_scope == "none":
        lines.append(_FILE_SCOPE_NONE)
    # The signed read-set (ticket 81ca), additive and emitted only when the review actually
    # recorded one: the ``read-set: <n>`` marker distinguishes a verified-EMPTY read-set from
    # a manifest that recorded none at all (which keeps the pre-change fail-safe).
    if read_set is not None:
        paths = sorted({str(p) for p in read_set if str(p)})
        lines.append(f"{_READ_SET_PREFIX} {len(paths)}")
        lines.extend(f"{_READ_PATH_PREFIX} {p}" for p in paths)
    if currency_basis:
        lines.append(f"{_CURRENCY_BASIS_PREFIX} {currency_basis}")
    return lines


def manifest_pins(manifest: list[str] | None) -> list[PlanMaterialPin]:
    """Parse and strictly validate additive plan-material-pin manifest records."""

    from .relation_snapshot import PlanMaterialPin, PlanMaterialRole, is_canonical_ticket_id

    pins: list[PlanMaterialPin] = []
    seen: set[tuple[str, str]] = set()
    for line in manifest or []:
        text = str(line)
        if not text.startswith(_PIN_PREFIX):
            if text.strip().startswith("plan-material-pin"):
                raise ManifestFormatError(f"malformed plan material pin: {text!r}")
            continue
        parts = text.split()
        if len(parts) != 4 or parts[0] != _PIN_PREFIX:
            raise ManifestFormatError(f"malformed plan material pin: {text!r}")
        _, role, canonical_id, fingerprint = parts
        if role not in ("child", "prerequisite"):
            raise ManifestFormatError(f"unknown plan material pin role: {role!r}")
        if not is_canonical_ticket_id(canonical_id):
            raise ManifestFormatError(f"invalid plan material pin ticket id: {canonical_id!r}")
        if not _PIN_FINGERPRINT_RE.fullmatch(fingerprint):
            raise ManifestFormatError(f"invalid plan material pin fingerprint: {fingerprint!r}")
        key = (role, canonical_id)
        if key in seen:
            raise ManifestFormatError(f"duplicate plan material pin: {role} {canonical_id}")
        seen.add(key)
        pins.append(PlanMaterialPin(cast(PlanMaterialRole, role), canonical_id, fingerprint))
    return sorted(pins, key=lambda item: (item.role, item.canonical_id))


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


def manifest_read_set(manifest: list[str] | None) -> list[str] | None:
    """The signed read-set, or ``None`` when the manifest recorded none at all.

    ``None`` and ``[]`` are DIFFERENT and the difference is load-bearing: ``None`` means no
    agentic pass ran, telemetry collection failed, or the attestation predates ticket 81ca —
    in every such case the no-file-impact scoping was not applied and the whole-HEAD fail-safe
    still governs. ``[]`` means the review verifiably read nothing."""
    if not any(str(line).startswith(_READ_SET_PREFIX) for line in manifest or []):
        return None
    return sorted(
        str(line).split(":", 1)[1].strip()
        for line in manifest or []
        if str(line).startswith(_READ_PATH_PREFIX) and str(line).split(":", 1)[1].strip()
    )


def manifest_currency_basis(manifest: list[str] | None) -> str:
    """Which basis the signed dependency set was composed on. A manifest predating ticket
    81ca carries no record, so it is DERIVED conservatively: dependency lines mean the set
    came from declared/cited paths (``file_impact``), none means the whole-HEAD fail-safe."""
    for line in manifest or []:
        if str(line).startswith(_CURRENCY_BASIS_PREFIX):
            recorded = str(line).split(":", 1)[1].strip()
            if recorded:
                return recorded
    return CURRENCY_BASIS_FILE_IMPACT if manifest_deps(manifest) else CURRENCY_BASIS_FAIL_SAFE


def manifest_file_scope(manifest: list[str] | None) -> Literal["none", "unscoped"]:
    """Read the exact authenticated no-file-impact declaration, if present."""
    declarations = [str(line) for line in manifest or [] if str(line).startswith("file-scope:")]
    return "none" if declarations == [_FILE_SCOPE_NONE] else "unscoped"


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


def manifest_material_parts(manifest: list[str] | None) -> dict[str, tuple[str, int]]:
    """Parse the per-component material fingerprints back out (``{}`` when absent).

    Deliberately LENIENT where :func:`manifest_pins` is strict: these lines are diagnostic
    only, so a malformed one is skipped rather than raised. Raising would let a cosmetic
    field convert a ``stale-material`` refusal into a ``malformed-pin`` one and change a gate
    outcome — exactly what this whole change must not do.
    """
    out: dict[str, tuple[str, int]] = {}
    for line in manifest or []:
        text = str(line)
        if not text.startswith(_MATERIAL_PART_PREFIX):
            continue
        parts = text.split()
        if len(parts) != 4:
            continue
        _, name, digest, size = parts
        try:
            out[name] = (digest, int(size))
        except ValueError:
            continue
    return out


def manifest_material(manifest: list[str] | None) -> str | None:
    """Extract the bound material fingerprint from a signed manifest, if present."""
    for line in manifest or []:
        if str(line).startswith("material:"):
            return str(line).split(":", 1)[1].strip()
    return None
