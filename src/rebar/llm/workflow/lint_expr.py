"""Single-``${{ … }}``-expression reference validator (extracted from ``lint_refs``).

A pure-stdlib leaf. Given one expression body and the frame :class:`_Scope` it
resolves against, :func:`_validate_expression` proves the expression is on the
closed allow-list — ``inputs.<name>``, ``steps.<id>.outputs.<name>``,
``secrets.<name>``, and the v2 frame bindings ``loop.<var>`` / ``map.<as>`` — and
that its reference actually resolves: a declared or engine-injected input, an
upstream same-frame (or enclosing) step whose declared :class:`_OutputContract`
produces the named field, or an in-scope loop/map binding.

This module imports nothing from rebar; :mod:`rebar.llm.workflow.lint_refs`
re-exports these names for back-compat (``ENGINE_INJECTED_INPUTS`` is consumed by
``lint.py`` and the contract tests via ``lint_refs``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

# ── Expression grammar (the closed allow-list) ────────────────────────────────

_ID = r"[A-Za-z_][A-Za-z0-9_-]*"
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_ALLOWED_EXPR = (
    ("input", re.compile(rf"^inputs\.({_ID})$")),
    ("step_output", re.compile(rf"^steps\.({_ID})\.outputs\.({_ID})$")),
    ("secret", re.compile(rf"^secrets\.({_NAME})$")),
    # v2 frame-scoped bindings: a loop's iteration index (`${{ loop.<var> }}`) and a
    # map's per-element binding (`${{ map.<as> }}`). Whole-value only (no attribute
    # walk past the binding name) — the same closed-grammar stance as step outputs.
    ("loop_var", re.compile(rf"^loop\.({_ID})$")),
    ("map_bind", re.compile(rf"^map\.({_ID})$")),
)

# The engine-injected `${{ inputs.* }}` namespace (workflow authoring v2, 5e78): vars
# the executor seeds into every run (target ticket + its rendered context + repo root),
# valid to reference though NOT declared in `inputs:`. SINGLE SOURCE OF TRUTH — the
# linter allow-lists these and the engine seeds exactly these; never hard-code inline.
ENGINE_INJECTED_INPUTS: frozenset[str] = frozenset({"ticket_id", "ticket_context", "repo_path"})


# ── Expression validation ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _OutputContract:
    """A producer step's declared output field-name contract for the ref linter.

    ``literals`` are exact output keys (a schema's ``properties``); ``patterns`` are
    compiled ``patternProperties`` regexes for dynamic keys (e.g. ``include_<overlay>``
    on ``overlay_union_output``). A referenced field is produced iff it is a literal
    OR fully matches a pattern — so a dynamic-keyed ref is honored while a typo that
    matches neither is still flagged.
    """

    literals: frozenset[str]
    patterns: tuple[re.Pattern[str], ...]

    def produces(self, field_name: str) -> bool:
        return field_name in self.literals or any(p.fullmatch(field_name) for p in self.patterns)

    def describe(self) -> str:
        parts = sorted(self.literals) + [f"match /{p.pattern}/" for p in self.patterns]
        return ", ".join(parts)


@dataclass(frozen=True)
class _Scope:
    """The lexical scope a step's expressions resolve against, in one frame.

    A step-output reference resolves with SAME-FRAME PRECEDENCE: a referenced id is
    matched against the current frame first, then the enclosing scope — so an outer
    step sharing an id with a same-frame sibling can never mask a missing-``needs``
    error.

    * ``siblings`` — every step id in the CURRENT frame.
    * ``frame_upstream`` — this step's same-frame ``needs`` ancestors (the subset of
      ``siblings`` whose outputs are already available).
    * ``outer`` — step ids visible from ENCLOSING frames (all completed before this
      frame ran); for a loop's ``while``/``until`` it additionally carries the loop's
      own body ids (the recorded-output the condition derives from).
    * ``loop_vars`` / ``map_binds`` — the in-scope frame bindings for
      ``${{ loop.<var> }}`` / ``${{ map.<as> }}``.
    """

    inputs: frozenset[str]
    siblings: frozenset[str]
    frame_upstream: frozenset[str]
    outer: frozenset[str]
    loop_vars: frozenset[str]
    map_binds: frozenset[str]
    # Doc-wide step-id → declared OUTPUT field names (None when the producer has no
    # contract). A `${{ steps.<id>.outputs.<f> }}` ref checks <f> against this when
    # known; an UNKNOWN producer is never flagged (workflow authoring v2, 5e78).
    output_fields: Mapping[str, _OutputContract | None]


def _validate_expression(inner: str, *, step_id: str, scope: _Scope) -> str | None:
    """Validate one ``${{ … }}`` body against the allow-list + frame-scoped
    reference integrity.

    Returns an error message, or ``None`` if the expression is allowed and resolves.
    """
    expr = inner.strip()
    kind = None
    match = None
    for k, pat in _ALLOWED_EXPR:
        match = pat.match(expr)
        if match:
            kind = k
            break
    if kind is None or match is None:
        return (
            f"disallowed expression {expr!r} (the closed allow-list is "
            f"inputs.<name>, steps.<id>.outputs.<name>, secrets.<name>, "
            f"loop.<var>, map.<as>)"
        )
    if kind == "input":
        name = match.group(1)
        # An engine-injected var (the target ticket + context + repo root) is always
        # valid even when not declared in `inputs:`; everything else must be declared.
        if name not in scope.inputs and name not in ENGINE_INJECTED_INPUTS:
            return f"references undeclared workflow input {name!r}"
    elif kind == "step_output":
        ref = match.group(1)
        field_name = match.group(2)
        if ref == step_id:
            return f"references its own output (steps.{ref}.outputs.*)"
        # Same-frame precedence: if the id names a sibling, it MUST be an upstream
        # sibling (an outer step with the same id cannot satisfy the reference).
        if ref in scope.siblings:
            if ref not in scope.frame_upstream:
                return (
                    f"references output of step {ref!r}, which is not an upstream "
                    f"dependency in this frame — add {ref!r} to this step's `needs` "
                    f"(a `needs` edge may not cross a frame boundary)"
                )
        elif ref not in scope.outer:
            return f"references unknown step {ref!r}"
        # Name-existence against the producer's declared OUTPUT contract — only when
        # KNOWN; a producer with no contract maps to None and is never flagged.
        declared = scope.output_fields.get(ref)
        if declared is not None and not declared.produces(field_name):
            return (
                f"references output {field_name!r} not produced by step {ref!r} — its "
                f"declared outputs are {{{declared.describe()}}}"
            )
    elif kind == "loop_var":
        name = match.group(1)
        if name not in scope.loop_vars:
            return (
                f"references loop variable {name!r} that is not in scope — only the "
                f"`var` of an enclosing `loop` is bound as `${{{{ loop.{name} }}}}`"
            )
    elif kind == "map_bind":
        name = match.group(1)
        if name not in scope.map_binds:
            return (
                f"references map binding {name!r} that is not in scope — only the "
                f"`as`/`index_var` of an enclosing `map` is bound as "
                f"`${{{{ map.{name} }}}}`"
            )
    # secrets.* is always allowed (the indirection is the point).
    return None
