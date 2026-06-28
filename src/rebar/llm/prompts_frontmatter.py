"""Prompt front-matter I/O — parse / split / write the YAML front-matter block.

Extracted from :mod:`rebar.llm.prompts` along the front-matter seam (epic 5ca8 /
``dazed-daisy-bur``) so the prompt-registry module drops back under the module-size
soft cap. This module owns the canonical front-matter format — the closed
:data:`FRONT_MATTER_KEYS` set + :data:`PROMPT_SCHEMA_VERSION` — the read-side parsers
(:func:`parse_front_matter`, rendering-normalized; :func:`_split_front_matter_raw`,
byte-preserving), the canonical writer (:func:`write_front_matter`), and the two
prompt exceptions (:class:`PromptError` and its :class:`PromptVersionError`
subclass). The exceptions MUST live here with the format: the version check raises
``PromptVersionError`` and ``PromptVersionError`` subclasses ``PromptError``, so
leaving either behind in ``prompts.py`` would force this module to import
``prompts.py`` and create a cycle.

:mod:`rebar.llm.prompts` imports every name here back and re-exports it, so existing
``from rebar.llm.prompts import parse_front_matter`` (etc.) call-sites are unchanged.

Stdlib-only (``yaml`` is imported lazily inside the functions that need it).
"""

from __future__ import annotations

import re
import warnings

from rebar.llm.errors import LLMConfigError

__all__ = [
    "FRONT_MATTER_KEYS",
    "PROMPT_SCHEMA_VERSION",
    "PromptError",
    "PromptVersionError",
    "parse_front_matter",
    "write_front_matter",
]


class PromptError(LLMConfigError):
    """A prompt could not be resolved/rendered: strict-undefined variable, a missing
    canonical file, or a declared/used variable parity mismatch (WS-F)."""


_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# ── Prompt front-matter format (workflow authoring v2, d25d) ───────────────────
#
# The current front-matter schema version this binary understands. A file declaring
# a HIGHER version is refused on read (mixed-fleet safety; deploy order is
# readers-before-writers, like TAG_DELTA). The WRITER bumps this only when it begins
# emitting a key a prior version lacked.
PROMPT_SCHEMA_VERSION = 1

# The CLOSED front-matter key set (Prompty spellings), in canonical emit order.
# `inputs`/`outputs` are the prompt's contract surface (heavy schemas are referenced
# BY NAME here, never inlined). Unknown keys are WARN+PRESERVEd (appended, sorted) so
# a newer key on an older binary is never silently dropped.
FRONT_MATTER_KEYS: tuple[str, ...] = (
    "schema_version",
    "title",
    "description",
    "inputs",
    "outputs",
    "execution_mode",
    "category",
    "model",
    "tags",
    "dimension",
    "applies_to",
    "file_impact",
    "langfuse_prompt",
    "default",
)


class PromptVersionError(PromptError):
    """A prompt file declares a front-matter ``schema_version`` newer than this
    binary understands — refused on read so unknown front-matter is never rendered
    into the body (upgrade rebar; deploy readers before writers)."""


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split an optional YAML front-matter block off a prompt file → ``(meta, body)``.

    A prompt may begin with ``---\\n<yaml>\\n---\\n``; declared keys are
    ``variables`` (the vars the template uses), ``required`` (subset that MUST be
    supplied), and ``variant_of`` (a parent prompt id for overlay chaining). No
    front-matter → ``({}, text)`` (so existing front-matter-less prompts are
    unchanged).

    Line endings are normalized first: a leading UTF-8 BOM is stripped and
    CRLF/CR are folded to LF, so a Windows checkout (or ``core.autocrlf``) cannot
    defeat the ``\\n``-anchored front-matter fence — and the resolved body (hence
    its content hash) is line-ending-independent."""
    if text.startswith("\ufeff"):
        text = text[1:]
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
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
    _refuse_newer_schema_version(meta)
    return meta, text[m.end() :]


def _refuse_newer_schema_version(meta: dict) -> None:
    """Read-side version coexistence (d25d): refuse a prompt whose ``schema_version``
    is HIGHER than this binary understands, so unknown front-matter is never rendered
    into the body. Graceful + clear (callers catch :class:`PromptVersionError` and
    skip the prompt); the safety net behind the readers-before-writers deploy order."""
    ver = meta.get("schema_version")
    if isinstance(ver, bool) or not isinstance(ver, int):
        return  # absent / non-int → legacy or malformed; the schema layer handles shape
    if ver > PROMPT_SCHEMA_VERSION:
        raise PromptVersionError(
            f"prompt front-matter schema_version {ver} is newer than this rebar "
            f"understands ({PROMPT_SCHEMA_VERSION}); upgrade rebar (deploy "
            f"readers-before-writers). Refusing to render this prompt."
        )


def _split_front_matter_raw(text: str) -> tuple[dict, str]:
    """Split front-matter off WITHOUT normalizing the body (byte-preserving) — the
    inverse of :func:`write_front_matter`, used for the canonical round-trip. Rejects
    a leading BOM; refuses a newer schema_version like the rendering reader."""
    if text.startswith("\ufeff"):
        raise PromptError("prompt file has a UTF-8 BOM; canonical prompt files are BOM-free")
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
    _refuse_newer_schema_version(meta)
    return meta, text[m.end() :]


def write_front_matter(meta: dict, body: str) -> str:
    """Serialize ``(meta, body)`` to a CANONICAL prompt file (d25d).

    Canonical = known keys in :data:`FRONT_MATTER_KEYS` order, then unknown keys
    WARN+PRESERVEd (appended, sorted, deterministic); ``schema_version`` stamped; LF
    line endings with a single trailing newline on the front-matter block; the body
    preserved BYTE-FOR-BYTE after the closing fence (no-trailing-newline, embedded
    CRLF, and a body that itself starts with ``---`` all survive). Idempotent:
    ``write(*split_raw(write(m, b))) == write(m, b)``. A leading BOM in the body is
    refused (canonical files are BOM-free)."""
    if body.startswith("\ufeff"):
        raise PromptError("refusing to write a prompt body with a leading UTF-8 BOM")
    import yaml

    m = dict(meta)
    m["schema_version"] = m.get("schema_version", PROMPT_SCHEMA_VERSION)  # stamp
    ordered: dict = {k: m[k] for k in FRONT_MATTER_KEYS if k in m}
    for k in sorted(key for key in m if key not in FRONT_MATTER_KEYS):
        warnings.warn(
            f"prompt front-matter: preserving unrecognized key {k!r} (appended)",
            stacklevel=2,
        )
        ordered[k] = m[k]
    dumped = yaml.safe_dump(ordered, sort_keys=False, default_flow_style=False, allow_unicode=True)
    dumped = dumped.replace("\r\n", "\n").replace("\r", "\n")
    if not dumped.endswith("\n"):
        dumped += "\n"
    return f"---\n{dumped}---\n{body}"
