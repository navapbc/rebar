"""The workflow DSL: a safe parser, the version-pinned JSON Schema, structural
validation, and deterministic serialization (WS-B1 / WS-B3).

The DSL is a coordinate-free, GitHub-Actions-style ``steps:`` YAML document,
git-tracked under ``.rebar/workflows/<name>.yaml``. Two properties matter most:

* **Safe to load.** ``parse_workflow`` reads YAML through a hardened loader built
  on PyYAML's ``SafeLoader`` (no arbitrary object construction) with three extra
  restrictions toward YAML 1.2 Core: anchors/aliases (``&``/``*``) and merge keys
  (``<<``) are rejected, and the YAML-1.1 ``yes/no/on/off`` boolean resolvers are
  removed so an unquoted ``on``/``no`` stays a string. A pre-parse byte cap and a
  single-document requirement bound the input.

* **Versioned & immutable.** Each DSL version ships ONE immutable JSON Schema at a
  stable ``$id`` (``workflow.v1.schema.json``). ``schema_version`` is a typed
  string; a value newer than the running rebar is a hard ``WorkflowVersionError``
  ("upgrade rebar"), never a best-effort parse. Older versions are up-converted at
  read time by the chained shim in :mod:`rebar.llm.workflow.migrate` — this module
  never rewrites a file in place.

PyYAML is a core dependency (loading a workflow is a lean-runtime capability);
``jsonschema`` is only needed to *validate* (it ships with the ``dev`` extra, like
the rest of ``rebar.schemas``). ``validate_document`` degrades to its structural
fallback when ``jsonschema`` is absent and says so.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from rebar import schemas
from rebar.llm.errors import (
    WorkflowParseError,
    WorkflowVersionError,
)

# ── Versioning ────────────────────────────────────────────────────────────────
# The newest DSL version this build understands. A file declaring a higher version
# is a hard upgrade-rebar error; lower versions are up-converted by the migrate
# shim before validation. Bump this (and add workflow.vN.schema.json + a shim) when
# the DSL gains a breaking change.
CURRENT_SCHEMA_VERSION = "2"
SUPPORTED_SCHEMA_VERSIONS = ("1", "2")

# Pre-parse byte cap. Workflow files are small, hand-authored documents; a hard
# ceiling bounds the YAML parser's work and is a cheap denial-of-service guard.
MAX_WORKFLOW_BYTES = 256 * 1024  # 256 KiB


# ── A hardened YAML loader ────────────────────────────────────────────────────


def _yaml():
    """Import PyYAML, raising a clear error if it is somehow absent.

    PyYAML is a declared core dependency, so this should never fail in a normal
    install; the guard keeps the failure mode legible rather than an opaque
    ``ModuleNotFoundError`` deep in a parse.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a core dependency
        raise WorkflowParseError(
            "PyYAML is required to load workflow files (it is a core dependency; "
            "reinstall with `pip install nava-rebar`)"
        ) from exc
    return yaml


def _build_loader():
    """Construct the strict workflow YAML loader class (cached on first use).

    Built lazily so importing this module stays free of a top-level ``yaml``
    import path question and so the class is assembled exactly once.
    """
    yaml = _yaml()

    class _StrictWorkflowLoader(yaml.SafeLoader):
        """SafeLoader hardened for the workflow DSL (anchors/merge/1.1-bools)."""

        def compose_node(self, parent, index):  # type: ignore[override]
            # A non-None .anchor on any event covers BOTH an anchored node (`&a`)
            # and an alias reference (`*a`, an AliasEvent) — reject both so a
            # workflow file cannot smuggle structure-sharing past a diff.
            event = self.peek_event()
            anchor = getattr(event, "anchor", None)
            if anchor is not None:
                raise WorkflowParseError(
                    "YAML anchors/aliases (& / *) are not allowed in workflow files",
                    line=_mark_line(event.start_mark),
                    column=_mark_col(event.start_mark),
                )
            return super().compose_node(parent, index)

        def construct_mapping(self, node, deep=False):  # type: ignore[override]
            # YAML 1.2 Core errors on duplicate mapping keys; PyYAML's SafeLoader
            # silently keeps the last. A duplicate `uses:`/`if:` silently shadowing
            # an earlier one is a real footgun (both Argo and GHA reject it), so we
            # detect dups before construction and reject.
            seen: set[Any] = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    raise WorkflowParseError(
                        "YAML merge keys (<<) are not allowed in workflow files",
                        line=_mark_line(key_node.start_mark),
                        column=_mark_col(key_node.start_mark),
                    )
                try:
                    key = self.construct_object(key_node, deep=True)
                except Exception:  # pragma: no cover - unusual/non-constructible key
                    continue
                if isinstance(key, (str, int, float, bool)) or key is None:
                    if key in seen:
                        raise WorkflowParseError(
                            f"duplicate key {key!r} in mapping",
                            line=_mark_line(key_node.start_mark),
                            column=_mark_col(key_node.start_mark),
                        )
                    seen.add(key)
            return super().construct_mapping(node, deep=deep)

    # YAML 1.2 Core normalization: give the subclass its own resolver table (so we
    # never mutate the shared SafeLoader's), drop every yaml.org bool resolver
    # (which in 1.1 also matches yes/no/on/off), and re-add a 1.2-Core bool that
    # matches only true/false. schema_version is required to be a quoted string by
    # the schema, so int/float resolvers are left as-is.
    _StrictWorkflowLoader.yaml_implicit_resolvers = deepcopy(
        yaml.SafeLoader.yaml_implicit_resolvers
    )
    for first_char, mappings in list(_StrictWorkflowLoader.yaml_implicit_resolvers.items()):
        kept = [(tag, rx) for (tag, rx) in mappings if tag != "tag:yaml.org,2002:bool"]
        if kept:
            _StrictWorkflowLoader.yaml_implicit_resolvers[first_char] = kept
        else:
            del _StrictWorkflowLoader.yaml_implicit_resolvers[first_char]
    _StrictWorkflowLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), list("tf")
    )
    return _StrictWorkflowLoader


_LOADER_CACHE: list[Any] = []


def _loader_cls():
    if not _LOADER_CACHE:
        _LOADER_CACHE.append(_build_loader())
    return _LOADER_CACHE[0]


def _mark_line(mark) -> int | None:
    return (mark.line + 1) if mark is not None else None


def _mark_col(mark) -> int | None:
    return (mark.column + 1) if mark is not None else None


# ── Parse ─────────────────────────────────────────────────────────────────────


def parse_workflow(text: str, *, source: str = "<workflow>") -> dict[str, Any]:
    """Parse workflow YAML ``text`` into a plain dict.

    Enforces the byte cap, the hardened loader (no anchors/merge keys/1.1 bools),
    and that the document is a single top-level mapping. Raises
    :class:`WorkflowParseError` (with a line/column when the YAML library reports
    one) on any failure. This does NOT validate against the schema — call
    :func:`validate_document` for that.
    """
    yaml = _yaml()
    raw = text.encode("utf-8")
    if len(raw) > MAX_WORKFLOW_BYTES:
        raise WorkflowParseError(
            f"workflow file is {len(raw)} bytes, over the {MAX_WORKFLOW_BYTES}-byte cap",
            source=source,
        )
    try:
        doc = yaml.load(text, Loader=_loader_cls())  # noqa: S506 - hardened SafeLoader subclass
    except WorkflowParseError as exc:
        # Re-stamp the source onto an error raised mid-compose (where it is unknown).
        raise WorkflowParseError(
            str(exc).split(": ", 1)[-1], source=source, line=exc.line, column=exc.column
        ) from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        raise WorkflowParseError(
            getattr(exc, "problem", None) or str(exc).splitlines()[0],
            source=source,
            line=_mark_line(mark),
            column=_mark_col(mark),
        ) from exc
    if doc is None:
        raise WorkflowParseError("workflow file is empty", source=source)
    if not isinstance(doc, dict):
        raise WorkflowParseError(
            f"workflow must be a mapping at the top level, got {type(doc).__name__}",
            source=source,
        )
    return doc


def load_workflow(path: str | Path, *, source: str | None = None) -> dict[str, Any]:
    """Read and parse a workflow file from disk (see :func:`parse_workflow`)."""
    p = Path(path)
    src = source or str(p)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowParseError(f"cannot read workflow file: {exc}", source=src) from exc
    return parse_workflow(text, source=src)


# ── Version resolution ────────────────────────────────────────────────────────


def declared_version(doc: dict[str, Any], *, source: str = "<workflow>") -> str:
    """The ``schema_version`` string from a parsed document.

    Raises :class:`WorkflowParseError` when missing or not a string (the typed-
    string requirement is load-time, not just schema-time, so the migrate shim and
    version gate can rely on it).
    """
    raw = doc.get("schema_version")
    if raw is None:
        raise WorkflowParseError("missing required key `schema_version`", source=source)
    if not isinstance(raw, str):
        raise WorkflowParseError(
            f"`schema_version` must be a quoted string, got {type(raw).__name__} "
            f'({raw!r}) — write `schema_version: "1"`',
            source=source,
        )
    return raw


def schema_name_for_version(version: str, *, source: str = "<workflow>") -> str:
    """Map a ``schema_version`` to its packaged schema name (``workflow.v1``).

    A version newer than this build understands is a hard
    :class:`WorkflowVersionError`. (Older-but-supported versions are handled by the
    migrate shim, which up-converts before this is consulted for the final shape.)
    """
    if version in SUPPORTED_SCHEMA_VERSIONS:
        return f"workflow.v{version}"
    # A purely-numeric version above the current ceiling is the upgrade case.
    if version.isdigit() and int(version) > int(CURRENT_SCHEMA_VERSION):
        raise WorkflowVersionError(
            f"{source}: workflow schema_version {version!r} is newer than this rebar "
            f"supports (max {CURRENT_SCHEMA_VERSION!r}) — upgrade rebar to run it"
        )
    raise WorkflowVersionError(
        f"{source}: unknown workflow schema_version {version!r} "
        f"(supported: {', '.join(SUPPORTED_SCHEMA_VERSIONS)})"
    )


# ── Validation ────────────────────────────────────────────────────────────────


def validate_document(doc: dict[str, Any], *, source: str = "<workflow>") -> list[str]:
    """Validate a parsed (and migrated) document against its version's JSON Schema.

    Returns a list of located, human-readable error strings — EMPTY means valid.
    Collects ALL schema errors in one pass (never raises on the first) so authoring
    tools can show every problem at once. Raises :class:`WorkflowVersionError` only
    for the upgrade-rebar case (resolving the schema name).

    Reference integrity, the expression allow-list, and the secret scan are NOT
    here — they are the linter's job (:mod:`rebar.llm.workflow.lint`), which calls
    this first and appends its own findings.
    """
    version = declared_version(doc, source=source)
    schema_name = schema_name_for_version(version, source=source)

    try:
        validator = schemas.validator(schema_name)
    except ImportError:
        # jsonschema/referencing absent: fall back to a minimal structural check so
        # the lean core still gives a useful answer, and flag the gap honestly.
        errs = _structural_fallback(doc)
        errs.append(
            "note: full JSON Schema validation skipped (install `jsonschema` for "
            "complete checks; e.g. `pip install nava-rebar[dev]`)"
        )
        return errs

    errors: list[str] = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{loc}: {err.message}")
    return errors


def _structural_fallback(doc: dict[str, Any]) -> list[str]:
    """A pure-stdlib structural check used only when jsonschema is unavailable.

    Deliberately shallow — it covers the load-bearing shape (required top-level
    keys, steps is a non-empty list of mappings with a unique id and exactly one of
    uses/prompt) so a workflow is never executed wholly unvalidated, but the JSON
    Schema remains the authoritative contract when jsonschema is present.
    """
    errors: list[str] = []
    for key in ("schema_version", "name", "steps"):
        if key not in doc:
            errors.append(f"<root>: missing required key `{key}`")
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps: must be a non-empty list")
        return errors
    seen: set[str] = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"steps/{i}: must be a mapping")
            continue
        sid = step.get("id")
        if not isinstance(sid, str) or not sid:
            errors.append(f"steps/{i}: missing string `id`")
        elif sid in seen:
            errors.append(f"steps/{i}: duplicate step id {sid!r}")
        else:
            seen.add(sid)
        # Exactly one discriminator: uses (scripted) | prompt (agent) | branch | loop
        # | map (the v2 control constructs). The full JSON Schema enforces the precise
        # oneOf + nested shapes; this shallow check only guards the lean (jsonschema-
        # absent) path against a step with zero or multiple top-level discriminators.
        present = [k for k in ("uses", "prompt", "branch", "loop", "map") if k in step]
        if len(present) != 1:
            errors.append(
                f"steps/{i}: a step needs exactly one of `uses` (scripted), `prompt` "
                f"(agent), `branch`, `loop`, or `map`; found {present or 'none'}"
            )
    return errors


# The v2 control constructs (each carries a nested frame). A step is exactly one of
# these or a leaf (scripted/agent); ``step_kind`` reports which.
CONTROL_KINDS = ("branch", "loop", "map")


def step_kind(step: dict[str, Any]) -> str:
    """Classify a step as ``"scripted"``, ``"agent"``, or a control construct
    (``"branch"`` / ``"loop"`` / ``"map"``) from its shape.

    The DSL discriminator is the single control key present: ``uses`` (scripted),
    ``prompt`` (agent), ``branch`` / ``loop`` / ``map`` (the v2 control constructs).
    An explicit ``type`` is honored when present. Assumes a schema-valid step
    (exactly one discriminator) — callers should validate first.
    """
    declared = step.get("type")
    if declared in ("scripted", "agent", *CONTROL_KINDS):
        return declared
    for kind in CONTROL_KINDS:
        if kind in step:
            return kind
    return "agent" if "prompt" in step else "scripted"


def is_control_step(step: dict[str, Any]) -> bool:
    """True if ``step`` is a v2 control construct (branch/loop/map), i.e. it carries
    a nested frame rather than dispatching a single scripted/agent action."""
    return step_kind(step) in CONTROL_KINDS


# ── Deterministic serialization ───────────────────────────────────────────────

# A stable top-level key order for human-facing serialization, so re-emitting a
# workflow (e.g. after `new`/scaffold) yields a minimal, review-friendly diff.
_TOP_ORDER = ("schema_version", "name", "description", "model", "inputs", "steps")
_STEP_ORDER = (
    "id",
    "type",
    "uses",
    "prompt",
    "branch",
    "loop",
    "map",
    "needs",
    "with",
    "output_schema",
    "mode",
    "model",
    "if",
)


def canonical_json(doc: Any) -> str:
    """A canonical JSON string for a document (sorted keys, compact separators).

    Used for content-hashing a workflow so a run can record exactly which
    definition it executed; NOT the human-facing form.
    """
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(doc: Any) -> str:
    """A short, stable content hash (sha256 of the canonical JSON) for a document."""
    import hashlib

    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


def _ordered(mapping: dict[str, Any], order: tuple[str, ...]) -> dict[str, Any]:
    out = {k: mapping[k] for k in order if k in mapping}
    for k in mapping:  # preserve any unknown keys deterministically at the end
        if k not in out:
            out[k] = mapping[k]
    return out


def dump_workflow(doc: dict[str, Any]) -> str:
    """Serialize a workflow dict to YAML with a stable key order (minimal diffs).

    Block style, no flow collections, no anchors — the same shape the parser
    accepts. Used by ``rebar workflow new`` to write a scaffold deterministically.
    """
    yaml = _yaml()
    ordered = _ordered(doc, _TOP_ORDER)
    if isinstance(ordered.get("steps"), list):
        ordered["steps"] = [
            _ordered(s, _STEP_ORDER) if isinstance(s, dict) else s for s in ordered["steps"]
        ]
    return yaml.dump(
        ordered,
        Dumper=yaml.SafeDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
