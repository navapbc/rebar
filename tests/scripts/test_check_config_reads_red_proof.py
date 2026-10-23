"""Compare the frozen pre-fix config-read gate with the live owner-aware gate.

Both inspect one synthetic schema. The old gate accepts an inert field whose
name is read on another class; the live gate reports its owning class. A
uniquely named inert field is a control that both gates must report. The
frozen snapshot supplies the old behavior without changing worktrees.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.scripts

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_config_reads.py"
_SNAPSHOT = Path(__file__).resolve().parent / "pre_fix_config_read_gate.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def old_gate() -> ModuleType:
    """Load the frozen gate by path for an in-process comparison with live."""
    return _load("pre_fix_config_read_gate", _SNAPSHOT)


@pytest.fixture(scope="module")
def live_gate() -> ModuleType:
    return _load("check_config_reads_red_proof", _SCRIPT)


# The confirmed instance, reduced: `CodeHealthConfig.enabled` was inert, but `.enabled`
# is read on unrelated configs (UiConfig, McpConfig.auth_enabled, LangfuseConfig...),
# so the bare name landed in the old gate's global set and satisfied the inert field.
_COLLIDING_SCHEMA = """\
from dataclasses import dataclass, field


@dataclass
class CodeHealthConfig:
    enabled: bool = False


@dataclass
class UiConfig:
    enabled: bool = False


@dataclass
class Config:
    code_health: CodeHealthConfig = field(default_factory=CodeHealthConfig)  # read-via: wiring
    ui: UiConfig = field(default_factory=UiConfig)  # read-via: section wiring
"""

_READS_ONLY_UI = "def f(cfg: Config):\n    return cfg.ui.enabled\n"


@pytest.fixture
def colliding_tree(tmp_path: Path) -> tuple[Path, Path]:
    schema = tmp_path / "_config_schema.py"
    schema.write_text(_COLLIDING_SCHEMA, encoding="utf-8")
    root = tmp_path / "srcroot"
    root.mkdir()
    (root / "consumer.py").write_text(_READS_ONLY_UI, encoding="utf-8")
    return schema, root


def test_the_pre_fix_gate_silently_passes_the_inert_field(old_gate, colliding_tree):
    """The old gate mistakes ``UiConfig.enabled`` for a read of the inert field."""
    schema, root = colliding_tree
    assert old_gate.check(schema, root) == [], (
        "the frozen snapshot must still exhibit the defect — if this fails, the "
        "snapshot was 'fixed' and the RED half of the proof has been destroyed"
    )


def test_the_live_gate_fires_on_the_inert_field(live_gate, colliding_tree):
    """GREEN half: owner resolution credits the read to UiConfig alone."""
    schema, root = colliding_tree
    errors = live_gate.check(schema, root)
    assert len(errors) == 1, f"exactly the inert field must fire; got: {errors!r}"
    assert "CodeHealthConfig" in errors[0] and "enabled" in errors[0]
    assert "UiConfig" not in errors[0], "the live field must not fire"


def test_old_and_new_disagree_exactly_on_the_owner_resolution(old_gate, live_gate, colliding_tree):
    """The differential assertion itself: the two versions must not agree here. A
    resolver regressed to bare-name matching would make both sides empty and trip this."""
    schema, root = colliding_tree
    before = old_gate.check(schema, root)
    after = live_gate.check(schema, root)
    assert before != after, (
        "the fix is indistinguishable from the pre-fix gate on its own reproduction — "
        "the owner-resolution behaviour has regressed"
    )
    assert not before and after


def test_a_uniquely_named_inert_field_fired_on_BOTH_versions(old_gate, live_gate, tmp_path):
    """Both gates report a uniquely named inert field, isolating owner resolution."""
    schema = tmp_path / "_config_schema.py"
    schema.write_text(
        _COLLIDING_SCHEMA.replace(
            "class CodeHealthConfig:\n    enabled: bool = False",
            "class CodeHealthConfig:\n    uniquely_named_knob: bool = False",
        ),
        encoding="utf-8",
    )
    root = tmp_path / "srcroot"
    root.mkdir()
    (root / "consumer.py").write_text(_READS_ONLY_UI, encoding="utf-8")

    before = old_gate.check(schema, root)
    after = live_gate.check(schema, root)
    assert len(before) == 1 and "uniquely_named_knob" in before[0]
    assert len(after) == 1 and "uniquely_named_knob" in after[0]
