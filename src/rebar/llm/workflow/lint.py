"""The workflow lint collector: secret scan + prompt refs + the one-pass entry (WS-B2).

The frame reference-integrity linter (reference checks, the expression allow-list,
the injection guard) lives in :mod:`rebar.llm.workflow.lint_refs`; this module adds
the remaining concerns and the public one-pass entry point:

* **Secret scan** — a gitleaks-style sweep of the raw file for embedded credentials
  (private keys, cloud/GitHub/Slack tokens) and a precise check for secret-named
  fields assigned a literal; both demand ``${{ secrets.NAME }}`` / ``${env:VAR}``
  indirection instead.
* **Prompt-ref validation** (opt-in) — every agent step's ``prompt:`` resolves to a
  real reviewer / ``.rebar/prompts/<id>.md`` file and its required vars are supplied.

``lint_workflow`` is the one-pass collector ``rebar workflow validate`` calls — it
parses, migrates, schema-validates, frame-lints, and secret-scans in one pass and
returns EVERY finding, never just the first. ``LintFinding`` / ``lint_document`` are
re-exported here so ``rebar.llm.workflow.lint`` stays the stable import surface.
"""

from __future__ import annotations

import re
from typing import Any

from rebar.llm.errors import WorkflowParseError, WorkflowVersionError

from .lint_refs import LintFinding, _iter_all_steps, lint_document
from .migrate import migrate_to_current
from .schema import parse_workflow, step_kind, validate_document

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


# ── Secret-named literal fields (precise, parsed-doc) ─────────────────────────


def _scan_secret_literals(doc: dict[str, Any]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for s in _iter_all_steps(doc):
        sid = s.get("id", "?")
        with_block = s.get("with")
        if not isinstance(with_block, dict):
            continue

        def visit(value: Any, path: str, _sid=sid) -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    keypath = f"{path}.{k}"
                    # The risk this catches is a LITERAL secret committed to git. Any
                    # ${{ }} / ${env:} value means the secret is NOT literal (it's
                    # indirected — secrets.*, env, an input, or an upstream output),
                    # so it is suppressed; only a bare literal in a credential-named
                    # field is flagged. (Reference integrity separately validates the
                    # expression itself.)
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


# Variables the agent runner always supplies to a prompt (RunnerAgentStep); a
# step needn't declare these in `with`.
_ENGINE_PROVIDED_VARS = frozenset({"ticket_id", "ticket_context", "repo_path"})


def lint_prompt_refs(doc: dict[str, Any], *, repo_root=None) -> list[LintFinding]:
    """Validate every agent step's ``prompt:`` ref (WS-F2): it must resolve to a real
    prompt (a catalog reviewer or a ``.rebar/prompts/<id>.md`` file), AND the step's
    inputs must satisfy the prompt's declared required-variable schema. Imports the
    (stdlib-only) prompt registry lazily so the core linter stays uncoupled."""
    from rebar.llm.prompts import get_prompt, load_catalog, prompt_input_schema, prompt_ref_exists

    findings: list[LintFinding] = []
    catalog = load_catalog()
    for step in _iter_all_steps(doc):
        if step_kind(step) != "agent":
            continue
        prompt_id = step.get("prompt")
        if not isinstance(prompt_id, str):
            continue
        loc = f"steps[{step.get('id', '?')}].prompt"
        if not prompt_ref_exists(prompt_id, repo_root=repo_root):
            findings.append(
                LintFinding(
                    loc,
                    f"prompt {prompt_id!r} does not resolve to a known reviewer or a "
                    f".rebar/prompts/{prompt_id}.md file",
                )
            )
            continue
        # Schema satisfaction: the prompt's required vars must be engine-provided or
        # supplied via the step's `with`. (Checked for catalog reviewers, whose schema
        # we can resolve; user-file prompts are existence-checked above.)
        if prompt_id in catalog:
            try:
                prompt = get_prompt(prompt_id, repo_root=repo_root)
                schema = prompt_input_schema(prompt, repo_root=repo_root)
            except Exception:  # a malformed prompt is the prompt's own lint, not this
                continue
            available = _ENGINE_PROVIDED_VARS | set((step.get("with") or {}).keys())
            missing = sorted(set(schema.get("required", [])) - available)
            if missing:
                findings.append(
                    LintFinding(
                        loc,
                        f"prompt {prompt_id!r} requires input(s) {missing} that the step "
                        f"neither supplies via `with` nor receives from the engine",
                    )
                )
    return findings


def lint_workflow(
    text: str,
    *,
    source: str = "<workflow>",
    expressions: bool = True,
    check_prompts: bool = False,
    repo_root=None,
) -> list[LintFinding]:
    """Parse, migrate, schema-validate, semantically lint, and secret-scan ``text``,
    returning EVERY finding in one pass (empty == clean). This is the function
    ``rebar workflow validate`` / ``--dry-run`` build on.

    ``check_prompts`` additionally validates agent ``prompt:`` refs (WS-F2) against
    the reviewer catalog + ``.rebar/prompts/`` — opt-in so the broad callers (which
    don't know about prompts) are unaffected. A hard parse/upgrade failure
    short-circuits (you cannot lint what will not load) and is one error finding.
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
    if check_prompts:
        findings.extend(lint_prompt_refs(doc, repo_root=repo_root))
    return findings


def lint_passes(findings: list[LintFinding]) -> bool:
    """True if no error-severity finding is present (warnings do not block)."""
    return not any(f.severity == "error" for f in findings)


__all__ = [
    "LintFinding",
    "lint_workflow",
    "lint_document",
    "secret_scan",
    "lint_prompt_refs",
    "lint_passes",
    "step_kind",
]
