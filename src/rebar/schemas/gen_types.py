"""Generate ``src/rebar/types.py`` — schema-derived ``TypedDict``s for the
public ``rebar.*`` return contract (story 3a10).

The canonical public contract is the JSON Schemas in this package. This module
translates the schema-backed subset of the facade's return shapes into
``TypedDict``s so library consumers get named keys + type-checker support, with
**zero runtime change** (returns stay plain dicts).

Design (see the story + the ADR ``docs/adr/0031-schema-derived-typeddicts.md``):

- **Open schemas → closed TypedDicts of the documented contract.** Every public
  output schema is ``additionalProperties: true`` (the event-sourced shape may
  grow). TypedDict can't express arbitrary extra keys, so we emit closed
  TypedDicts naming the *guaranteed/known* keys: schema-``required`` keys are
  normal fields; non-required keys are ``NotRequired[...]``. The runtime dict
  stays open — reading undocumented extra keys is outside the typed contract.
- **Custom, dependency-free.** We resolve exactly the constructs rebar's schemas
  use (cross-file ``$ref`` into ``common.schema.json#/$defs/*``, ``["T","null"]``
  unions, enum ``$ref`` → ``Literal[...]``, arrays) rather than pulling in an
  off-the-shelf generator with partial draft-2020-12 / cross-file-``$ref`` support.
- **Literal only from a formal ``enum``.** A value vocabulary that lives only in a
  ``description`` string (e.g. ``workflow_run.status``) maps to ``str``, not
  ``Literal`` — we don't over-promise.

Entry point (mirrors ``regenerate_prompt_index``)::

    python -m rebar.schemas.gen_types            # regenerate in place
    python -m rebar.schemas.gen_types --check     # exit 1 if the file is stale

The CI drift-gate runs the regenerate form then ``git diff --exit-code`` — a
stale ``types.py`` fails the build.
"""

from __future__ import annotations

import keyword
import subprocess
import sys
from pathlib import Path
from typing import Any

from rebar import schemas

# --- the schema-backed facade subset we generate TypedDicts for -----------------
# Top-level OBJECT schemas → one TypedDict each (named by the schema ``title``).
TOP_LEVEL_OBJECTS: list[str] = [
    "ticket_state",
    "ticket_state_llm",
    "create_result",
    "claim_result",
    "transition_result",
    "clarity_result",
    "gate_result",
    "validate_report",
    "grounding_info",
    "sign_result",
    "verify_signature_result",
    "deps_graph",
    "next_batch",
    "bridge_fsck",
    "workflow_run",
]
# Top-level ARRAY schemas → a ``list[...]`` alias (facade returns a list).
TOP_LEVEL_ARRAYS: list[str] = [
    "file_impact",
    "verify_commands",
    "summary",
]

_COMMON = "common"
_COMMON_REF_PREFIX = "common.schema.json#/$defs/"


def _camel(name: str) -> str:
    """``file_impact_entry`` → ``FileImpactEntry`` (a schema/def name → class)."""
    return "".join(part.capitalize() for part in name.replace("-", "_").split("_"))


class _Generator:
    def __init__(self) -> None:
        self._common_defs: dict[str, Any] = schemas.load(_COMMON).get("$defs", {})
        # Referenced common $defs, recorded in first-seen order but emitted in the
        # canonical order they appear in common.schema.json (dependency-safe).
        self._used_defs: set[str] = set()
        self._used_typing: set[str] = {"TypedDict"}

    # -- type resolution ---------------------------------------------------------
    def _ref_name(self, ref: str) -> str:
        if not ref.startswith(_COMMON_REF_PREFIX):
            raise ValueError(f"unsupported $ref (only common $defs are supported): {ref}")
        return ref[len(_COMMON_REF_PREFIX) :]

    def _def_pytype(self, def_name: str) -> str:
        """Python type for a ``common.schema.json#/$defs/<def_name>`` reference."""
        node = self._common_defs[def_name]
        if "enum" in node:
            self._used_defs.add(def_name)
            return _camel(def_name)  # emitted as a ``Literal[...]`` alias
        if node.get("type") == "object":
            self._used_defs.add(def_name)
            return _camel(def_name)  # emitted as a shared TypedDict
        # scalar def (e.g. priority: integer)
        return self._scalar(node.get("type"))

    @staticmethod
    def _scalar(json_type: str | None) -> str:
        return {
            "string": "str",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
            "null": "None",
        }.get(json_type or "", "Any")

    def _pytype(self, node: dict[str, Any]) -> str:
        """Translate a schema node (a property value) into a Python type string."""
        if "$ref" in node:
            return self._def_pytype(self._ref_name(node["$ref"]))
        if "enum" in node:
            self._used_typing.add("Literal")
            return "Literal[" + ", ".join(repr(v) for v in node["enum"]) + "]"
        # A JSON-Schema `const` pins the value to a single literal (e.g.
        # `{"const": true}` on `creation_channel_inferred` -> `Literal[True]`).
        # `enum` handles a set of values; `const` is the one-value case, which
        # otherwise falls through to `Any`.
        if "const" in node:
            self._used_typing.add("Literal")
            return f"Literal[{node['const']!r}]"

        jtype = node.get("type")
        if isinstance(jtype, list):
            parts = [self._scalar(t) for t in jtype if t != "null"]
            rendered = " | ".join(dict.fromkeys(parts)) or "Any"
            if "null" in jtype:
                rendered = f"{rendered} | None"
            if "Any" in parts:
                self._used_typing.add("Any")
            return rendered

        if jtype == "array":
            items = node.get("items")
            if isinstance(items, dict) and items:
                return f"list[{self._pytype(items)}]"
            self._used_typing.add("Any")
            return "list[Any]"

        if jtype == "object":
            # Inline nested object (or open free-form map) — kept as a dict; the
            # named nested shapes in rebar's schemas are always $ref'd, not inline.
            self._used_typing.add("Any")
            return "dict[str, Any]"

        if jtype is None:
            self._used_typing.add("Any")
            return "Any"
        return self._scalar(jtype)

    # -- TypedDict emission ------------------------------------------------------
    def _object_block(self, class_name: str, schema: dict[str, Any], doc: str) -> str:
        props: dict[str, Any] = schema.get("properties", {})
        required = set(schema.get("required", []))
        fields: list[tuple[str, str]] = []
        for key, node in props.items():
            pytype = self._pytype(node)
            if key not in required:
                self._used_typing.add("NotRequired")
                pytype = f"NotRequired[{pytype}]"
            fields.append((key, pytype))

        needs_functional = any((not k.isidentifier()) or keyword.iskeyword(k) for k, _ in fields)
        if needs_functional:
            # A key isn't a valid identifier (e.g. ``from``) → functional form.
            lines = [f"{class_name} = TypedDict(", f'    "{class_name}",', "    {"]
            for key, pytype in fields:
                lines.append(f'        "{key}": {pytype},')
            lines.append("    },")
            lines.append(")")
            body = "\n".join(lines)
            return f"# {doc}\n{body}\n"

        lines = [f"class {class_name}(TypedDict):", f'    """{doc}"""']
        if not fields:
            lines.append("    pass")
        for key, pytype in fields:
            lines.append(f"    {key}: {pytype}")
        return "\n".join(lines) + "\n"

    def generate(self) -> str:
        object_blocks: list[str] = []
        alias_blocks: list[str] = []

        # Top-level object schemas.
        for name in TOP_LEVEL_OBJECTS:
            schema = schemas.load(name)
            class_name = schema.get("title") or _camel(name)
            doc = f"Return shape of the `{name}` output schema."
            object_blocks.append(self._object_block(class_name, schema, doc))

        # Top-level array schemas → ``Alias = list[Item]``.
        for name in TOP_LEVEL_ARRAYS:
            schema = schemas.load(name)
            class_name = schema.get("title") or _camel(name)
            items = schema.get("items", {})
            item_type = self._pytype(items) if items else "dict[str, Any]"
            alias_blocks.append(
                f"# list form of the `{name}` output schema\n{class_name} = list[{item_type}]"
            )

        # Shared common $defs. Object defs may reference other defs, so render to a
        # fixed point (a newly-rendered object can pull in more defs) before we
        # decide the emit set — otherwise a def used only by a later-ordered def
        # (e.g. `relation`, used by `dep`) would be skipped.
        rendered: dict[str, str] = {}
        while True:
            pending = [d for d in self._common_defs if d in self._used_defs and d not in rendered]
            if not pending:
                break
            for def_name in pending:
                node = self._common_defs[def_name]
                cname = _camel(def_name)
                if "enum" in node:
                    self._used_typing.add("Literal")
                    values = ", ".join(repr(v) for v in node["enum"])
                    rendered[def_name] = f"{cname} = Literal[{values}]"
                else:
                    doc = f"Shared `{def_name}` object (common.schema.json)."
                    rendered[def_name] = self._object_block(cname, node, doc)

        # Emit in common.schema.json order (dependency-safe): enums first, objects next.
        enum_aliases = [
            rendered[d]
            for d in self._common_defs
            if d in rendered and "enum" in self._common_defs[d]
        ]
        def_blocks = [
            rendered[d]
            for d in self._common_defs
            if d in rendered and "enum" not in self._common_defs[d]
        ]

        return self._render(enum_aliases, def_blocks, object_blocks, alias_blocks)

    def _render(
        self,
        enum_aliases: list[str],
        def_blocks: list[str],
        object_blocks: list[str],
        alias_blocks: list[str],
    ) -> str:
        typing_imports = ", ".join(sorted(self._used_typing))
        header = [
            '"""Typed return contract for the public ``rebar.*`` facade — GENERATED.',
            "",
            "DO NOT EDIT BY HAND. Regenerate with::",
            "",
            "    python -m rebar.schemas.gen_types",
            "",
            "These ``TypedDict``s are derived from the canonical JSON Schemas in",
            "``rebar/schemas/*.schema.json`` and name the *guaranteed* keys of each",
            "return shape. The runtime dicts are OPEN (``additionalProperties: true``):",
            "extra keys may appear as the event-sourced shape evolves, so reading a key",
            "not named here is outside the typed contract by design. Required schema keys",
            "are normal fields; optional keys are ``NotRequired[...]``.",
            '"""',
            "",
            "# NOTE: deliberately NO `from __future__ import annotations` — stringized",
            "# annotations hide `NotRequired` from TypedDict, breaking __required_keys__.",
            "# Every type here is defined before use and valid at runtime on Python >=3.11.",
            f"from typing import {typing_imports}",
        ]
        sections = ["\n".join(header)]
        if enum_aliases:
            sections.append(
                "# --- shared enums (common.schema.json) ---\n" + "\n".join(enum_aliases)
            )
        if def_blocks:
            sections.append(
                "# --- shared objects (common.schema.json) ---\n"
                + "\n\n\n".join(def_blocks).rstrip()
            )
        if object_blocks:
            sections.append(
                "# --- public return shapes ---\n"
                + "\n\n\n".join(b.rstrip() for b in object_blocks)
            )
        if alias_blocks:
            sections.append("# --- public list return shapes ---\n" + "\n\n".join(alias_blocks))
        return "\n\n\n".join(sections) + "\n"


def _target_path() -> Path:
    return Path(__file__).resolve().parent.parent / "types.py"


def _ruff(args: list[str], text: str) -> str:
    proc = subprocess.run(
        ["ruff", *args],
        input=text,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def render() -> str:
    text = _Generator().generate()
    # Canonicalize to ruff's output so the committed file is stable under
    # ``ruff format --check`` + ``ruff check`` (and thus the drift gate): first
    # the import-sorter/spacing fix (I), then the formatter. Best-effort: if ruff
    # is absent the hand-emitted text (already close to ruff style) is used as-is.
    try:
        text = _ruff(["check", "--select", "I", "--fix", "--stdin-filename", "types.py", "-"], text)
        text = _ruff(["format", "-"], text)
        return text
    except (OSError, subprocess.CalledProcessError):
        return text


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in argv
    text = render()
    target = _target_path()
    if check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != text:
            print(
                f"{target} is stale — regenerate with 'python -m rebar.schemas.gen_types'",
                file=sys.stderr,
            )
            return 1
        print(f"{target}: up to date")
        return 0
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
