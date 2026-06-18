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


def canonical_prompt_text(reviewer: Reviewer, *, repo_root=None) -> str:
    """The GIT-CANONICAL prompt text for a reviewer (WS-F1).

    A user override at ``<repo>/.rebar/prompts/<id>.md`` wins (local, git-tracked in
    the user's repo); otherwise the packaged ``reviewers/*.md`` (committed in this
    repo) is canonical. Langfuse is NEVER consulted here."""
    if repo_root:
        override = Path(repo_root) / ".rebar" / "prompts" / f"{reviewer.id}.md"
        try:
            if override.is_file():
                return override.read_text(encoding="utf-8")
        except OSError:
            pass
    return packaged_prompt_text(reviewer)


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
    reviewer: Reviewer, variables: dict, langfuse_cfg=None, *, repo_root=None
) -> tuple[str, dict]:
    """Resolve a reviewer's system prompt → ``(compiled_text, prompt_meta)`` (WS-F1).

    GIT-CANONICAL: the text is the committed prompt file (a user
    ``.rebar/prompts/<id>.md`` override, else the packaged ``*.md``); **Langfuse is
    never consulted for the text** (read-replica only). Rendering is strict. The
    returned ``prompt_meta`` carries the content hash + provenance and is threaded
    into the trace (via RunRequest.langfuse_prompt), so the exact prompt bytes that
    ran are recorded and any divergence from a Langfuse copy is visible.

    ``langfuse_cfg`` is accepted for call-site compatibility but no longer drives a
    text fetch."""
    text = canonical_prompt_text(reviewer, repo_root=repo_root)
    compiled = _render_strict(text, variables)
    meta = {
        "prompt_id": reviewer.id,
        "content_sha256": prompt_content_hash(text),
        "source": "git",
    }
    return compiled, meta


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
