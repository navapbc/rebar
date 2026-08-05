"""Happy-path contract for the config-is-read gate (ticket 6754, corrected by bug 2c58).

The gate proves every `_config_schema.py` dataclass field has >=1 attribute-read
site outside the schema/config plumbing, or carries a `# read-via: <pointer>`
escape marker.

Detection is OWNER-RESOLVED (bug 2c58-e710-275e-4ff7): a read satisfies a field only
when the read's RECEIVER resolves to the dataclass that declares it. The gate
originally matched the bare terminal attribute name against one global set, so a
field was satisfied by any same-named attribute on any unrelated object -- which is
why an inert `CodeHealthConfig.enabled` passed while a uniquely-named sibling did
not. Owner resolution uses the schema's own section map (`Config.mcp: McpConfig`),
annotations, local alias assignment, function return annotations, and a
cross-module fixed point binding unannotated parameters from their call sites.

API contract (scripts/check_config_reads.py):
  - check(schema_path: Path, root: Path) -> list[str]   # error strings, [] == clean
  - main(argv: list[str] | None) -> int                  # 0 clean, 1 failures
Edge shapes (receiver-resolution variants, marker placement/validation variants,
store-context, plumbing exclusion, CI wiring, real-tree cleanliness) are held out.
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


# A minimal but REALISTIC schema shape: a section dataclass plus the root ``Config``
# that binds it under a section attribute name. The root class is what makes owner
# resolution possible at all -- ``Config.mcp: McpConfig`` is the edge that tells the
# gate a read off ``cfg.mcp`` belongs to ``McpConfig``.
_SCHEMA_ONE_FIELD = """\
from dataclasses import dataclass, field


@dataclass
class McpConfig:
    ghost_knob: bool = False


@dataclass
class Config:
    # Marked so the section-wiring field never confounds an assertion about the knob.
    mcp: McpConfig = field(default_factory=McpConfig)  # read-via: section wiring
"""


def test_field_with_a_read_site_passes(gate, tmp_path):
    schema, root = _tree(
        tmp_path,
        _SCHEMA_ONE_FIELD,
        "def f(cfg: Config):\n    return cfg.mcp.ghost_knob\n",
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


# ── bug 2c58: the owner-resolution contract ─────────────────────────────────────
_SCHEMA_NAME_COLLISION = """\
from dataclasses import dataclass, field


@dataclass
class CodeHealthConfig:
    # Inert: nothing anywhere reads *this* class's `enabled`.
    enabled: bool = False


@dataclass
class UiConfig:
    # Live: read by the consumer below. Shares the bare name `enabled`.
    enabled: bool = False


@dataclass
class Config:
    # Both marked: these tests are about the knobs, not the section wiring.
    code_health: CodeHealthConfig = field(default_factory=CodeHealthConfig)  # read-via: wiring
    ui: UiConfig = field(default_factory=UiConfig)  # read-via: section wiring
"""


def test_same_named_field_on_an_unrelated_config_does_not_satisfy_it(gate, tmp_path):
    """Bug 2c58 (RED-first): the reproduction of the false-negative class.

    ``CodeHealthConfig.enabled`` has zero read sites; ``UiConfig.enabled`` has one.
    Under the original global terminal-name matching, the live ``ui.enabled`` read put
    the bare name ``enabled`` into one global set, which silently satisfied the inert
    ``CodeHealthConfig.enabled`` too -- so the gate reported clean. That is the exact
    shape that let the real ``CodeHealthConfig.enabled`` survive until a573.

    Owner resolution must credit the read to ``UiConfig`` alone and fire on
    ``CodeHealthConfig.enabled``.
    """
    schema, root = _tree(
        tmp_path,
        _SCHEMA_NAME_COLLISION,
        "def f(cfg: Config):\n    return cfg.ui.enabled\n",
    )
    errors = gate.check(schema, root)
    assert len(errors) == 1, "exactly the inert field must fire; got: " + repr(errors)
    assert "CodeHealthConfig" in errors[0] and "enabled" in errors[0]
    assert "UiConfig" not in errors[0], "the live field must not fire"
