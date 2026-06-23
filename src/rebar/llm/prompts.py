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
import warnings
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
        includes_base = _BASE_MARKER in body
        if includes_base:
            body = body.replace(_BASE_MARKER, parent_body)
        merged = {**parent_meta, **meta}
        merged.pop("variant_of", None)
        # When the overlay splices the base body in, the spliced result uses BOTH
        # the base's and the variant's variables — so UNION their declared
        # `variables`/`required` rather than letting the shallow overlay drop the
        # base's (which would break parity + strict rendering of base vars). A
        # full override (no <!--base-->) keeps only the variant's own declarations.
        if includes_base:
            for k in ("variables", "required"):
                union: list = []
                for v in [*(parent_meta.get(k) or []), *(meta.get(k) or [])]:
                    if v not in union:
                        union.append(v)
                if union:
                    merged[k] = union
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
    # Mirror prompt_input_schema's required/optional contract exactly: when
    # front-matter declares `variables`, an absent `required` key means ALL
    # declared vars are required, and any declared-but-not-required var is OPTIONAL
    # — it defaults to empty when omitted instead of failing strict rendering (a
    # declared-optional var must not be de-facto required). Undeclared used vars
    # still raise. No front-matter → the legacy strict path (every used var
    # required) is preserved for the review prompts.
    render_vars = dict(variables)
    if "variables" in meta:
        declared = list(meta.get("variables") or [])
        required = list(meta.get("required", declared))
        missing_required = sorted(set(required) & set(declared) - set(variables))
        if missing_required:
            raise PromptError(
                f"prompt {reviewer.id!r} requires variable(s) {missing_required} "
                "that were not supplied"
            )
        for v in declared:
            if v not in required:
                render_vars.setdefault(v, "")
    else:
        missing_required = sorted(set(meta.get("required") or []) - set(variables))
        if missing_required:
            raise PromptError(
                f"prompt {reviewer.id!r} requires variable(s) {missing_required} "
                "that were not supplied"
            )
    compiled = _render_strict(body, render_vars)
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
