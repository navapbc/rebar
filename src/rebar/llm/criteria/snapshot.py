"""Immutable effective review-policy snapshots — the single compiled authority a review
gate consumes instead of rereading ambient ``.rebar/criteria_routing.json`` policy (RP-06 S1).

WHY this exists. The overlay core (:mod:`rebar.llm.criteria.overlay`) already reconciles the
packaged routing index with a project overlay into an *effective* view, and each gate's
registry exposes thin readers over it. But every consumer reads that ambient policy on its
own cadence, so two consumers of the same gate can disagree the instant the overlay changes
under them, and the code-review applicability rule (``applies_to`` globs) lives in one gate's
registry where a plan-review reader can never see it. This module compiles ONE immutable,
digest-bound projection — effective built-ins, project LLM criteria, project DET criteria,
routing, and per-id source provenance for BOTH gates — from a repo root, and hands it to
consumers whole. It is DATA/POLICY only: it does not interpret YAML/BPMN topology, execute a
criterion, or decide a verdict. It sits ALONGSIDE the existing registry readers (which remain
compatibility adapters over the same overlay core); rollback is a plain code revert.

The digest reuses the overlay's own content signature (:func:`overlay._overlay_signature`)
combined with a canonical serialization of the compiled per-gate routing, so it is stable
across recompiles of the same policy and changes exactly when overlay content changes. See
ADR 0102 (it is a projection composed with ADR 0098's ``OperationSnapshot`` and extends the
ADR 0017 shared-``rebar.llm.criteria`` delegation layer).

The gate-specific applicability rule for code-review project LLM criteria — an empty/absent
``applies_to`` is legacy "ungated"; ``["**"]`` is repository-wide and selects UNCONDITIONALLY
(including an empty ``changed_files`` set); a scoped glob selects only on a match — is
implemented ONCE in :func:`select_project_applicability` and shared by both this snapshot and
the code-review registry consumer, so the ``[]`` → ``["**"]`` migration never regresses at the
empty-``changed_files`` edge.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from . import overlay
from .model import CriteriaError

_PROJECT_PREFIX = "project."
_REPO_WIDE_GLOB = "**"
_GATES: tuple[str, ...] = ("plan_review", "code_review")


@dataclass(frozen=True)
class ApplicabilityDecision:
    """Whether a code-review project criterion's ``applies_to`` admits a review, plus a typed
    reason (``"repository-wide"`` / ``"glob-match:<glob>"`` / ``"no-changed-path-match"`` /
    ``"ungated"``)."""

    applies: bool
    reason: str


@dataclass(frozen=True)
class CriterionRecord:
    """The compiled, provenance-bearing view of ONE criterion under ONE gate."""

    id: str
    gate: str
    exec: str
    kind: str
    source: str
    routing: dict[str, Any]


def select_project_applicability(
    globs: Sequence[str], changed_files: Sequence[str]
) -> ApplicabilityDecision:
    """The one shared "does this project criterion's ``applies_to`` admit the review?" rule.

    * empty / absent globs ⇒ ``ungated`` (legacy meaning: runs on every review);
    * ``"**"`` present ⇒ ``repository-wide``, selecting UNCONDITIONALLY — including when
      ``changed_files`` is empty (this reproduces the prior ungated ``applies_to: []``
      short-circuit; a naive ``any(glob_match(f, "**") ...)`` over an empty ``changed_files``
      would wrongly return False);
    * a scoped glob that matches any changed path ⇒ ``glob-match:<glob>`` (the matching glob);
    * scoped globs, none matching ⇒ ``no-changed-path-match`` (does not select).
    """
    real_globs = [g for g in globs if isinstance(g, str) and g]
    if not real_globs:
        return ApplicabilityDecision(applies=True, reason="ungated")
    if _REPO_WIDE_GLOB in real_globs:
        return ApplicabilityDecision(applies=True, reason="repository-wide")
    from rebar._engine_support.commit_impact import glob_match

    for glob in real_globs:
        if any(glob_match(path, glob) for path in changed_files):
            return ApplicabilityDecision(applies=True, reason=f"glob-match:{glob}")
    return ApplicabilityDecision(applies=False, reason="no-changed-path-match")


def _exec_of(entry: dict[str, Any]) -> str:
    raw = entry.get("exec", "1-TURN")
    return raw.upper() if isinstance(raw, str) else "1-TURN"


def _validate_code_review_applies_to(cid: str, entry: dict[str, Any], where: str) -> None:
    """The stricter compile-time check for a ``code_review`` ``project.``-prefixed LLM
    (exec != DET) criterion: ``applies_to`` MUST be a non-empty list of non-empty glob
    strings. The shared overlay validator deliberately permits an empty list ("ungated") for
    BOTH gates; this narrower rule lives here (a code-review-gated path) so plan-review project
    criteria and DET criteria are unaffected."""
    globs = entry.get("applies_to")
    ok = isinstance(globs, list) and bool(globs) and all(isinstance(g, str) and g for g in globs)
    if ok:
        return
    raise CriteriaError(
        f"{where}: code-review project criterion {cid!r} must declare applies_to as a "
        "non-empty list of non-empty repository-relative glob strings in "
        'criteria_routing.json (use ["**"] for a repository-wide criterion), '
        f"got {globs!r}"
    )


def _precheck_code_review_applies_to(repo_root: str | None, where: str) -> None:
    """Surface the remedy-bearing ``applies_to`` error for a ``code_review`` ``project.``
    non-DET criterion BEFORE :func:`overlay.effective_routing` runs — otherwise the SHARED
    :func:`overlay._validate_applies_to` (which deliberately keeps an empty list legal for both
    gates) would raise its own remedy-LESS message first for a non-list / blank / non-string
    ``applies_to``. We read the RAW overlay and re-apply the narrower code-review rule; all
    non-``project.``/DET/mis-shaped entries are skipped here (the overlay core validates those),
    so only a well-shaped ``project.`` non-DET entry with a bad ``applies_to`` raises."""
    raw = overlay._load_overlay(repo_root)
    if not isinstance(raw, dict):
        return
    gate_map = raw.get("code_review")
    if not isinstance(gate_map, dict):
        return
    for cid, entry in gate_map.items():
        if not (isinstance(cid, str) and cid.startswith(_PROJECT_PREFIX)):
            continue
        if not isinstance(entry, dict) or _exec_of(entry) == "DET":
            continue
        _validate_code_review_applies_to(cid, entry, where)


@dataclass(frozen=True)
class _GateView:
    criteria: tuple[str, ...]
    routing: dict[str, Any]
    canonical: frozenset[str]
    disabled: tuple[str, ...]


def _compile_gate(repo_root: str | None, gate: str) -> _GateView:
    where = f"criteria overlay {overlay._overlay_path(repo_root)} [{gate}]"
    if gate == "code_review":
        _precheck_code_review_applies_to(repo_root, where)
    routing = overlay.effective_routing(repo_root, gate_key=gate)
    canonical = frozenset(overlay._spec(gate).canonical())
    active = overlay.effective_criteria(repo_root, gate_key=gate)
    if gate == "code_review":
        for cid in active:
            if not cid.startswith(_PROJECT_PREFIX):
                continue
            entry = routing.get(cid) or {}
            if _exec_of(entry) != "DET":
                _validate_code_review_applies_to(cid, entry, where)
    return _GateView(
        criteria=active,
        routing=routing,
        canonical=canonical,
        disabled=tuple(overlay.disabled_builtins(repo_root, gate_key=gate)),
    )


def _canonical_routing_payload(views: dict[str, _GateView]) -> str:
    return json.dumps(
        {gate: view.routing for gate, view in sorted(views.items())},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


@dataclass(frozen=True)
class CriteriaSnapshot:
    """One immutable, digest-bound view of the effective review policy for both gates.

    Consumers receive it and read policy off it; they do not reread ambient overlay state.
    Every accessor returns a FRESH copy of mutable data, so mutating a return value can never
    corrupt the snapshot."""

    repo_root: str | None
    digest: str
    _views: dict[str, _GateView] = field(repr=False)

    def _view(self, gate: str) -> _GateView:
        try:
            return self._views[gate]
        except KeyError:  # pragma: no cover — a bad gate key is a caller bug
            raise CriteriaError(
                f"unknown review gate {gate!r} (known gates: {sorted(self._views)})"
            ) from None

    def criteria(self, gate: str) -> tuple[str, ...]:
        """The active criterion-id vocabulary for ``gate`` (delegates to the overlay core)."""
        return self._view(gate).criteria

    def routing(self, gate: str) -> dict[str, Any]:
        """A FRESH deep copy of the effective routing for ``gate`` (mutating it is safe)."""
        return json.loads(json.dumps(self._view(gate).routing, default=str))

    def disabled_builtins(self, gate: str) -> tuple[str, ...]:
        """The built-in ids the overlay DISABLES for ``gate`` (delegates to the overlay core)."""
        return self._view(gate).disabled

    def builtins(self, gate: str) -> tuple[str, ...]:
        """The active built-in (un-``project.``-prefixed) criterion ids for ``gate``."""
        return tuple(
            cid for cid in self._view(gate).criteria if not cid.startswith(_PROJECT_PREFIX)
        )

    def project_llm(self, gate: str) -> tuple[str, ...]:
        """The active ``project.`` ids whose exec tier is NOT ``DET`` (LLM criteria)."""
        return self._project_ids(gate, det=False)

    def project_det(self, gate: str) -> tuple[str, ...]:
        """The active ``project.`` ids whose exec tier IS ``DET`` (deterministic detectors)."""
        return self._project_ids(gate, det=True)

    def _project_ids(self, gate: str, *, det: bool) -> tuple[str, ...]:
        view = self._view(gate)
        out = []
        for cid in view.criteria:
            if not cid.startswith(_PROJECT_PREFIX):
                continue
            is_det = _exec_of(view.routing.get(cid) or {}) == "DET"
            if is_det == det:
                out.append(cid)
        return tuple(out)

    def record(self, gate: str, cid: str) -> CriterionRecord:
        """The provenance-bearing :class:`CriterionRecord` for ``cid`` under ``gate``."""
        view = self._view(gate)
        entry = dict(view.routing.get(cid) or {})
        is_builtin = cid in view.canonical
        source = "packaged" if is_builtin else str(overlay._overlay_path(self.repo_root))
        return CriterionRecord(
            id=cid,
            gate=gate,
            exec=_exec_of(entry),
            kind="builtin" if is_builtin else "project",
            source=source,
            routing=entry,
        )

    def code_review_project_applies(
        self, cid: str, changed_files: Sequence[str]
    ) -> ApplicabilityDecision:
        """Whether the code-review project criterion ``cid`` admits ``changed_files``, using the
        SAME shared rule the code-review consumer uses.

        See :func:`select_project_applicability`."""
        entry = self._view("code_review").routing.get(cid) or {}
        globs = entry.get("applies_to") or []
        return select_project_applicability(globs, changed_files)


def compile_snapshot(repo_root: str | None = None) -> CriteriaSnapshot:
    """Compile the immutable effective review-policy snapshot for ``repo_root`` (or the ambient
    project root when ``None``), covering both the ``plan_review`` and ``code_review`` gates."""
    # Ensure both gates have registered their packaged-index/canonical providers with the
    # overlay core; the registries self-register at import. Deferred to call time (and via
    # importlib, for a side-effect-only import) to keep the package import graph acyclic.
    import importlib

    importlib.import_module("rebar.llm.plan_review.registry")
    importlib.import_module("rebar.llm.code_review.registry")

    views = {gate: _compile_gate(repo_root, gate) for gate in _GATES}
    signature = overlay._overlay_signature(repo_root)
    digest = hashlib.sha256(
        (signature + "\n" + _canonical_routing_payload(views)).encode("utf-8")
    ).hexdigest()
    return CriteriaSnapshot(
        repo_root=repo_root if repo_root is None else str(repo_root),
        digest=digest,
        _views=views,
    )
