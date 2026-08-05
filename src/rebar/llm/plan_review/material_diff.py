"""Component-level plan-material fingerprinting, and the explainer that names WHAT changed.

The plan-review / completion attestations bind a SINGLE composite hash of the ticket's
material plan content (:func:`pass1.material_fingerprint`). A composite is one-way, so when
it stops matching, the gate knew only *that* something changed — every message therefore
recited a fixed list, ``description/AC/file_impact/children``, which named an input that does
not exist ("AC" is not a basis key; acceptance criteria live inside ``description``) and left
the reader to guess. Three agents reached three different conclusions about whether ticking a
checkbox invalidates an attestation because of it (bug 94a3).

This module closes that gap by hashing each basis key SEPARATELY. The per-component hashes
ride along in the signed manifest as additive ``material-part:`` lines, so a later read can
diff them component-wise and name exactly what moved. The composite itself is untouched — it
is still the only thing any gate decides on, and this module never changes an outcome, only
the sentence explaining it.

Attestations signed before those lines existed degrade gracefully: the explainer falls back
to the signed ``plan-material-pin:`` child ids (already in every pinned manifest) to report a
children diff, and otherwise says plainly that the component cannot be named.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .det_floor import PlanContext

logger = logging.getLogger(__name__)

# Basis keys in the order a human wants to read them. ``file_impact_scope`` is conditional
# (present only for an explicit no-file-impact declaration), so it trails the fixed four.
COMPONENT_ORDER = ("ticket_id", "description", "file_impact", "children", "file_impact_scope")

# What the size number COUNTS, per component — rendered so "13 -> 16" is unambiguous.
_UNITS = {
    "description": "chars",
    "file_impact": "paths",
    "children": "children",
    "ticket_id": "chars",
    "file_impact_scope": "fields",
}

_CHECKBOX_STATE_RE = re.compile(r"^(\s*[-*]\s*\[)[xX ](\])", re.MULTILINE)

#: The advice every material-staleness reason carries, because it is the single most
#: frequently mis-believed fact about the gate (bug 94a3, observations 1 and 3).
CHECKBOX_NOTE = (
    "ticking an AC checkbox is attestation-SAFE (box state is normalized out of the "
    "fingerprint), so a tick is never the cause"
)


def normalize_checkbox_state(description: str) -> str:
    """Erase checkbox STATE (``[x]``/``[X]`` -> ``[ ]``) for fingerprinting. Box state
    is progress metadata — the close precheck (433c) requires flipping it, so it must
    not stale a signed review (bug 330c). Item TEXT stays material."""
    return _CHECKBOX_STATE_RE.sub(r"\g<1> \g<2>", description)


def material_basis(ctx: PlanContext, *, normalize_checkboxes: bool = True) -> dict[str, Any]:
    """The ordered mapping the composite fingerprint hashes.

    Single-sourced here so :func:`pass1.material_fingerprint` and :func:`material_components`
    can never disagree about what "material" means — a divergence would let the explainer
    name a component the gate did not actually decide on.
    """
    basis: dict[str, Any] = {
        "ticket_id": ctx.ticket_id,
        "description": normalize_checkbox_state(ctx.description)
        if normalize_checkboxes
        else ctx.description,
        "file_impact": ctx.state.get("file_impact") or [],
        "children": sorted(c.get("ticket_id", "") for c in ctx.children),
    }
    if ctx.state.get("file_impact_scope") == "none":
        reason = ctx.state.get("no_file_impact_reason")
        basis["file_impact_scope"] = {
            "kind": "none",
            "reason": reason if isinstance(reason, str) else "",
        }
    return basis


def _hash_component(name: str, value: Any) -> str:
    """Hash ONE basis entry. The key is inside the hashed blob so two components holding
    equal values (an empty list and an empty list) still get distinct digests."""
    blob = json.dumps({name: value}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _size_of(value: Any) -> int:
    """A compact, non-disclosing magnitude: characters for text, entries otherwise. It
    never carries content, so recording it in the manifest leaks nothing."""
    try:
        return len(value)
    except TypeError:
        return 0


def material_components(
    ctx: PlanContext, *, normalize_checkboxes: bool = True
) -> dict[str, tuple[str, int]]:
    """``{component_name: (hash16, size)}`` for every key of the material basis."""
    basis = material_basis(ctx, normalize_checkboxes=normalize_checkboxes)
    return {
        name: (_hash_component(name, basis[name]), _size_of(basis[name]))
        for name in COMPONENT_ORDER
        if name in basis
    }


def context_from_snapshot(snapshot: Any) -> PlanContext | None:
    """Rebuild the subject :class:`PlanContext` a review actually saw, from its relation
    snapshot. Lets the signer record components for exactly the reviewed state, and lets a
    mid-review abort say which component moved."""
    from .det_floor import PlanContext

    state = getattr(snapshot, "subject_state", None)
    if not isinstance(state, dict):
        return None
    return PlanContext(
        ticket_id=state.get("ticket_id", ""),
        ticket_type=state.get("ticket_type", ""),
        title=state.get("title", ""),
        description=state.get("description", ""),
        state=state,
        children=[{"ticket_id": c} for c in getattr(snapshot, "child_ids", ()) or ()],
    )


def describe_delta(
    signed: dict[str, tuple[str, int]], current: dict[str, tuple[str, int]]
) -> str | None:
    """``"changed: description (410 -> 512 chars)"``, or ``None`` when nothing differs."""
    names = [n for n in COMPONENT_ORDER if n in signed or n in current]
    changed = [n for n in names if signed.get(n) != current.get(n)]
    if not changed:
        return None
    return "changed: " + ", ".join(_render(n, signed.get(n), current.get(n)) for n in changed)


def explain_snapshot_change(snapshot: Any, ticket_id: str, *, repo_root=None) -> str:
    """Name the component that moved between a review's own snapshot and the live ticket."""
    reviewed = context_from_snapshot(snapshot)
    current = _current_components(ticket_id, repo_root)
    if reviewed is None or current is None:
        return "the changed component could not be determined"
    return describe_delta(material_components(reviewed), current) or (
        "no material component differs (the material fingerprint moved for another reason)"
    )


def reviewed_material_parts(snapshot: Any, expected_composite: str | None):
    """Components for the state a review saw, recorded on its manifest and sidecar.

    ``None`` unless they provably reproduce ``expected_composite`` — see
    :func:`verified_material_components` for why a guess is worse than a blank."""
    if not expected_composite:
        return None
    ctx = context_from_snapshot(snapshot)
    return None if ctx is None else verified_material_components(ctx, expected_composite)


def verified_material_components(
    ctx: PlanContext, expected_composite: str
) -> dict[str, tuple[str, int]] | None:
    """Components for ``ctx``, but ONLY when they provably describe ``expected_composite``.

    The signer derives the composite and the components from separately-obtained state, so a
    mismatch is possible in principle. Returning ``None`` rather than a best guess means the
    manifest either carries components that are certainly the reviewed ones, or carries none —
    the explainer can then say "cannot be named" instead of naming the wrong thing.
    """
    from .pass1 import material_fingerprint

    try:
        if material_fingerprint(ctx) != expected_composite:
            return None
        return material_components(ctx)
    except Exception:  # noqa: BLE001 — a diagnostic aid must never break signing
        logger.warning("could not derive material components", exc_info=True)
        return None


def _render(name: str, signed: tuple[str, int] | None, current: tuple[str, int] | None) -> str:
    unit = _UNITS.get(name, "entries")
    if signed is None:
        return f"{name} (added, now {current[1] if current else 0} {unit})"
    if current is None:
        return f"{name} (removed, was {signed[1]} {unit})"
    if signed[1] == current[1]:
        return f"{name} ({signed[1]} {unit}, contents edited)"
    return f"{name} ({signed[1]} -> {current[1]} {unit})"


def _children_from_pins(manifest: list[str] | None) -> set[str] | None:
    """The child id set the review signed, recovered from ``plan-material-pin:`` lines.

    This is the retroactive lever: those lines predate component hashing and are present on
    essentially every pinned attestation, so a children add/remove can still be named on an
    attestation signed before this module existed.
    """
    from .manifest import ManifestFormatError, manifest_pins

    try:
        pins = manifest_pins(manifest)
    except ManifestFormatError:
        return None
    children = {pin.canonical_id for pin in pins if pin.role == "child"}
    return children if pins else None


def _current_components(ticket_id: str, repo_root) -> dict[str, tuple[str, int]] | None:
    from .relation_snapshot import current_plan_context

    try:
        ctx = current_plan_context(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — explaining a failure must never raise a second one
        logger.warning("could not read current material for %s", ticket_id, exc_info=True)
        return None
    return None if ctx is None else material_components(ctx)


def explain_material_change(attestation: Any, ticket_id: str, *, repo_root=None) -> str:
    """A compact clause naming which material component(s) differ from the signed ones.

    Total by construction: every failure mode degrades to a sentence that still tells the
    reader what to do. Never returns ticket content — only component names and magnitudes.
    """
    from .attest import _authoritative_manifest
    from .manifest import manifest_material_parts

    try:
        manifest = _authoritative_manifest(attestation)
    except Exception:  # noqa: BLE001 — an unreadable manifest is a degraded explanation, not an error
        manifest = None

    current = _current_components(ticket_id, repo_root)
    signed = manifest_material_parts(manifest)

    if signed and current is not None:
        delta = describe_delta(signed, current)
        if delta is not None:
            return delta
        return (
            "no material component differs — the attestation predates AC-checkbox "
            f"normalization (bug 330c), so re-signing is required; {CHECKBOX_NOTE}"
        )

    # Degraded path: an attestation signed before component hashing existed.
    pinned = _children_from_pins(manifest)
    if pinned is not None and current is not None:
        live = _live_children(ticket_id, repo_root)
        if live is not None and live != pinned:
            added, removed = sorted(live - pinned), sorted(pinned - live)
            bits = []
            if added:
                bits.append(
                    f"+{len(added)} ({', '.join(added[:3])}{' …' if len(added) > 3 else ''})"
                )
            if removed:
                bits.append(
                    f"-{len(removed)} ({', '.join(removed[:3])}{' …' if len(removed) > 3 else ''})"
                )
            return f"changed: children {' '.join(bits)}"
    return (
        "the changed component cannot be named: this attestation predates component-level "
        f"fingerprinting. Re-run `rebar review-plan {ticket_id}` and any future staleness "
        f"will name it. Note that {CHECKBOX_NOTE}"
    )


def _live_children(ticket_id: str, repo_root) -> set[str] | None:
    from .relation_snapshot import live_material_children

    try:
        return {
            str(k.get("ticket_id"))
            for k in live_material_children(ticket_id, repo_root=repo_root) or []
        }
    except Exception:  # noqa: BLE001 — best-effort enrichment of a degraded explanation
        return None
