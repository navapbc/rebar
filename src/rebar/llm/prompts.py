"""Reviewer registry — the library of reviewer prompts, each addressed by id.

Two concerns are kept deliberately separate (the converging OSS pattern):

  * **Reviewer identity + selection rules** (id, dimension, file-glob ``applies_to``,
    whether it's a default) live in a versioned, testable local catalog
    (``reviewers/catalog.json``) — so selection is code-reviewed and offline-testable.
  * **Prompt text** is fetched from **Langfuse prompt management** by id
    (``get_prompt(name, label="production", fallback=…)``) — hot-editable without a
    deploy — with a packaged ``*.md`` fallback so the framework still runs offline /
    when Langfuse is unconfigured.

Stdlib-only; Langfuse is imported lazily and only when configured.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib.resources import files

_VAR = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class ReviewerError(KeyError):
    """Raised when a reviewer id is not in the catalog."""


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
        raise ReviewerError(
            f"unknown reviewer '{reviewer_id}'; known: {sorted(catalog)}"
        ) from None


def packaged_prompt_text(reviewer: Reviewer) -> str:
    """The packaged fallback prompt text for a reviewer (raw, with {{vars}})."""
    if not reviewer.fallback_file:
        return ""
    return _catalog_dir().joinpath(reviewer.fallback_file).read_text(encoding="utf-8")


def _render(template: str, variables: dict) -> str:
    """Render Langfuse-style ``{{var}}`` placeholders from ``variables``."""
    return _VAR.sub(lambda m: str(variables.get(m.group(1), "")), template)


def resolve_prompt(reviewer: Reviewer, variables: dict, langfuse_cfg) -> tuple[str, object | None]:
    """Resolve a reviewer's system prompt → ``(compiled_text, langfuse_prompt_obj)``.

    Prefers Langfuse prompt management when configured+installed (so the prompt is
    versioned/observable and the returned object can link prompt→trace); otherwise
    renders the packaged fallback. Never raises on a Langfuse outage — falls back."""
    fallback = packaged_prompt_text(reviewer)
    if langfuse_cfg is not None and getattr(langfuse_cfg, "enabled", False):
        try:
            from langfuse import get_client

            client = get_client()
            prompt = client.get_prompt(
                reviewer.prompt_name, label="production", fallback=fallback or None
            )
            return prompt.compile(**variables), prompt
        except Exception:
            pass  # network/SDK/config issue — fall through to the packaged prompt
    return _render(fallback, variables), None


def _glob_match(path: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    # `**/` should also match at the repo root, so try the pattern with the
    # leading `**/` stripped too.
    return fnmatch(path, pattern) or (
        pattern.startswith("**/") and fnmatch(path, pattern[3:])
    )


def select_reviewers(changed_files: list[str], *, catalog: dict[str, Reviewer] | None = None) -> list[str]:
    """Deterministic reviewer selection for a change (the rule layer used by the
    future code-review op). Returns the union of every ``default`` reviewer and
    every reviewer whose ``applies_to`` globs match a changed file, in catalog
    order. Pure + table-driven so it is fully offline-testable.
    """
    cat = catalog if catalog is not None else load_catalog()
    selected: list[str] = []
    for rid, rv in cat.items():
        if rv.default or any(
            _glob_match(f, g) for g in rv.applies_to for f in changed_files
        ):
            selected.append(rid)
    return selected
