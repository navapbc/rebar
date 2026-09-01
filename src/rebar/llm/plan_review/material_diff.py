"""Component-level plan-material fingerprinting, and the explainer that names WHAT changed.

The plan-review / completion attestations bind a SINGLE composite hash of the ticket's
material plan content (:func:`pass1.material_fingerprint`). A composite is one-way, so when
it stops matching, the gate knew only *that* something changed — every message therefore
recited a fixed list of description, AC, file_impact, and children, which named an input
that does not exist ("AC" is not a basis key; acceptance criteria live inside
``description``) and left
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
_CHECKBOX_ITEM_RE = re.compile(r"^\s*[-*]\s*\[([xX ])\]", re.MULTILINE)

#: Diagnostic component name recorded ALONGSIDE the basis components. It is absent from
#: ``COMPONENT_ORDER`` on purpose: nothing hashed into the composite, nothing
#: :func:`describe_delta` reports — it exists only so the explainer can separate "the text
#: moved" from "boxes were also ticked".
BOXES_COMPONENT = "description_boxes"
DIAGNOSTIC_COMPONENTS = (BOXES_COMPONENT,)

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


def normalize_insignificant_whitespace(description: str) -> str:
    """Erase whitespace that carries no plan substance, so a cosmetic edit cannot stale a
    signed review (bug 2be7 — ``rebar edit --description="$(cat file)"`` strips the trailing
    newline via shell command substitution and the gate reported ``stale-material``).

    Normalized out, and ONLY these: line-ending form (CRLF/CR -> LF), whitespace at the END
    of a line (so a whitespace-only separator line equals an empty one), and blank lines at
    the DOCUMENT boundary. Everything else stays material — LEADING indentation is preserved
    because it restructures markdown list nesting, whitespace between two non-whitespace
    characters is preserved, and an interior blank line is never removed.
    """
    text = description.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def canonical_description(
    description: str, *, normalize_checkboxes: bool = True, normalize_whitespace: bool | None = None
) -> str:
    """Every canonicalization the fingerprint applies to the description, in one place.

    ``normalize_whitespace`` defaults to ``normalize_checkboxes`` so the single legacy
    switch turns BOTH off at once; pass it explicitly to reproduce an intermediate
    algorithm generation (checkbox-only, i.e. post-330c / pre-2be7)."""
    if normalize_whitespace is None:
        normalize_whitespace = normalize_checkboxes
    if normalize_whitespace:
        description = normalize_insignificant_whitespace(description)
    if normalize_checkboxes:
        description = normalize_checkbox_state(description)
    return description


def material_basis(
    ctx: PlanContext,
    *,
    normalize_checkboxes: bool = True,
    normalize_whitespace: bool | None = None,
    normalize_reason: bool = True,
) -> dict[str, Any]:
    """The ordered mapping the composite fingerprint hashes.

    Single-sourced here so :func:`pass1.material_fingerprint` and :func:`material_components`
    can never disagree about what "material" means — a divergence would let the explainer
    name a component the gate did not actually decide on.

    ``normalize_checkboxes`` selects the pre-330c LEGACY algorithm and so governs BOTH
    description canonicalizations: when False the RAW description is hashed — neither
    checkbox state (:func:`normalize_checkbox_state`) nor insignificant whitespace
    (:func:`normalize_insignificant_whitespace`) is normalized away. The flag keeps its
    historical name because the repo documents it as the legacy-algorithm switch;
    ``normalize_whitespace`` overrides only the whitespace half, which the grandfather
    fallback uses to recompute the intermediate (checkbox-only) generation.
    ``normalize_reason=False`` independently reproduces generations that hashed the raw
    no-file-impact reason before reason canonicalization was introduced.
    """
    basis: dict[str, Any] = {
        "ticket_id": ctx.ticket_id,
        "description": canonical_description(
            ctx.description,
            normalize_checkboxes=normalize_checkboxes,
            normalize_whitespace=normalize_whitespace,
        ),
        "file_impact": ctx.state.get("file_impact") or [],
        "children": sorted(c.get("ticket_id", "") for c in ctx.children),
    }
    if ctx.state.get("file_impact_scope") == "none":
        reason = ctx.state.get("no_file_impact_reason")
        reason = reason if isinstance(reason, str) else ""
        if normalize_reason:
            reason = normalize_insignificant_whitespace(reason)
        basis["file_impact_scope"] = {
            "kind": "none",
            "reason": reason,
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


def checkbox_state_component(description: str) -> tuple[str, int]:
    """``(hash16, ticked_count)`` for the checkbox STATE the description carries.

    Diagnostic ONLY — deliberately *not* a basis key (see :data:`DIAGNOSTIC_COMPONENTS`).
    Box state is normalized out of the material basis, so the signed component hashes carry
    no record of it and a stale-material message could not previously say whether an author
    who ticked boxes *and* edited prose in one ``rebar edit`` was stale because of the prose
    (they always are) or the ticks (they never are). Recording the state sequence separately
    lets the explainer answer that. Carries no ticket content: a state string and a count.
    """
    states = [m.group(1).lower() for m in _CHECKBOX_ITEM_RE.finditer(description)]
    blob = "".join(states)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return digest, sum(1 for s in states if s == "x")


def components_with_diagnostics(
    ctx: PlanContext, *, normalize_checkboxes: bool = True
) -> dict[str, tuple[str, int]]:
    """The material components PLUS the non-basis diagnostics the explainer reads.

    This is what rides on the manifest and what the live-state reader returns; the composite
    fingerprint and :func:`describe_delta` both ignore the diagnostic keys, so nothing here
    can change a gate outcome.
    """
    parts = material_components(ctx, normalize_checkboxes=normalize_checkboxes)
    parts[BOXES_COMPONENT] = checkbox_state_component(ctx.description)
    return parts


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
        return components_with_diagnostics(ctx)
    except Exception:
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


def _description_detail(
    signed: dict[str, tuple[str, int]], current: dict[str, tuple[str, int]]
) -> str:
    """Say what moved INSIDE ``description``: its text, and whether boxes moved as well.

    Empty string unless ``description`` is one of the changed components and BOTH sides
    recorded the (diagnostic) checkbox state — an attestation signed before that line
    existed simply gets no clause rather than a guess.
    """
    if signed.get("description") == current.get("description"):
        return ""
    signed_boxes, current_boxes = signed.get(BOXES_COMPONENT), current.get(BOXES_COMPONENT)
    if signed_boxes is None or current_boxes is None:
        return ""
    if signed_boxes == current_boxes:
        return "; its TEXT changed, checkbox state is unchanged"
    return (
        "; its TEXT changed, and checkbox state changed too "
        f"({signed_boxes[1]} -> {current_boxes[1]} ticked), which is NOT the cause"
    )


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
    except Exception:
        logger.warning("could not read current material for %s", ticket_id, exc_info=True)
        return None
    return None if ctx is None else components_with_diagnostics(ctx)


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
            # The note rides on THIS branch too — it is the branch a current attestation
            # takes, so it is the one nearly every author reads (bug b886).
            return f"{delta}{_description_detail(signed, current)}; note that {CHECKBOX_NOTE}"
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
