"""Reviewer registry — the library of reviewer prompts, each addressed by id.

Two concerns are kept deliberately separate (the converging OSS pattern):

  * **Reviewer identity + selection rules** (id, dimension, file-glob ``applies_to``,
    whether it's a default) live in a versioned, testable local catalog
    (``reviewers/catalog.json``) — so selection is code-reviewed and offline-testable.
  * **Prompt text is GIT-CANONICAL** (epic a88f / WS-F1): the committed prompt file
    is the single source of truth — a packaged ``reviewers/*.md`` (or a user override
    at ``.rebar/prompts/<id>.md``). **Langfuse is NEVER consulted for prompt text**
    (read-replica only); the resolved text's content hash (sha256) is returned so it
    can be embedded in traces and any divergence from a Langfuse copy is detectable.
    Rendering is **strict** — an unsupplied ``{{var}}`` raises, never a silent empty.

Stdlib-only.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from rebar.llm.errors import LLMConfigError

_VAR = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class ReviewerError(LLMConfigError):
    """Raised when a reviewer id is not in the catalog. Subclasses ``LLMConfigError``
    (hence ``LLMError``) so a bad reviewer id surfaces as a clean error across all
    three interfaces rather than an uncaught ``KeyError`` traceback."""


class PromptError(LLMConfigError):
    """A prompt could not be resolved/rendered: strict-undefined variable, a missing
    canonical file, or a declared/used variable parity mismatch (WS-F)."""


@dataclass
class Reviewer:
    id: str
    dimension: str
    title: str = ""
    description: str = ""
    langfuse_prompt: str | None = None  # Langfuse prompt name (defaults to id)
    fallback_file: str | None = None  # packaged *.md used when Langfuse is absent
    default: bool = False  # part of the default reviewer set
    applies_to: list[str] = field(default_factory=list)  # globs for rule-based selection

    @property
    def prompt_name(self) -> str:
        return self.langfuse_prompt or self.id


def _catalog_dir():
    return files(__package__).joinpath("reviewers")


def load_catalog() -> dict[str, Reviewer]:
    """Load the packaged reviewer catalog → {id: Reviewer}."""
    raw = json.loads(_catalog_dir().joinpath("catalog.json").read_text(encoding="utf-8"))
    out: dict[str, Reviewer] = {}
    for rid, spec in raw.items():
        out[rid] = Reviewer(
            id=rid,
            dimension=spec.get("dimension", rid),
            title=spec.get("title", ""),
            description=spec.get("description", ""),
            langfuse_prompt=spec.get("langfuse_prompt"),
            fallback_file=spec.get("fallback_file"),
            default=bool(spec.get("default", False)),
            applies_to=list(spec.get("applies_to", [])),
        )
    return out


def get_reviewer(reviewer_id: str) -> Reviewer:
    catalog = load_catalog()
    try:
        return catalog[reviewer_id]
    except KeyError:
        raise ReviewerError(f"unknown reviewer '{reviewer_id}'; known: {sorted(catalog)}") from None


def packaged_prompt_text(reviewer: Reviewer) -> str:
    """The packaged fallback prompt text for a reviewer (raw, with {{vars}})."""
    if not reviewer.fallback_file:
        return ""
    return _catalog_dir().joinpath(reviewer.fallback_file).read_text(encoding="utf-8")


def template_variables(template: str) -> set[str]:
    """The set of ``{{var}}`` names a template references (for parity checks)."""
    return {m.group(1) for m in _VAR.finditer(template)}


def _render_strict(template: str, variables: dict) -> str:
    """Render ``{{var}}`` placeholders, STRICTLY (WS-F): every referenced variable
    must be supplied — an unsupplied one raises :class:`PromptError` rather than
    silently rendering empty (which would ship a malformed prompt)."""
    missing = sorted(template_variables(template) - set(variables))
    if missing:
        raise PromptError(
            f"prompt references undefined variable(s) {missing}; supplied: {sorted(variables)}"
        )
    return _VAR.sub(lambda m: str(variables[m.group(1)]), template)


# ── front-matter + variant overlays (WS-F2) ──────────────────────────────────

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# Overlay sentinel: a variant body may include its base/parent here. Chosen as an
# HTML comment so it never collides with {{var}} rendering.
_BASE_MARKER = "<!--base-->"


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split an optional YAML front-matter block off a prompt file → ``(meta, body)``.

    A prompt may begin with ``---\\n<yaml>\\n---\\n``; declared keys are
    ``variables`` (the vars the template uses), ``required`` (subset that MUST be
    supplied), and ``variant_of`` (a parent prompt id for overlay chaining). No
    front-matter → ``({}, text)`` (so existing front-matter-less prompts are
    unchanged)."""
    m = _FRONT_MATTER.match(text)
    if not m:
        return {}, text
    import yaml

    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise PromptError(f"invalid prompt front-matter: {exc}") from None
    if not isinstance(meta, dict):
        raise PromptError("prompt front-matter must be a mapping")
    return meta, text[m.end() :]


def _prompt_file(reviewer: Reviewer, repo_root, variant: str | None):
    """The file for a reviewer's base or ``<id>.<variant>`` prompt — a user
    ``.rebar/prompts/`` override wins over the packaged ``reviewers/`` copy."""
    suffix = f".{variant}" if variant else ""
    if repo_root:
        user = Path(repo_root) / ".rebar" / "prompts" / f"{reviewer.id}{suffix}.md"
        try:
            if user.is_file():
                return user.read_text(encoding="utf-8")
        except OSError:
            pass
    if reviewer.fallback_file:
        stem = (
            reviewer.fallback_file[:-3]
            if reviewer.fallback_file.endswith(".md")
            else (reviewer.fallback_file)
        )
        pkg = _catalog_dir().joinpath(f"{stem}{suffix}.md")
        try:
            return pkg.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            pass
    return None


def load_prompt(
    reviewer: Reviewer, *, repo_root=None, variant: str | None = None, _seen: set | None = None
) -> tuple[str, dict]:
    """Resolve a reviewer's prompt to ``(body, meta)`` — front-matter stripped and
    variant overlay applied (WS-F2).

    A variant (``<id>.<variant>.md``) overlays a base: its front-matter ``variant_of``
    names the parent (default: the base), and its body may include the parent via the
    ``<!--base-->`` marker (no marker → it is a full override). The ``variant_of``
    chain is cycle-guarded."""
    _seen = _seen if _seen is not None else set()
    key = variant or "<base>"
    if key in _seen:
        raise PromptError(f"prompt variant cycle detected at {key!r} for {reviewer.id!r}")
    _seen.add(key)

    raw = _prompt_file(reviewer, repo_root, variant)
    if raw is None:
        if variant:
            raise PromptError(f"unknown prompt variant {variant!r} for reviewer {reviewer.id!r}")
        raw = ""  # a reviewer with no fallback_file resolves to empty (legacy behavior)
    meta, body = parse_front_matter(raw)

    if variant:
        parent = meta.get("variant_of")  # None → overlay onto the base
        parent_body, parent_meta = load_prompt(
            reviewer, repo_root=repo_root, variant=parent, _seen=_seen
        )
        if _BASE_MARKER in body:
            body = body.replace(_BASE_MARKER, parent_body)
        merged = {**parent_meta, **meta}
        merged.pop("variant_of", None)
        return body, merged
    return body, meta


def canonical_prompt_text(reviewer: Reviewer, *, repo_root=None, variant: str | None = None) -> str:
    """The GIT-CANONICAL prompt text for a reviewer (WS-F1/F2): the committed prompt
    file's body (front-matter stripped, variant overlay applied). A user
    ``.rebar/prompts/<id>.md`` override wins over the packaged ``reviewers/*.md``;
    Langfuse is NEVER consulted."""
    return load_prompt(reviewer, repo_root=repo_root, variant=variant)[0]


def prompt_input_schema(reviewer: Reviewer, *, repo_root=None, variant: str | None = None) -> dict:
    """A JSON Schema for a prompt's declared input variables (WS-F2).

    Properties = the declared ``variables`` (string-typed); ``required`` = the
    declared required subset (or ALL declared variables when ``required`` is
    unspecified). Falls back to the template's actually-used ``{{vars}}`` when no
    front-matter declares them. ``additionalProperties`` is allowed (the engine
    supplies extra context like ``repo_path``)."""
    body, meta = load_prompt(reviewer, repo_root=repo_root, variant=variant)
    declared = list(meta.get("variables") or sorted(template_variables(body)))
    required = list(meta.get("required", declared))
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {v: {"type": "string"} for v in declared},
        "required": [r for r in required if r in declared],
    }


def check_prompt_parity(
    reviewer: Reviewer, *, repo_root=None, variant: str | None = None
) -> list[str]:
    """Parity gate (WS-F2): diff a prompt's DECLARED front-matter ``variables`` vs the
    vars its template actually uses. A used-but-undeclared var is an error; a
    declared-but-unused var is an error. No declared front-matter → no findings (the
    template's used vars are self-declaring; the universe gate covers those)."""
    body, meta = load_prompt(reviewer, repo_root=repo_root, variant=variant)
    if "variables" not in meta:
        return []
    declared = set(meta.get("variables") or [])
    used = template_variables(body)
    errors: list[str] = []
    for v in sorted(used - declared):
        errors.append(f"variable {{{{{v}}}}} used in template but not declared in front-matter")
    for v in sorted(declared - used):
        errors.append(f"variable {v!r} declared in front-matter but unused in template")
    bad_required = set(meta.get("required") or []) - declared
    for v in sorted(bad_required):
        errors.append(f"required variable {v!r} is not in declared variables")
    return errors


def prompt_ref_exists(prompt_id: str, *, repo_root=None) -> bool:
    """True if a workflow ``prompt:`` ref resolves to a real prompt (WS-F2): a known
    catalog reviewer, or a user ``.rebar/prompts/<id>.md`` file. Stdlib-only."""
    if prompt_id in load_catalog():
        return True
    if repo_root:
        f = Path(repo_root) / ".rebar" / "prompts" / f"{prompt_id}.md"
        try:
            return f.is_file()
        except OSError:
            return False
    return False


def prompt_content_hash(text: str) -> str:
    """sha256 of the canonical prompt text — the identity embedded in traces so a
    divergence between what ran and any Langfuse/registry copy is detectable."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_prompt(
    reviewer: Reviewer,
    variables: dict,
    langfuse_cfg=None,
    *,
    repo_root=None,
    variant: str | None = None,
) -> tuple[str, dict]:
    """Resolve a reviewer's system prompt → ``(compiled_text, prompt_meta)`` (WS-F1/F2).

    GIT-CANONICAL: the text is the committed prompt file's body (a user
    ``.rebar/prompts/<id>.md`` override, else the packaged ``*.md``), with front-matter
    stripped and any ``variant`` overlay applied; **Langfuse is never consulted for
    the text** (read-replica only). Front-matter-declared ``required`` variables must
    be supplied (else PromptError), and rendering is strict on used vars. The returned
    ``prompt_meta`` carries the content hash + provenance + variant and is threaded
    into the trace (via RunRequest.langfuse_prompt), so the exact prompt bytes that
    ran are recorded and any divergence from a Langfuse copy is visible.

    ``langfuse_cfg`` is accepted for call-site compatibility but no longer drives a
    text fetch."""
    body, meta = load_prompt(reviewer, repo_root=repo_root, variant=variant)
    missing_required = sorted(set(meta.get("required") or []) - set(variables))
    if missing_required:
        raise PromptError(
            f"prompt {reviewer.id!r} requires variable(s) {missing_required} that were not supplied"
        )
    compiled = _render_strict(body, variables)
    return compiled, {
        "prompt_id": reviewer.id,
        "content_sha256": prompt_content_hash(body),
        "source": "git",
        "variant": variant,
    }


def _glob_match(path: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    # `**/` should also match at the repo root, so try the pattern with the
    # leading `**/` stripped too.
    return fnmatch(path, pattern) or (pattern.startswith("**/") and fnmatch(path, pattern[3:]))


def select_reviewers(
    changed_files: list[str], *, catalog: dict[str, Reviewer] | None = None
) -> list[str]:
    """Deterministic reviewer selection for a change (the rule layer used by the
    future code-review op). Returns the union of every ``default`` reviewer and
    every reviewer whose ``applies_to`` globs match a changed file, in catalog
    order. Pure + table-driven so it is fully offline-testable.
    """
    cat = catalog if catalog is not None else load_catalog()
    selected: list[str] = []
    for rid, rv in cat.items():
        if rv.default or any(_glob_match(f, g) for g in rv.applies_to for f in changed_files):
            selected.append(rid)
    return selected
