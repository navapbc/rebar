"""Static safety checks for the workflow DSL, beyond JSON Schema (WS-B2).

The JSON Schema (``rebar.llm.workflow.schema``) proves a workflow file is shaped
right; this linter proves it is *safe and coherent* before anything runs:

* **Reference integrity** — every ``${{ inputs.X }}`` resolves to a declared
  input; every ``${{ steps.Y.outputs.Z }}`` resolves to a step that actually runs
  upstream (Y is a transitive ``needs`` ancestor, not itself, not a stranger);
  ``needs`` edges point at real steps; the graph is acyclic; the DAG converges to
  exactly one terminal step. Findings are located by step + field.

* **Closed expression allow-list** — the ONLY expressions permitted inside
  ``${{ … }}`` are ``inputs.<name>``, ``steps.<id>.outputs.<name>``, and
  ``secrets.<name>``. Anything else is rejected (no arbitrary code, no attribute
  walks). An ``expressions=off`` kill-switch rejects *every* expression for a
  locked-down deployment.

* **Injection guard** — raw ``${{ … }}`` is forbidden in identifier/body fields
  (``id``/``uses``/``prompt``/``model``/``output_schema``); expressions belong in
  ``with:`` values (and the ``if`` guard), passed by name. After substituting
  placeholders we re-validate against the schema and assert the rendered document
  introduced no new keys or steps — the Argo lesson that a templated value must
  never expand into structure.

* **Secret scan** — a gitleaks-style sweep of the raw file for embedded
  credentials (private keys, cloud/GitHub/Slack tokens) and a precise check for
  secret-named fields assigned a literal; both demand ``${{ secrets.NAME }}`` /
  ``${env:VAR}`` indirection instead.

This module is pure stdlib (``re`` + ``graphlib``); ``lint_workflow`` is the
one-pass collector ``rebar workflow validate`` calls — it returns EVERY finding,
never just the first.
"""

from __future__ import annotations

import graphlib
import re
from dataclasses import dataclass
from typing import Any

from rebar.llm.errors import WorkflowParseError, WorkflowVersionError

from .migrate import migrate_to_current
from .schema import parse_workflow, step_kind, validate_document

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
)

# Fields whose value is an identifier or a body sent verbatim to an LLM/shell —
# a raw expression here is an injection vector, so it is forbidden (pass via with:).
_LITERAL_ONLY_FIELDS = ("id", "type", "uses", "prompt", "model", "output_schema")

# A placeholder the substitution pass swaps in for every expression; chosen to be a
# harmless scalar so re-validation exercises structure, not content.
_PLACEHOLDER = "__rebar_subst__"


# ── Secret scanning ───────────────────────────────────────────────────────────

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Stripe live key", re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Slack webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
)

# Field names that look like they hold a credential; a LITERAL value here (not an
# expression / env indirection) must be flagged.
_SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)"
)


def _redact(s: str) -> str:
    s = s.strip()
    if len(s) <= 8:
        return "***"
    return f"{s[:3]}…{s[-3:]}"


def secret_scan(text: str, *, source: str = "<workflow>") -> list[LintFinding]:
    """Gitleaks-style raw-text sweep for embedded credentials (line-located).

    Operates on the unparsed bytes so a secret in a comment is caught too. Only
    high-confidence token *shapes* are matched here to keep false positives near
    zero; the literal-in-a-secret-field check (precise, parsed-doc) lives in
    :func:`lint_document`.
    """
    findings: list[LintFinding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pat in _SECRET_PATTERNS:
            m = pat.search(line)
            if m:
                findings.append(
                    LintFinding(
                        f"{source}:{lineno}",
                        f"possible {label} embedded in the workflow file "
                        f"({_redact(m.group(0))}); store it outside git and reference it "
                        f"with ${{{{ secrets.NAME }}}} or ${{env:VAR}}",
                    )
                )
    return findings


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


def _validate_expression(
    inner: str,
    *,
    step_id: str,
    inputs: set[str],
    steps: dict[str, Any],
    ancestors: set[str],
) -> str | None:
    """Validate one ``${{ … }}`` body against the allow-list + reference integrity.

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
            f"inputs.<name>, steps.<id>.outputs.<name>, secrets.<name>)"
        )
    if kind == "input":
        name = match.group(1)
        if name not in inputs:
            return f"references undeclared workflow input {name!r}"
    elif kind == "step_output":
        ref = match.group(1)
        if ref == step_id:
            return f"references its own output (steps.{ref}.outputs.*)"
        if ref not in steps:
            return f"references unknown step {ref!r}"
        if ref not in ancestors:
            return (
                f"references output of step {ref!r}, which is not an upstream "
                f"dependency — add {ref!r} to this step's `needs`"
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


def _check_step(
    step: dict[str, Any],
    *,
    step_id: str,
    inputs: set[str],
    steps: dict[str, Any],
    ancestors: set[str],
    expressions_on: bool,
    findings: list[LintFinding],
) -> None:
    base = f"steps[{step_id}]"

    # 1. Injection guard: no raw expressions in identifier/body fields.
    for field in _LITERAL_ONLY_FIELDS:
        val = step.get(field)
        if isinstance(val, str) and ("${{" in val or "${env:" in val):
            findings.append(
                LintFinding(
                    f"{base}.{field}",
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
            err = _validate_expression(
                m.group(1), step_id=step_id, inputs=inputs, steps=steps, ancestors=ancestors
            )
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
        _walk_with(with_block, f"{base}.with", check_string, on_key_expr)

    guard = step.get("if")
    if isinstance(guard, str):
        check_string(guard, f"{base}.if")


def lint_document(
    doc: dict[str, Any], *, source: str = "<workflow>", expressions: bool = True
) -> list[LintFinding]:
    """Semantic lint of a parsed + migrated document (no raw text / secrets here).

    Reference integrity, the expression allow-list, the injection guard, the
    acyclic-one-terminal DAG shape, and the post-substitution structure-invariance
    re-validation. Tolerant of a not-fully-schema-valid document (collects what it
    can) so a single run surfaces both schema and semantic problems.
    """
    findings: list[LintFinding] = []
    steps_list = doc.get("steps")
    if not isinstance(steps_list, list):
        return findings  # schema layer already flagged this

    by_id: dict[str, Any] = {}
    for i, s in enumerate(steps_list):
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if isinstance(sid, str) and sid:
            if sid in by_id:
                findings.append(LintFinding(f"steps[{i}]", f"duplicate step id {sid!r}"))
            else:
                by_id[sid] = s

    inputs = set((doc.get("inputs") or {}).keys()) if isinstance(doc.get("inputs"), dict) else set()

    # Build the needs graph (only over known step ids); flag bad needs edges.
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
                        LintFinding(f"steps[{sid}].needs", f"step {sid!r} cannot depend on itself")
                    )
                elif n not in by_id:
                    findings.append(
                        LintFinding(f"steps[{sid}].needs", f"unknown step {n!r} in `needs`")
                    )
                else:
                    edges.append(n)
        graph[sid] = edges

    ancestors = _ancestors(graph)
    if ancestors is None:
        findings.append(LintFinding("steps", "dependency cycle detected among `needs` edges"))
        ancestors = {sid: set() for sid in by_id}
    else:
        # Exactly one terminal (sink) step — the workflow's converging result.
        depended: set[str] = set()
        for edges in graph.values():
            depended.update(edges)
        sinks = sorted(sid for sid in by_id if sid not in depended)
        if len(sinks) > 1:
            findings.append(
                LintFinding(
                    "steps",
                    f"a workflow must converge to exactly one terminal step; found "
                    f"{len(sinks)}: {', '.join(sinks)} (add `needs` so they feed a single sink)",
                )
            )
        elif not sinks and by_id:
            findings.append(LintFinding("steps", "no terminal step found (a cycle?)"))

    for sid, s in by_id.items():
        _check_step(
            s,
            step_id=sid,
            inputs=inputs,
            steps=by_id,
            ancestors=ancestors.get(sid, set()),
            expressions_on=expressions,
            findings=findings,
        )

    findings.extend(_post_substitution_check(doc, source=source))
    return findings


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
    ids = {
        s["id"]
        for s in doc.get("steps", [])
        if isinstance(s, dict) and isinstance(s.get("id"), str)
    }
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


# ── Secret-named literal fields (precise, parsed-doc) ─────────────────────────


def _scan_secret_literals(doc: dict[str, Any]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    steps = doc.get("steps")
    if not isinstance(steps, list):
        return findings
    for s in steps:
        if not isinstance(s, dict):
            continue
        sid = s.get("id", "?")
        with_block = s.get("with")
        if not isinstance(with_block, dict):
            continue

        def visit(value: Any, path: str, _sid=sid) -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    keypath = f"{path}.{k}"
                    if (
                        isinstance(k, str)
                        and _SECRET_KEY_RE.search(k)
                        and isinstance(v, str)
                        and v.strip()
                        and "${{" not in v
                        and "${env:" not in v
                    ):
                        findings.append(
                            LintFinding(
                                keypath,
                                f"field {k!r} looks like a credential but holds a literal "
                                f"value ({_redact(v)}); use ${{{{ secrets.NAME }}}} or "
                                f"${{env:VAR}} indirection",
                            )
                        )
                    visit(v, keypath)
            elif isinstance(value, list):
                for i, v in enumerate(value):
                    visit(v, f"{path}[{i}]")

        visit(with_block, f"steps[{sid}].with")
    return findings


# ── The one-pass collector ────────────────────────────────────────────────────


def lint_workflow(
    text: str, *, source: str = "<workflow>", expressions: bool = True
) -> list[LintFinding]:
    """Parse, migrate, schema-validate, semantically lint, and secret-scan ``text``,
    returning EVERY finding in one pass (empty == clean). This is the function
    ``rebar workflow validate`` / ``--dry-run`` build on.

    A hard parse/upgrade failure short-circuits (you cannot lint what will not
    load) and is returned as a single error finding.
    """
    try:
        doc = parse_workflow(text, source=source)
    except WorkflowParseError as exc:
        return [LintFinding(source, str(exc).split(": ", 1)[-1] if ": " in str(exc) else str(exc))]
    try:
        doc = migrate_to_current(doc, source=source)
    except (WorkflowVersionError, WorkflowParseError) as exc:
        return [LintFinding(source, str(exc))]

    findings: list[LintFinding] = []
    for msg in validate_document(doc, source=source):
        # The "note: full JSON Schema validation skipped" line (degraded path when
        # jsonschema is absent in a lean install) is informational — a WARNING, not
        # a blocking error, so the lean core still validates structurally + passes.
        severity = "warning" if msg.startswith("note:") else "error"
        findings.append(LintFinding(source, msg, severity))
    findings.extend(lint_document(doc, source=source, expressions=expressions))
    findings.extend(_scan_secret_literals(doc))
    findings.extend(secret_scan(text, source=source))
    return findings


def lint_passes(findings: list[LintFinding]) -> bool:
    """True if no error-severity finding is present (warnings do not block)."""
    return not any(f.severity == "error" for f in findings)


__all__ = [
    "LintFinding",
    "lint_workflow",
    "lint_document",
    "secret_scan",
    "lint_passes",
    "step_kind",
]
