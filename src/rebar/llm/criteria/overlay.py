"""Shared criteria overlay core — the gate-parameterized ``.rebar/criteria_routing.json``
merge / activation / cache-isolation logic both review gates delegate to (story 5065).

This GENERALIZES the overlay machinery landed for plan-review in story ef7e (which was
keyed to the literal ``"plan_review"`` gate key) so the SAME ``.rebar/criteria_routing.json``
can carry a per-gate map — ``{"plan_review": {…}, "code_review": {…}, "activate": [...]}`` —
and each gate reads its OWN key. The merge rules, the located load-time validation, and the
``(repo_root, overlay-content-signature)`` lru_cache isolation (the G6 cross-repo-leak fix)
are the exact ones from ef7e, now taking the gate key + the gate's packaged index + canonical
built-in set via a small per-gate REGISTRATION (:func:`register_gate`) that each gate's
registry module calls at import. The cached compute reads those providers by gate key —
mirroring how ef7e's cached function read the plan-review module globals — so a monkeypatch of
a gate's ``canonical`` set (as a test does) is still honoured on a fresh overlay signature.

For an overlay-ABSENT repo the merged routing is ``dict(packaged_index())`` — byte-identical
to the packaged index — so both gates behave exactly as before the unification. See ADR 0017
(+ ADR 0015 for the original plan-review overlay design this reuses)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .model import CriteriaError

_OVERLAY_FILENAME = "criteria_routing.json"
_PROJECT_PREFIX = "project."


@dataclass(frozen=True)
class _GateSpec:
    """The per-gate providers the shared overlay core needs, registered at gate import.

    ``packaged_index`` returns the gate's PACKAGED routing index (its own ``@lru_cache``d,
    immutable-per-binary read); ``canonical`` returns the gate's closed built-in id set.
    Both are callables (read fresh inside the cached compute) so a test monkeypatch of a
    gate's canonical set is honoured, exactly as ef7e read the module globals."""

    packaged_index: Callable[[], dict[str, Any]]
    canonical: Callable[[], Iterable[str]]


_GATES: dict[str, _GateSpec] = {}


def register_gate(
    gate_key: str,
    *,
    packaged_index: Callable[[], dict[str, Any]],
    canonical: Callable[[], Iterable[str]],
) -> None:
    """Register a gate's packaged-index + canonical providers under its ``gate_key``
    (``"plan_review"`` | ``"code_review"``). Called once at each gate registry module's
    import; idempotent (re-registering overwrites with the same providers)."""
    _GATES[gate_key] = _GateSpec(packaged_index=packaged_index, canonical=canonical)


def _spec(gate_key: str) -> _GateSpec:
    try:
        return _GATES[gate_key]
    except KeyError:  # pragma: no cover — a gate that forgot to register is a wiring bug
        raise CriteriaError(
            f"no criteria gate registered for {gate_key!r} "
            f"(known gates: {sorted(_GATES)}); its registry module must call register_gate()"
        ) from None


# ── overlay discovery (repo root → `.rebar/criteria_routing.json`) ──────────────────
def _resolve_repo_root(repo_root: str | None) -> str | None:
    """Resolve an overlay discovery root: the explicit arg, else the rebar project root
    (``config.repo_root()`` — the same root ``get_prompt`` resolves ``.rebar/prompts/``
    overrides against). Returns ``None`` only when there is no resolvable root."""
    if repo_root is not None:
        return str(repo_root)
    try:
        from rebar import config as _config

        return str(_config.repo_root())
    except Exception:  # noqa: BLE001 — no repo ⇒ packaged criteria only
        return None


def _overlay_path(repo_root: str | None) -> Path | None:
    if not repo_root:
        return None
    return Path(repo_root) / ".rebar" / _OVERLAY_FILENAME


def _overlay_signature(repo_root: str | None) -> str:
    """A content signature of the overlay file (sha256 of its bytes, or ``""`` when absent) —
    the cache key that makes an EDIT to the overlay invalidate the memo without an explicit
    clear. Prefer content over mtime (mtime granularity is coarse/flaky)."""
    path = _overlay_path(repo_root)
    if path is None:
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _load_overlay(repo_root: str | None) -> dict[str, Any] | None:
    """Read + parse the project's ``.rebar/criteria_routing.json`` overlay, or ``None`` when
    absent. A malformed overlay is a LOCATED :class:`CriteriaError` (never a silent skip) —
    the file path is named so the author can fix it."""
    path = _overlay_path(repo_root)
    if path is None or not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CriteriaError(f"cannot read criteria overlay {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CriteriaError(f"criteria overlay {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CriteriaError(
            f"criteria overlay {path} must be a JSON object "
            f"{{'plan_review': {{...}}, 'activate': [...]}}; got {type(data).__name__}"
        )
    return data


def _validate_routing_entry(cid: str, entry: Any, *, where: str) -> None:
    """Structural floor-check on ONE routing entry (located error). Mirrors the shape the
    packaged index carries so an overlay entry can't smuggle a malformed record past load."""
    if not isinstance(entry, dict):
        raise CriteriaError(
            f"{where}: routing for {cid!r} must be an object, got {type(entry).__name__}"
        )
    # A project criterion's <name> must be the SAME filesystem-safe charset as any prompt id
    # (task stew-kid-motif): [A-Za-z0-9][A-Za-z0-9-]* — alnum + dash, NO dots/underscores — so
    # the single namespace dot is the only '.', and `criterion_prompt_id` (project.<name> →
    # plan-review-project-<name>) is a total, injective, filesystem-safe map. A dotted/underscored
    # name would make the rubric filename unauthorable (or the id→prompt-id map non-injective).
    if cid.startswith(_PROJECT_PREFIX):
        name = cid[len(_PROJECT_PREFIX) :]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", name):
            raise CriteriaError(
                f"{where}: project criterion name {name!r} in {cid!r} must match "
                r"[A-Za-z0-9][A-Za-z0-9-]* (alnum + dash, no dots/underscores) — the rubric is "
                "stored at plan-review-project-<name>.md, so the name must be filesystem-safe"
            )
    exec_v = entry.get("exec", "1-TURN")
    if not isinstance(exec_v, str) or exec_v.upper() not in ("1-TURN", "2-STEP", "AGENT", "DET"):
        raise CriteriaError(
            f"{where}: criterion {cid!r} has invalid exec {exec_v!r} "
            "(expected one of 1-TURN / 2-STEP / AGENT / DET)"
        )
    bt = entry.get("block_threshold", 0.95)
    if not isinstance(bt, (int, float)) or isinstance(bt, bool) or not (0.0 <= float(bt) <= 1.0):
        raise CriteriaError(
            f"{where}: criterion {cid!r} block_threshold must be a number in [0,1], got {bt!r}"
        )
    posture = entry.get("default_posture", "advisory")
    if posture not in ("advisory", "blocking"):
        raise CriteriaError(
            f"{where}: criterion {cid!r} default_posture must be 'advisory' or 'blocking', "
            f"got {posture!r}"
        )
    # fail_mode governs an exec:DET criterion's abstain posture (fail-open records coverage;
    # fail-closed blocks on absence). Validated only when present (a non-DET / silent entry
    # defaults to "open" downstream), mirroring the code-review consumer's default.
    if "fail_mode" in entry and entry.get("fail_mode") not in ("open", "closed"):
        raise CriteriaError(
            f"{where}: criterion {cid!r} fail_mode must be 'open' or 'closed', "
            f"got {entry.get('fail_mode')!r}"
        )
    # A bool `disabled` key TURNS OFF a built-in criterion (removed from effective_criteria, so
    # it is never loaded/run) — allowed ONLY on an un-prefixed built-in id, never on a
    # `project.` id (a project criterion is turned off by omitting it from `activate`).
    if "disabled" in entry:
        if cid.startswith(_PROJECT_PREFIX):
            raise CriteriaError(
                f"{where}: criterion {cid!r} may not carry 'disabled' — only a built-in "
                "criterion can be disabled (omit a project id from 'activate' to turn it off)"
            )
        if not isinstance(entry["disabled"], bool):
            raise CriteriaError(
                f"{where}: criterion {cid!r} 'disabled' must be a boolean, "
                f"got {entry.get('disabled')!r}"
            )
    ap = entry.get("applies_at")
    if isinstance(ap, dict):
        # Plan-review proportionate scrutiny is keyed on container/leaf, never ticket
        # type. The legacy `levels`/`container_only` vocabulary is rejected with a
        # migration hint so a stale overlay fails loudly, not silently ignored.
        # (Code-review criteria don't carry applies_at, so this is inert for them.)
        for legacy in ("levels", "container_only"):
            if legacy in ap:
                raise CriteriaError(
                    f"{where}: criterion {cid!r} applies_at.{legacy} is no longer supported "
                    "— proportionate scrutiny is keyed on container/leaf; use "
                    '\'scope\': ["container", "leaf"] (either or both) instead'
                )
        scope = ap.get("scope")
        if scope is not None and (
            not isinstance(scope, list)
            or not scope
            or any(s not in ("container", "leaf") for s in scope)
        ):
            raise CriteriaError(
                f"{where}: criterion {cid!r} applies_at.scope must be a non-empty list of "
                f"'container'/'leaf', got {scope!r}"
            )
        # `require_parent_id` (G7): a boolean axis restricting a criterion to tickets WITH a
        # parent. bool ONLY (Python bool is a subclass of int, so check isinstance explicitly
        # to reject int/str).
        if "require_parent_id" in ap and not isinstance(ap["require_parent_id"], bool):
            raise CriteriaError(
                f"{where}: criterion {cid!r} applies_at.require_parent_id must be a boolean, "
                f"got {ap['require_parent_id']!r}"
            )


# ── effective (overlay-merged) views, gate-parameterized + repo-keyed ───────────────
def effective_routing(repo_root: str | None = None, *, gate_key: str) -> dict[str, Any]:
    """The gate's packaged routing index MERGED with the project overlay's ``gate_key`` map
    (repo-keyed, memoized by overlay content-signature — so no cross-repo leakage). Overlay
    merge rules (each violation is a LOCATED load-time error):

    * an un-prefixed **built-in** id ⇒ re-tune (routing merged over the packaged entry);
    * a ``project.<name>``-prefixed id ⇒ a net-new project criterion (added);
    * a ``project.``-id equal to a built-in id ⇒ REJECT; a net-new id that is NOT
      ``project.``-prefixed ⇒ REJECT (must be namespaced)."""
    rr = _resolve_repo_root(repo_root)
    return _effective_routing_cached(gate_key, rr or "", _overlay_signature(rr))


@lru_cache(maxsize=256)
def _effective_routing_cached(gate_key: str, rr: str, _overlay_sig: str) -> dict[str, Any]:
    """The (gate_key, repo_root, overlay-signature)-keyed compute for :func:`effective_routing`.
    The signature is a pure CACHE KEY (an overlay edit ⇒ a new key ⇒ a fresh compute); the merge
    reads the overlay bytes fresh. ``rr == ""`` means no resolvable repo (packaged-only)."""
    spec = _spec(gate_key)
    rr_arg: str | None = rr or None
    merged: dict[str, Any] = dict(spec.packaged_index())
    canonical = frozenset(spec.canonical())
    overlay = _load_overlay(rr_arg)
    if overlay is not None:
        gate = overlay.get(gate_key) or {}
        if not isinstance(gate, dict):
            raise CriteriaError(
                f"criteria overlay {_overlay_path(rr_arg)}: '{gate_key}' must be an object of "
                f"{{id: routing}}, got {type(gate).__name__}"
            )
        where = f"criteria overlay {_overlay_path(rr_arg)} [{gate_key}]"
        for cid, entry in gate.items():
            _validate_routing_entry(cid, entry, where=where)
            is_builtin = cid in canonical
            if cid.startswith(_PROJECT_PREFIX):
                if is_builtin:
                    raise CriteriaError(
                        f"{where}: project id {cid!r} collides with a built-in criterion "
                        "(a project criterion can never rebind a built-in)"
                    )
                merged[cid] = entry
            elif is_builtin:
                merged[cid] = {**merged[cid], **entry}  # re-tune: overlay wins per-key
            else:
                raise CriteriaError(
                    f"{where}: net-new criterion id {cid!r} must be "
                    f"'{_PROJECT_PREFIX}<name>'-prefixed "
                    "(an un-prefixed id may only re-tune an existing built-in)"
                )
    return merged


def effective_criteria(repo_root: str | None = None, *, gate_key: str) -> tuple[str, ...]:
    """The ACTIVE criterion-id vocabulary for a repo = the gate's canonical built-ins ∪ the
    project ids listed in the overlay's ``activate`` list (presence in the file ≠ active),
    minus any built-in the overlay DISABLES. An activated project id with no routing entry, or
    a non-``project.`` id in ``activate``, is a LOCATED load-time error."""
    rr = _resolve_repo_root(repo_root)
    canonical = frozenset(_spec(gate_key).canonical())
    overlay = _load_overlay(rr)
    ids = set(canonical)
    if overlay is not None:
        activate = overlay.get("activate") or []
        if not isinstance(activate, list):
            raise CriteriaError(
                f"criteria overlay {_overlay_path(rr)}: 'activate' must be a list of ids, "
                f"got {type(activate).__name__}"
            )
        routing = effective_routing(rr, gate_key=gate_key)
        for aid in activate:
            if not isinstance(aid, str):
                raise CriteriaError(
                    f"criteria overlay {_overlay_path(rr)}: 'activate' entries must be strings"
                )
            if aid in canonical:
                continue  # activating a built-in is a no-op (built-ins are always active)
            if not aid.startswith(_PROJECT_PREFIX):
                raise CriteriaError(
                    f"criteria overlay {_overlay_path(rr)}: activated id {aid!r} must be a "
                    f"'{_PROJECT_PREFIX}<name>' project criterion (built-ins are always active)"
                )
            if aid not in routing:
                # The `activate` list is SHARED across gates (one top-level list), so a project
                # criterion defined for a DIFFERENT gate legitimately appears here — it is simply
                # not active for THIS gate. Only ERROR when the id is defined for NO gate at all
                # (a genuine dangling activation). This keeps each gate's vocabulary isolated
                # (story 5065) while preserving the "activate a criterion that exists nowhere is a
                # located error" contract.
                in_another_gate = any(
                    isinstance(overlay.get(g), dict) and aid in overlay[g]
                    for g in _GATES
                    if g != gate_key
                )
                if in_another_gate:
                    continue
                raise CriteriaError(
                    f"criteria overlay {_overlay_path(rr)}: activated criterion {aid!r} has no "
                    f"'{gate_key}' routing entry"
                )
            ids.add(aid)
    ids.difference_update(disabled_builtins(rr, gate_key=gate_key))
    return tuple(sorted(ids))


def disabled_builtins(repo_root: str | None = None, *, gate_key: str) -> list[str]:
    """The sorted built-in criterion ids the project overlay DISABLES (a ``"disabled": true``
    key on an un-prefixed built-in routing entry). A disabled built-in is EXCLUDED from
    :func:`effective_criteria` (never loaded/run) while its routing entry stays resolvable in
    :func:`effective_routing`. Empty (``[]``) when there is no overlay / nothing disabled — so
    an overlay-absent repo is byte-identical to the packaged registry."""
    routing = effective_routing(repo_root, gate_key=gate_key)
    canonical = frozenset(_spec(gate_key).canonical())
    return sorted(
        cid
        for cid, entry in routing.items()
        if cid in canonical and isinstance(entry, dict) and entry.get("disabled") is True
    )


def clear_caches() -> None:
    """Clear the shared overlay-merged lru_cache (the ``effective_routing`` memo for EVERY
    gate). Called by ``prompt_library._invalidate_caches`` so a freshly-authored criterion /
    overlay is visible in-process without a restart (an overlay EDIT self-invalidates via its
    content signature; this covers a same-signature in-place authoring write)."""
    _effective_routing_cached.cache_clear()
