"""Frame reference-integrity linting for the workflow DSL (the heart of WS-B2).

Given a parsed + migrated workflow document, this module proves it is *coherent*:

* **Frame-scoped reference integrity** — every ``${{ inputs.X }}`` resolves to a
  declared input; every ``${{ steps.Y.outputs.Z }}`` resolves to a step that runs
  upstream IN THE SAME FRAME (or an enclosing one) — a ``needs`` edge or output
  reference may not cross a frame boundary; each frame's ``needs`` graph is acyclic
  (iteration is expressed ONLY by a declared ``loop`` frame, never a back-edge) and
  the top frame converges to exactly one terminal step.
* **Closed expression allow-list** — the ONLY expressions permitted inside
  ``${{ … }}`` are ``inputs.<name>``, ``steps.<id>.outputs.<name>``,
  ``secrets.<name>``, and the v2 frame bindings ``loop.<var>`` / ``map.<as>``.
* **Injection guard** — raw ``${{ … }}`` is forbidden in identifier/body fields, and
  after placeholder substitution the rendered document must introduce no new key or
  step (the Argo lesson that a templated value must never expand into structure).

:func:`lint_document` walks the document FRAME BY FRAME (top steps + every nested
branch/loop/map body) and returns EVERY finding. Pure stdlib (``re`` + ``graphlib``).
The raw-text secret scan and the one-pass collector live in
:mod:`rebar.llm.workflow.lint`, which builds on this.
"""

from __future__ import annotations

import graphlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from rebar.llm.errors import WorkflowVersionError

from .schema import CONTROL_KINDS, step_kind, validate_document

# ── Findings ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LintFinding:
    """One located, actionable problem with a workflow file."""

    location: str
    message: str
    severity: str = "error"  # "error" | "warning"

    def __str__(self) -> str:
        return f"[{self.severity}] {self.location}: {self.message}"


# ── Expression grammar (the closed allow-list) ────────────────────────────────

# A ${{ … }} occurrence. Non-greedy so adjacent expressions don't merge.
_EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
# The ${env:VAR} literal env-indirection token (a different delimiter, allowed in
# value position as a non-secret-leaking reference to an environment variable).
_ENV_RE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")

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

# Fields whose value is an identifier or a body sent verbatim to an LLM/shell —
# a raw expression here is an injection vector, so it is forbidden (pass via with:).
_LITERAL_ONLY_FIELDS = ("id", "type", "uses", "prompt", "model", "output_schema")

# The engine-injected `${{ inputs.* }}` namespace (workflow authoring v2, 5e78): vars
# the executor seeds into every run (target ticket + its rendered context + repo root),
# valid to reference though NOT declared in `inputs:`. SINGLE SOURCE OF TRUTH — the
# linter allow-lists these and the engine seeds exactly these; never hard-code inline.
ENGINE_INJECTED_INPUTS: frozenset[str] = frozenset({"ticket_id", "ticket_context", "repo_path"})

# A placeholder the substitution pass swaps in for every expression; chosen to be a
# harmless scalar so re-validation exercises structure, not content.
_PLACEHOLDER = "__rebar_subst__"


# ── Graph helpers ─────────────────────────────────────────────────────────────


def _ancestors(graph: dict[str, list[str]]) -> dict[str, set[str]] | None:
    """Transitive ``needs`` ancestors per node, or ``None`` if the graph cycles."""
    try:
        order = list(graphlib.TopologicalSorter(graph).static_order())
    except graphlib.CycleError:
        return None
    anc: dict[str, set[str]] = {}
    for node in order:
        acc: set[str] = set()
        for dep in graph.get(node, []):
            if dep in graph:  # ignore dangling deps (reported separately)
                acc.add(dep)
                acc |= anc.get(dep, set())
        anc[node] = acc
    return anc


# ── Expression validation ─────────────────────────────────────────────────────


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
    output_fields: Mapping[str, frozenset[str] | None]


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
    if kind is None:
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
        if declared is not None and field_name not in declared:
            return (
                f"references output {field_name!r} not produced by step {ref!r} — its "
                f"declared outputs are {{{', '.join(sorted(declared))}}}"
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


def _walk_with(value: Any, path: str, on_string, on_key_expr) -> None:
    """Recurse a ``with`` value, invoking callbacks on strings and on any
    expression that appears in a mapping KEY position (the structure-injection
    guard)."""
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and ("${{" in k or "${env:" in k):
                on_key_expr(f"{path}[{k!r}]")
            _walk_with(v, f"{path}.{k}", on_string, on_key_expr)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _walk_with(v, f"{path}[{i}]", on_string, on_key_expr)
    elif isinstance(value, str):
        on_string(value, path)


def _shape_kind(step: dict[str, Any]) -> str | None:
    """The kind implied by the step's actual discriminator KEY (ignoring an explicit
    ``type``), or None if it has no/ambiguous discriminator (schema flags that)."""
    for k in ("branch", "loop", "map"):
        if k in step:
            return k
    if "prompt" in step:
        return "agent"
    if "uses" in step:
        return "scripted"
    return None


# The discriminator key that names each shape, for a legible type-mismatch message.
_DISC_KEY = {
    "scripted": "uses",
    "agent": "prompt",
    "branch": "branch",
    "loop": "loop",
    "map": "map",
}


def _check_condition(
    value: Any,
    *,
    loc: str,
    step_id: str,
    scope: _Scope,
    expressions_on: bool,
    findings: list[LintFinding],
    forbid_secrets: bool,
) -> None:
    """Validate a control-flow condition field (``branch.when`` / ``loop.while`` /
    ``loop.until`` / ``map.over``): it must be a real ``${{ … }}`` expression (a bare
    literal is the always-truthy footgun), may not reference a secret when
    ``forbid_secrets`` (control flow must not branch on a credential), and every
    embedded expression must resolve in ``scope``."""
    if not isinstance(value, str):
        return  # a missing/non-string required field is the schema layer's finding
    if "${{" not in value:
        findings.append(
            LintFinding(
                loc,
                "must be a `${{ … }}` expression — a bare value is treated as a "
                "literal string and is always truthy",
            )
        )
        return
    if forbid_secrets and "secrets." in value:
        findings.append(
            LintFinding(
                loc,
                "secrets may not be referenced in a control-flow condition — control "
                "flow must not depend on a credential",
            )
        )
        return
    for m in _EXPR_RE.finditer(value):
        if not expressions_on:
            findings.append(
                LintFinding(
                    loc,
                    "expressions are disabled (expressions=off) but the workflow "
                    f"uses ${{{{{m.group(1).strip()}}}}}",
                )
            )
            continue
        err = _validate_expression(m.group(1), step_id=step_id, scope=scope)
        if err:
            findings.append(LintFinding(loc, err))


def _check_step(
    step: dict[str, Any],
    *,
    step_id: str,
    loc: str,
    scope: _Scope,
    expressions_on: bool,
    findings: list[LintFinding],
) -> None:
    """Lint the NON-recursive parts of one step (any frame): the ``type``
    discriminator, agent-only fields, the injection guard, and the expressions in
    ``with`` / ``if``. A control step's condition + nested frames are linted by
    :func:`_lint_control` (which owns the child scope)."""
    kind = step_kind(step)

    # 0a. Explicit `type` discriminator must agree with the actual shape, or the
    # executor's step_kind (which honors an explicit `type`) would dispatch wrong.
    declared_type = step.get("type")
    shape = _shape_kind(step)
    if declared_type is not None and shape is not None and declared_type != shape:
        findings.append(
            LintFinding(
                f"{loc}.type",
                f"`type: {declared_type}` but the step has a {shape!r} shape "
                f"(`{_DISC_KEY[shape]}:`)",
            )
        )

    # 0b. Agent-only fields on a scripted step are silently ignored at run time —
    # flag them rather than let the author believe they take effect. (Control steps
    # structurally cannot carry these — the schema's oneOf forbids them.)
    if kind == "scripted":
        for agent_field in ("output_schema", "mode", "model"):
            if agent_field in step:
                findings.append(
                    LintFinding(
                        f"{loc}.{agent_field}",
                        f"`{agent_field}` only applies to an agent step (`prompt:`); "
                        f"it is ignored on a scripted (`uses:`) step",
                    )
                )

    # 1. Injection guard: no raw expressions in identifier/body fields.
    for field in _LITERAL_ONLY_FIELDS:
        val = step.get(field)
        if isinstance(val, str) and ("${{" in val or "${env:" in val):
            findings.append(
                LintFinding(
                    f"{loc}.{field}",
                    f"raw expression not allowed in `{field}` — pass values through "
                    f"`with:` and reference them by name",
                )
            )

    # 2. Expression-bearing positions: `with` values and the `if` guard.
    def check_string(s: str, path: str) -> None:
        for m in _EXPR_RE.finditer(s):
            if not expressions_on:
                findings.append(
                    LintFinding(
                        path,
                        "expressions are disabled (expressions=off) but the workflow "
                        f"uses ${{{{{m.group(1).strip()}}}}}",
                    )
                )
                continue
            err = _validate_expression(m.group(1), step_id=step_id, scope=scope)
            if err:
                findings.append(LintFinding(path, err))

    def on_key_expr(path: str) -> None:
        findings.append(
            LintFinding(
                path,
                "an expression may not appear in a mapping key (it must not expand "
                "into document structure — the Argo lesson)",
            )
        )

    with_block = step.get("with")
    if isinstance(with_block, dict):
        _walk_with(with_block, f"{loc}.with", check_string, on_key_expr)

    guard = step.get("if")
    if isinstance(guard, str):
        # A bare `if:` with no `${{ … }}` resolves to its literal string and is
        # silently truthy (the GHA `if: steps.a.outputs.ok` footgun) — require an
        # explicit expression so the guard's semantics are unambiguous.
        if "${{" not in guard:
            findings.append(
                LintFinding(
                    f"{loc}.if",
                    "`if:` must be a `${{ … }}` expression — a bare value is "
                    "treated as a literal string and is always truthy",
                )
            )
        # A run-control decision must not branch on a credential's presence (and
        # would risk persisting the secret into run state); secrets belong only in
        # `with:` values passed to a step that needs them.
        elif "secrets." in guard:
            findings.append(
                LintFinding(
                    f"{loc}.if",
                    "secrets may not be referenced in an `if:` guard — control "
                    "flow must not depend on a credential",
                )
            )
        else:
            check_string(guard, f"{loc}.if")


def _lint_control(
    step: dict[str, Any],
    kind: str,
    step_id: str,
    *,
    loc: str,
    scope: _Scope,
    child_outer_ids: frozenset[str],
    inputs: frozenset[str],
    output_fields: Mapping[str, frozenset[str] | None],
    expressions_on: bool,
    findings: list[LintFinding],
) -> None:
    """Lint a control step's condition(s) and recurse into its nested frame(s).

    ``scope`` is the control step's OWN scope (used for ``when`` / ``until`` /
    ``over``). ``child_outer_ids`` is what the nested frame sees from outside —
    the enclosing visible ids plus this step's same-frame ancestors. Each construct
    extends the binding scope of its body (loop adds its ``var``; map adds its
    ``as``/``index_var``); branch adds nothing.
    """
    if kind == "branch":
        branch = step.get("branch") or {}
        _check_condition(
            branch.get("when"),
            loc=f"{loc}.branch.when",
            step_id=step_id,
            scope=scope,
            expressions_on=expressions_on,
            findings=findings,
            forbid_secrets=True,
        )
        for arm in ("then", "else"):
            arm_body = branch.get(arm)
            if isinstance(arm_body, list):
                _lint_frame(
                    arm_body,
                    findings=findings,
                    frame_loc=f"{loc}.branch.{arm}",
                    inputs=inputs,
                    outer_ids=child_outer_ids,
                    loop_vars=scope.loop_vars,
                    map_binds=scope.map_binds,
                    output_fields=output_fields,
                    expressions_on=expressions_on,
                    is_top=False,
                )
    elif kind == "loop":
        loop = step.get("loop") or {}
        var = loop.get("var") if isinstance(loop.get("var"), str) else "index"
        body = loop.get("body")
        body_ids = _frame_ids(body)
        # The loop condition may reference its OWN body's outputs (the prev-iteration
        # pattern, POC-proven), so the body ids are visible to while/until ONLY (added
        # to `outer`, not `siblings`, so they never gain same-frame `needs` semantics),
        # and the loop var is bound.
        cond_scope = replace(
            scope,
            outer=scope.outer | body_ids,
            loop_vars=scope.loop_vars | {var},
        )
        for cond_key in ("while", "until"):
            if cond_key in loop:
                _check_condition(
                    loop.get(cond_key),
                    loc=f"{loc}.loop.{cond_key}",
                    step_id=step_id,
                    scope=cond_scope,
                    expressions_on=expressions_on,
                    findings=findings,
                    forbid_secrets=True,
                )
        if isinstance(body, list):
            _lint_frame(
                body,
                findings=findings,
                frame_loc=f"{loc}.loop.body",
                inputs=inputs,
                outer_ids=child_outer_ids,
                loop_vars=scope.loop_vars | {var},
                map_binds=scope.map_binds,
                output_fields=output_fields,
                expressions_on=expressions_on,
                is_top=False,
            )
    elif kind == "map":
        mp = step.get("map") or {}
        # `over` resolves in the control step's OWN scope (the collection must exist
        # before fan-out) — it can be a collection but is validated like any expr.
        _check_condition(
            mp.get("over"),
            loc=f"{loc}.map.over",
            step_id=step_id,
            scope=scope,
            expressions_on=expressions_on,
            findings=findings,
            forbid_secrets=False,
        )
        binds = {b for b in (mp.get("as"), mp.get("index_var")) if isinstance(b, str)}
        body = mp.get("body")
        if isinstance(body, list):
            _lint_frame(
                body,
                findings=findings,
                frame_loc=f"{loc}.map.body",
                inputs=inputs,
                outer_ids=child_outer_ids,
                loop_vars=scope.loop_vars,
                map_binds=scope.map_binds | binds,
                output_fields=output_fields,
                expressions_on=expressions_on,
                is_top=False,
            )


def _output_fields_map(doc: dict[str, Any]) -> dict[str, frozenset[str] | None]:
    """Step id → declared OUTPUT field names (or ``None`` for a producer with no known
    contract), built once per lint and shared across frames so a
    `${{ steps.<id>.outputs.<name> }}` ref can be checked NAME-EXISTENCE. ``None`` is
    "skip" — never a false error (5e78: an unannotated producer is UNKNOWN). Only
    scripted (`uses`) steps carry a contract in this slice; agent/control → UNKNOWN."""
    # Importing the step library registers its contracts (decorators run on import); a
    # bare lint may not have triggered that. Lazy + best-effort → else all UNKNOWN.
    try:
        from rebar import schemas

        from . import steps  # noqa: F401  (side effect: register built-in contracts)
        from .executor import contract_for
    except Exception:  # noqa: BLE001
        return {}

    def fields(uses: str) -> frozenset[str] | None:
        try:
            contract = contract_for(uses)
            if contract is None or not contract.output_schema:
                return None
            props = schemas.load(contract.output_schema).get("properties")
            return frozenset(props.keys()) if isinstance(props, dict) else None
        except Exception:  # noqa: BLE001 - any resolution failure is UNKNOWN
            return None

    out: dict[str, frozenset[str] | None] = {}
    for s in _iter_all_steps(doc):
        sid, uses = s.get("id"), s.get("uses")
        if isinstance(sid, str) and sid:
            out[sid] = fields(uses) if isinstance(uses, str) else None
    return out


def _frame_ids(steps_list: Any) -> frozenset[str]:
    """The set of valid string ids directly in a frame's step list (non-recursive)."""
    if not isinstance(steps_list, list):
        return frozenset()
    return frozenset(
        s["id"]
        for s in steps_list
        if isinstance(s, dict) and isinstance(s.get("id"), str) and s["id"]
    )


def _lint_frame(
    steps_list: list[Any],
    *,
    findings: list[LintFinding],
    frame_loc: str,
    inputs: frozenset[str],
    outer_ids: frozenset[str],
    loop_vars: frozenset[str],
    map_binds: frozenset[str],
    output_fields: Mapping[str, frozenset[str] | None],
    expressions_on: bool,
    is_top: bool,
) -> None:
    """Lint ONE frame (a step list sharing a ``needs`` DAG) and recurse into the
    nested frames of any control step.

    A frame is self-contained: ``needs`` edges resolve only within the frame (an
    edge to a non-sibling id is "unknown step" — edges may not cross a frame
    boundary), the per-frame graph must be acyclic (iteration is expressed ONLY by a
    declared ``loop`` frame), and the TOP frame must converge to one terminal step
    (preserving the v1 contract; nested frames may have several independent sinks).
    """
    by_id: dict[str, Any] = {}
    for i, s in enumerate(steps_list):
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if isinstance(sid, str) and sid:
            if sid in by_id:
                findings.append(LintFinding(f"{frame_loc}[{i}]", f"duplicate step id {sid!r}"))
            else:
                by_id[sid] = s

    # Build the needs graph (only over this frame's ids); flag bad needs edges.
    graph: dict[str, list[str]] = {}
    for sid, s in by_id.items():
        needs = s.get("needs") or []
        edges: list[str] = []
        if isinstance(needs, list):
            for n in needs:
                if not isinstance(n, str):
                    continue
                if n == sid:
                    findings.append(
                        LintFinding(
                            f"{frame_loc}[{sid}].needs", f"step {sid!r} cannot depend on itself"
                        )
                    )
                elif n not in by_id:
                    findings.append(
                        LintFinding(
                            f"{frame_loc}[{sid}].needs",
                            f"unknown step {n!r} in `needs` (a `needs` edge may not cross "
                            f"a frame boundary)",
                        )
                    )
                else:
                    edges.append(n)
        graph[sid] = edges

    ancestors = _ancestors(graph)
    if ancestors is None:
        findings.append(
            LintFinding(
                frame_loc,
                "dependency cycle detected among `needs` edges (iteration must use a "
                "declared `loop` frame, not a `needs` back-edge)",
            )
        )
        ancestors = {sid: set() for sid in by_id}
    elif is_top:
        # Exactly one terminal (sink) step at the top — the workflow's converging
        # result. Nested frames may legitimately have multiple independent sinks.
        depended: set[str] = set()
        for edges in graph.values():
            depended.update(edges)
        sinks = sorted(sid for sid in by_id if sid not in depended)
        if len(sinks) > 1:
            findings.append(
                LintFinding(
                    frame_loc,
                    f"a workflow must converge to exactly one terminal step; found "
                    f"{len(sinks)}: {', '.join(sinks)} (add `needs` so they feed a single sink)",
                )
            )
        elif not sinks and by_id:
            findings.append(LintFinding(frame_loc, "no terminal step found (a cycle?)"))

    frame_ids = frozenset(by_id)
    for sid, s in by_id.items():
        scope = _Scope(
            inputs=inputs,
            siblings=frame_ids,
            frame_upstream=frozenset(ancestors.get(sid, set())),
            outer=outer_ids,
            loop_vars=loop_vars,
            map_binds=map_binds,
            output_fields=output_fields,
        )
        _check_step(
            s,
            step_id=sid,
            loc=f"{frame_loc}[{sid}]",
            scope=scope,
            expressions_on=expressions_on,
            findings=findings,
        )
        kind = step_kind(s)
        if kind in CONTROL_KINDS:
            _lint_control(
                s,
                kind,
                sid,
                loc=f"{frame_loc}[{sid}]",
                scope=scope,
                # The nested frame sees the enclosing visible ids PLUS this control
                # step's same-frame ancestors (all guaranteed-before the frame runs).
                child_outer_ids=outer_ids | frozenset(ancestors.get(sid, set())),
                inputs=inputs,
                output_fields=output_fields,
                expressions_on=expressions_on,
                findings=findings,
            )


def lint_document(
    doc: dict[str, Any], *, source: str = "<workflow>", expressions: bool = True
) -> list[LintFinding]:
    """Semantic lint of a parsed + migrated document (no raw text / secrets here).

    Walks the document FRAME BY FRAME (top steps + every nested branch/loop/map
    body): frame-scoped reference integrity, the expression allow-list (incl. the
    ``loop.<var>`` / ``map.<as>`` bindings), the injection guard, per-frame acyclic
    ``needs`` (loops are the only legal cycle) with one top-level terminal, and the
    post-substitution structure-invariance re-validation. Tolerant of a
    not-fully-schema-valid document (collects what it can) so a single run surfaces
    both schema and semantic problems.
    """
    findings: list[LintFinding] = []
    steps_list = doc.get("steps")
    if not isinstance(steps_list, list):
        return findings  # schema layer already flagged this

    inputs = (
        frozenset((doc.get("inputs") or {}).keys())
        if isinstance(doc.get("inputs"), dict)
        else frozenset()
    )
    _lint_frame(
        steps_list,
        findings=findings,
        frame_loc="steps",
        inputs=inputs,
        outer_ids=frozenset(),
        loop_vars=frozenset(),
        map_binds=frozenset(),
        output_fields=_output_fields_map(doc),
        expressions_on=expressions,
        is_top=True,
    )
    findings.extend(_post_substitution_check(doc, source=source))
    return findings


def _iter_all_steps(doc: dict[str, Any]):
    """Yield every step in the document, recursing into the nested frames of
    branch/loop/map (so scanners that care about *all* steps — secret literals,
    prompt refs, the structure check — see the whole tree, not just the top frame).

    Yields each step dict in document order, parents before children.
    """

    def walk(steps_list: Any):
        if not isinstance(steps_list, list):
            return
        for s in steps_list:
            if not isinstance(s, dict):
                continue
            yield s
            branch = s.get("branch")
            if isinstance(branch, dict):
                yield from walk(branch.get("then"))
                yield from walk(branch.get("else"))
            loop = s.get("loop")
            if isinstance(loop, dict):
                yield from walk(loop.get("body"))
            mp = s.get("map")
            if isinstance(mp, dict):
                yield from walk(mp.get("body"))

    yield from walk(doc.get("steps"))


def _substitute(value: Any) -> Any:
    """Return a copy of ``value`` with every expression / env token replaced by a
    harmless scalar placeholder (used for the structure-invariance re-check)."""
    if isinstance(value, dict):
        return {k: _substitute(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v) for v in value]
    if isinstance(value, str):
        s = _EXPR_RE.sub(_PLACEHOLDER, value)
        s = _ENV_RE.sub(_PLACEHOLDER, s)
        return s
    return value


def _step_ids_and_keys(doc: dict[str, Any]) -> tuple[set[str], set[str]]:
    # Recurse into nested frames so the structure-invariance check also catches a
    # substitution that adds/renames a step inside a branch/loop/map body.
    ids = {s["id"] for s in _iter_all_steps(doc) if isinstance(s.get("id"), str)}
    keys = set(doc.keys())
    return ids, keys


def _post_substitution_check(doc: dict[str, Any], *, source: str) -> list[LintFinding]:
    """Render placeholders for every expression and assert the document's shape is
    unchanged (no new top-level key, no new/renamed step) and still schema-valid —
    the Argo guarantee that a substituted value cannot become structure."""
    findings: list[LintFinding] = []
    rendered = _substitute(doc)
    before_ids, before_keys = _step_ids_and_keys(doc)
    after_ids, after_keys = _step_ids_and_keys(rendered)
    if before_ids != after_ids or before_keys != after_keys:
        findings.append(
            LintFinding(
                source,
                "substituting expressions changed the document's structure "
                "(new key or step) — expressions must only fill scalar values",
            )
        )
    # Re-validate the rendered doc; surface only errors substitution INTRODUCED.
    try:
        new_errs = set(validate_document(rendered, source=source))
        old_errs = set(validate_document(doc, source=source))
    except WorkflowVersionError:
        return findings
    for err in sorted(new_errs - old_errs):
        findings.append(LintFinding(source, f"after substitution: {err}"))
    return findings


__all__ = [
    "LintFinding",
    "lint_document",
]
