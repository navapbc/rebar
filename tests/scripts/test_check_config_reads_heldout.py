"""Held-out edge suite for the config-is-read gate (ticket 6754).

Pins the receiver-agnostic detection shape, marker placement/validation edges,
plumbing exclusion, exit codes, the accepted fail-quiet collision trade-off,
CI wiring, and tree-cleanliness of the real repo at landing.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.scripts

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "check_config_reads.py"
_REAL_SCHEMA = REPO_ROOT / "src" / "rebar" / "_config_schema.py"
_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "_build-and-test.yml"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_config_reads_heldout", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SCHEMA = """\
from dataclasses import dataclass


@dataclass
class SectionConfig:
    ghost_knob: bool = False
"""


def _tree(tmp_path: Path, schema_body: str, reader_body: str) -> tuple[Path, Path]:
    schema = tmp_path / "_config_schema.py"
    schema.write_text(schema_body, encoding="utf-8")
    root = tmp_path / "srcroot"
    root.mkdir()
    (root / "consumer.py").write_text(reader_body, encoding="utf-8")
    return schema, root


# ── receiver-agnostic read shapes (the G6/E2 finding the plan pinned) ────────────
@pytest.mark.parametrize(
    "reader",
    [
        "def f():\n    return load_config().section.ghost_knob\n",
        "def f(section_cfg):\n    return section_cfg.ghost_knob\n",
        "class C:\n    def m(self):\n        return self.config.section.ghost_knob\n",
        "def f(cfg=None):\n    x = cfg.section\n    return x.ghost_knob\n",
    ],
)
def test_every_real_receiver_shape_counts_as_a_read(gate, tmp_path, reader):
    schema, root = _tree(tmp_path, _SCHEMA, reader)
    assert gate.check(schema, root) == []


def test_store_context_assignment_is_not_a_read(gate, tmp_path):
    schema, root = _tree(tmp_path, _SCHEMA, "def f(o):\n    o.ghost_knob = True\n")
    errors = gate.check(schema, root)
    assert len(errors) == 1 and "ghost_knob" in errors[0]


def test_unrelated_same_named_attribute_keeps_the_gate_quiet(gate, tmp_path):
    """Accepted trade-off (stated in the plan): terminal-name matching is permissive —
    an unrelated object's same-named attribute read suppresses the firing (fail-QUIET),
    which can never false-fire on a live field."""
    schema, root = _tree(tmp_path, _SCHEMA, "def f(unrelated):\n    return unrelated.ghost_knob\n")
    assert gate.check(schema, root) == []


# ── marker placement + validation edges ──────────────────────────────────────────
def test_marker_on_the_preceding_line_is_honoured(gate, tmp_path):
    body = _SCHEMA.replace(
        "    ghost_knob: bool = False",
        "    # read-via: getattr dispatch in consumer.py\n    ghost_knob: bool = False",
    )
    schema, root = _tree(tmp_path, body, "def f():\n    return None\n")
    assert gate.check(schema, root) == []


def test_whitespace_only_pointer_is_rejected_as_bare(gate, tmp_path):
    body = _SCHEMA.replace(
        "    ghost_knob: bool = False",
        "    ghost_knob: bool = False  # read-via:   ",
    )
    schema, root = _tree(tmp_path, body, "def f():\n    return None\n")
    errors = gate.check(schema, root)
    assert len(errors) == 1 and "ghost_knob" in errors[0]


def test_every_dead_field_is_reported_not_just_the_first(gate, tmp_path):
    body = _SCHEMA.replace(
        "    ghost_knob: bool = False",
        "    ghost_knob: bool = False\n    phantom_knob: int = 3",
    )
    schema, root = _tree(tmp_path, body, "def f():\n    return None\n")
    errors = gate.check(schema, root)
    assert len(errors) == 2
    joined = "\n".join(errors)
    assert "ghost_knob" in joined and "phantom_knob" in joined


def test_non_dataclass_annotations_are_not_collected(gate, tmp_path):
    body = "class Plain:\n    ghost_knob: bool = False\n"
    schema, root = _tree(tmp_path, body, "def f():\n    return None\n")
    assert gate.check(schema, root) == []


# ── plumbing exclusion ────────────────────────────────────────────────────────────
def test_reads_inside_config_plumbing_do_not_count(gate, tmp_path):
    schema = tmp_path / "_config_schema.py"
    schema.write_text(_SCHEMA, encoding="utf-8")
    root = tmp_path / "srcroot"
    root.mkdir()
    (root / "_config_schema.py").write_text(
        "def coerce(o):\n    return o.ghost_knob\n", encoding="utf-8"
    )
    (root / "config.py").write_text("def load(o):\n    return o.ghost_knob\n", encoding="utf-8")
    errors = gate.check(schema, root)
    assert len(errors) == 1 and "ghost_knob" in errors[0]


# ── CLI contract ───────────────────────────────────────────────────────────────────
def test_main_exit_codes_and_output(gate, tmp_path, capsys):
    schema, root = _tree(tmp_path, _SCHEMA, "def f():\n    return None\n")
    rc = gate.main(["--schema", str(schema), "--root", str(root)])
    out = capsys.readouterr()
    combined = out.out + out.err
    assert rc == 1
    assert "ghost_knob" in combined
    assert "read-via" in combined, "failure output must teach the escape marker"

    schema2, root2 = _tree(
        (lambda p: (p.mkdir(), p)[1])(tmp_path / "clean"),
        _SCHEMA,
        "def f(c):\n    return c.s.ghost_knob\n",
    )
    rc2 = gate.main(["--schema", str(schema2), "--root", str(root2)])
    out2 = capsys.readouterr()
    assert rc2 == 0
    assert "OK" in (out2.out + out2.err)


# ── the real tree at landing ───────────────────────────────────────────────────────
def test_real_repo_is_clean_with_default_paths(gate):
    assert gate.main([]) == 0


def test_real_schema_markers_all_carry_pointers(gate):
    """Every `# read-via:` marker in the real schema must justify itself with a
    non-empty pointer/reason (bare markers are exactly what the gate rejects)."""
    text = _REAL_SCHEMA.read_text(encoding="utf-8")
    markers = re.findall(r"#\s*read-via:(.*)$", text, flags=re.MULTILINE)
    assert markers, "landing enumerated indirect fields — expected at least one marker"
    for pointer in markers:
        assert pointer.strip(), "bare read-via marker committed to the real schema"


def test_ci_wires_the_gate(gate):
    body = _WORKFLOW.read_text(encoding="utf-8")
    assert "check_config_reads.py" in body
