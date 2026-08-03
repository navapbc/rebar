"""Happy-path contract for the config-is-read gate (ticket 6754).

The gate proves every `_config_schema.py` dataclass field has >=1 attribute-read
site outside the schema/config plumbing, or carries a `# read-via: <pointer>`
escape marker. Detection is receiver-agnostic: an `ast.Attribute` in Load context
whose terminal name equals the field name counts, regardless of how the config
object reached the reader.

API contract (scripts/check_config_reads.py):
  - check(schema_path: Path, root: Path) -> list[str]   # error strings, [] == clean
  - main(argv: list[str] | None) -> int                  # 0 clean, 1 failures
Edge shapes (marker placement/validation variants, store-context, plumbing
exclusion, CI wiring, real-tree cleanliness) are held out.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.scripts

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_config_reads.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_config_reads", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(tmp_path: Path, schema_body: str, reader_body: str) -> tuple[Path, Path]:
    """A synthetic schema file + a one-module source root."""
    schema = tmp_path / "_config_schema.py"
    schema.write_text(schema_body, encoding="utf-8")
    root = tmp_path / "srcroot"
    root.mkdir()
    (root / "consumer.py").write_text(reader_body, encoding="utf-8")
    return schema, root


_SCHEMA_ONE_FIELD = """\
from dataclasses import dataclass


@dataclass
class McpConfig:
    ghost_knob: bool = False
"""


def test_field_with_a_read_site_passes(gate, tmp_path):
    schema, root = _tree(
        tmp_path,
        _SCHEMA_ONE_FIELD,
        "def f(cfg):\n    return cfg.mcp.ghost_knob\n",
    )
    assert gate.check(schema, root) == []


def test_zero_read_field_fails_naming_field_and_teaching_the_marker(gate, tmp_path):
    schema, root = _tree(
        tmp_path,
        _SCHEMA_ONE_FIELD,
        "def f():\n    return None\n",
    )
    errors = gate.check(schema, root)
    assert len(errors) == 1
    msg = errors[0]
    assert "ghost_knob" in msg, "the failing field must be named"
    assert "McpConfig" in msg, "the owning section class must be named"
    assert "read-via" in msg, "the message must teach the escape marker"


def test_marker_with_pointer_passes_despite_zero_reads(gate, tmp_path):
    schema_body = _SCHEMA_ONE_FIELD.replace(
        "    ghost_knob: bool = False",
        "    ghost_knob: bool = False  # read-via: getattr dispatch in consumer.py",
    )
    schema, root = _tree(tmp_path, schema_body, "def f():\n    return None\n")
    assert gate.check(schema, root) == []


def test_bare_marker_without_pointer_is_rejected(gate, tmp_path):
    schema_body = _SCHEMA_ONE_FIELD.replace(
        "    ghost_knob: bool = False",
        "    ghost_knob: bool = False  # read-via:",
    )
    schema, root = _tree(tmp_path, schema_body, "def f():\n    return None\n")
    errors = gate.check(schema, root)
    assert len(errors) == 1
    assert "ghost_knob" in errors[0]
    assert "pointer" in errors[0].lower() or "reason" in errors[0].lower()
