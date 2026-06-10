"""Coverage guard: structured-output coverage is ENFORCED, not aspirational.

Three invariants close the loop opened by test_schema_outputs.py (which drives
each known shape and validates it):

  1. Every schema file shipped under src/rebar/schemas/ (except the shared
     common.schema.json) is wired into the OUTPUT_SCHEMAS registry — no
     authored-but-unreferenced schema.
  2. Every OUTPUT_SCHEMAS entry resolves to a real schema file.
  3. Every CLI command whose `--help` advertises the canonical `--output` flag is
     represented in OUTPUT_SCHEMAS — so adding `--output` to a NEW command
     without authoring + registering its schema fails this test.

(3) discovers commands straight from the dispatcher's per-subcommand help source,
so it can't drift from what the CLI actually offers.
"""

from __future__ import annotations

import re
from pathlib import Path

import rebar
from rebar import schemas


def test_every_schema_file_is_wired() -> None:
    wired = set(schemas.OUTPUT_SCHEMAS.values())
    for name in schemas.names():
        if name == schemas.COMMON:
            continue
        assert name in wired, (
            f"schema {name!r} exists on disk but is not referenced by "
            f"schemas.OUTPUT_SCHEMAS (wire it or delete it)"
        )


def test_every_registry_entry_resolves() -> None:
    on_disk = set(schemas.names())
    for key, name in schemas.OUTPUT_SCHEMAS.items():
        assert name in on_disk, f"OUTPUT_SCHEMAS[{key!r}] -> missing schema {name!r}"


def _help_arms() -> dict[str, str]:
    """Map each subcommand -> its `--help` text, parsed from the dispatcher's
    _print_subcommand_help case arms (the authoritative per-command usage)."""
    text = (Path(rebar.engine_dir()) / "rebar").read_text(encoding="utf-8")
    start = text.index("_print_subcommand_help()")
    body = text[start:]
    arms: dict[str, str] = {}
    # Case arms are indented 8 spaces: `        cmd)`  or  `        a|b)` ... `;;`
    for m in re.finditer(r"\n {8}([a-z][a-z|\-]*)\)(.*?);;", body, re.S):
        pattern, content = m.group(1), m.group(2)
        for cmd in pattern.split("|"):
            arms[cmd] = content
    return arms


def test_commands_advertising_output_have_a_schema() -> None:
    arms = _help_arms()
    assert arms, "could not parse any subcommand help arms (parser drift?)"
    missing = []
    for cmd, help_text in arms.items():
        if "--output" not in help_text:
            continue
        key = cmd.replace("-", "_")  # CLI uses hyphens; registry keys use underscores
        if key not in schemas.OUTPUT_SCHEMAS:
            missing.append(cmd)
    assert not missing, (
        "these commands advertise --output in their help but lack a schema in "
        f"schemas.OUTPUT_SCHEMAS: {sorted(missing)}"
    )
