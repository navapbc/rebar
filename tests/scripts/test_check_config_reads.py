"""Check owner-resolved reads of config dataclass fields.

Each field needs an external attribute read on its declaring class or a
``read-via: <pointer>`` marker. Resolution follows sections, annotations,
aliases, returns, and cross-module calls; same-named fields on unrelated
classes do not count. ``check`` returns errors or an empty list; ``main``
returns 1 on failure and 0 when clean.

Held-out oracles merged from test_check_config_reads_heldout: Held-out edge suite for the
config-is-read gate (ticket 6754, corrected by bug 2c58).
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
    """Credit ``ui.enabled`` only to UiConfig, leaving CodeHealthConfig inert.

    Matching the bare name would wrongly satisfy both classes.
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


# ── held-out oracles (merged from test_check_config_reads_heldout) ──

REPO_ROOT = Path(__file__).resolve().parents[2]


_SCRIPT_HELDOUT = REPO_ROOT / "scripts" / "check_config_reads.py"


@pytest.fixture(scope="module")
def gate_heldout() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_config_reads_heldout", _SCRIPT_HELDOUT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SCHEMA = """\
from dataclasses import dataclass, field


@dataclass
class SectionConfig:
    ghost_knob: bool = False


@dataclass
class Config:
    # Marked so the section-wiring field never confounds an assertion about the knob.
    s: SectionConfig = field(default_factory=SectionConfig)  # read-via: section wiring
"""


_READS_SECTION_ONLY = "def f():\n    return None\n"


def _tree_heldout(tmp_path: Path, schema_body: str, reader_body: str) -> tuple[Path, Path]:
    schema = tmp_path / "_config_schema.py"
    schema.write_text(schema_body, encoding="utf-8")
    root = tmp_path / "srcroot"
    root.mkdir()
    (root / "consumer.py").write_text(reader_body, encoding="utf-8")
    return schema, root


@pytest.mark.parametrize(
    ("label", "reader"),
    [
        # (1) the section attribute name itself carries the owner
        ("section-name", "def f():\n    return load_config().s.ghost_knob\n"),
        # (2) an explicit annotation on the receiving parameter
        ("annotation", "def f(section_cfg: SectionConfig):\n    return section_cfg.ghost_knob\n"),
        # (3) a section name reached through an attribute chain on self
        ("self-chain", "class C:\n    def m(self):\n        return self.config.s.ghost_knob\n"),
        # (4) a local alias assigned from the section
        ("alias", "def f(cfg=None):\n    x = cfg.s\n    return x.ghost_knob\n"),
        # (5) an UNANNOTATED parameter bound from its call site — the real
        #     mcp_server.build_composite_verifier(mcp_cfg) pattern
        (
            "call-site-binding",
            "def helper(sect):\n    return sect.ghost_knob\n\n\n"
            "def caller(cfg):\n    return helper(cfg.s)\n",
        ),
        # (6) a function whose RETURN annotation names the owning class
        (
            "return-annotation",
            "def get_section() -> SectionConfig:\n    raise NotImplementedError\n\n\n"
            "def f():\n    return get_section().ghost_knob\n",
        ),
    ],
)
def test_every_real_receiver_shape_counts_as_a_read(gate_heldout, tmp_path, label, reader):
    schema, root = _tree_heldout(tmp_path, _SCHEMA, reader)
    assert gate_heldout.check(schema, root) == [], f"shape {label!r} must resolve to its owner"


def test_store_context_assignment_is_not_a_read(gate_heldout, tmp_path):
    schema, root = _tree_heldout(tmp_path, _SCHEMA, "def f(o):\n    o.ghost_knob = True\n")
    errors = gate_heldout.check(schema, root)
    assert len(errors) == 1 and "ghost_knob" in errors[0]


def test_unrelated_same_named_attribute_no_longer_satisfies_the_field(gate_heldout, tmp_path):
    """Ignore same-named reads on unrelated objects when attributing a config field.

    An inert field must still fail even when another object has that attribute name.
    """
    schema, root = _tree_heldout(
        tmp_path, _SCHEMA, "def f(unrelated):\n    return unrelated.ghost_knob\n"
    )
    errors = gate_heldout.check(schema, root)
    assert len(errors) == 1 and "ghost_knob" in errors[0]


def test_a_read_is_credited_to_exactly_one_owner(gate_heldout, tmp_path):
    """Two sections declaring the same bare field name: only the one actually read
    is satisfied. This is the owner-resolution contract in its smallest form."""
    body = """\
from dataclasses import dataclass, field


@dataclass
class LiveConfig:
    shared_name: bool = False


@dataclass
class InertConfig:
    shared_name: bool = False


@dataclass
class Config:
    live: LiveConfig = field(default_factory=LiveConfig)  # read-via: section wiring
    inert: InertConfig = field(default_factory=InertConfig)  # read-via: section wiring
"""
    schema, root = _tree_heldout(
        tmp_path, body, "def f(cfg: Config):\n    return cfg.live.shared_name\n"
    )
    errors = gate_heldout.check(schema, root)
    assert len(errors) == 1
    assert "InertConfig" in errors[0] and "LiveConfig" not in errors[0]


def test_marker_on_the_preceding_line_is_honoured(gate_heldout, tmp_path):
    body = _SCHEMA.replace(
        "    ghost_knob: bool = False",
        "    # read-via: getattr dispatch in consumer.py\n    ghost_knob: bool = False",
    )
    schema, root = _tree_heldout(tmp_path, body, _READS_SECTION_ONLY)
    assert gate_heldout.check(schema, root) == []


def test_whitespace_only_pointer_is_rejected_as_bare(gate_heldout, tmp_path):
    body = _SCHEMA.replace(
        "    ghost_knob: bool = False",
        "    ghost_knob: bool = False  # read-via:   ",
    )
    schema, root = _tree_heldout(tmp_path, body, _READS_SECTION_ONLY)
    errors = gate_heldout.check(schema, root)
    assert len(errors) == 1 and "ghost_knob" in errors[0]


def test_every_dead_field_is_reported_not_just_the_first(gate_heldout, tmp_path):
    body = _SCHEMA.replace(
        "    ghost_knob: bool = False",
        "    ghost_knob: bool = False\n    phantom_knob: int = 3",
    )
    schema, root = _tree_heldout(tmp_path, body, _READS_SECTION_ONLY)
    errors = gate_heldout.check(schema, root)
    assert len(errors) == 2
    joined = "\n".join(errors)
    assert "ghost_knob" in joined and "phantom_knob" in joined


def test_non_dataclass_annotations_are_not_collected(gate_heldout, tmp_path):
    body = "class Plain:\n    ghost_knob: bool = False\n"
    schema, root = _tree_heldout(tmp_path, body, _READS_SECTION_ONLY)
    assert gate_heldout.check(schema, root) == []


def test_reads_inside_config_plumbing_do_not_count(gate_heldout, tmp_path):
    schema = tmp_path / "_config_schema.py"
    schema.write_text(_SCHEMA, encoding="utf-8")
    root = tmp_path / "srcroot"
    root.mkdir()
    (root / "_config_schema.py").write_text(
        "def coerce(o):\n    return o.ghost_knob\n", encoding="utf-8"
    )
    (root / "config.py").write_text("def load(o):\n    return o.ghost_knob\n", encoding="utf-8")
    errors = gate_heldout.check(schema, root)
    assert len(errors) == 1 and "ghost_knob" in errors[0]


def test_main_exit_codes_and_output(gate_heldout, tmp_path, capsys):
    schema, root = _tree_heldout(tmp_path, _SCHEMA, _READS_SECTION_ONLY)
    rc = gate_heldout.main(["--schema", str(schema), "--root", str(root)])
    out = capsys.readouterr()
    combined = out.out + out.err
    assert rc == 1
    assert "ghost_knob" in combined
    assert "read-via" in combined, "failure output must teach the escape marker"

    schema2, root2 = _tree_heldout(
        (lambda p: (p.mkdir(), p)[1])(tmp_path / "clean"),
        _SCHEMA,
        "def f(c):\n    return c.s.ghost_knob\n",
    )
    rc2 = gate_heldout.main(["--schema", str(schema2), "--root", str(root2)])
    out2 = capsys.readouterr()
    assert rc2 == 0
    assert "OK" in (out2.out + out2.err)


_TICKET_MARKER_SCHEMA = """\
from dataclasses import dataclass, field


@dataclass
class SectionConfig:
    parked_knob: bool = False  # read-via: inert pending bug dce2-b93d-4112-451c
    real_reader_knob: bool = False  # read-via: consumer.py getattr dispatch


@dataclass
class Config:
    s: SectionConfig = field(default_factory=SectionConfig)  # read-via: section wiring
"""


def test_a_marker_pointing_at_a_ticket_is_reported(gate_heldout, tmp_path):
    schema, _root = _tree_heldout(tmp_path, _TICKET_MARKER_SCHEMA, _READS_SECTION_ONLY)
    reports = gate_heldout.ticket_pointer_reports(schema)
    assert len(reports) == 1, f"exactly the ticket-pointing marker reports; got {reports!r}"
    assert "parked_knob" in reports[0] and "SectionConfig" in reports[0]
    assert "dce2-b93d-4112-451c" in reports[0], "the report must name the ticket"
    assert "real_reader_knob" not in reports[0], "a reader pointer is not a ticket pointer"


def test_the_short_ticket_id_form_is_reported_too(gate_heldout, tmp_path):
    body = _TICKET_MARKER_SCHEMA.replace("dce2-b93d-4112-451c", "dce2-b93d")
    schema, _root = _tree_heldout(tmp_path, body, _READS_SECTION_ONLY)
    reports = gate_heldout.ticket_pointer_reports(schema)
    assert len(reports) == 1 and "dce2-b93d" in reports[0]


def test_ticket_pointer_reporting_never_changes_the_exit_code(gate_heldout, tmp_path, capsys):
    """The report is advisory: a parked marker is surfaced, not failed. Whether it is
    still justified depends on the ticket's state, which this gate cannot know."""
    schema, root = _tree_heldout(tmp_path, _TICKET_MARKER_SCHEMA, _READS_SECTION_ONLY)
    rc = gate_heldout.main(["--schema", str(schema), "--root", str(root)])
    combined = "".join(capsys.readouterr())
    assert rc == 0, "a ticket-pointing marker must not fail the gate"
    assert "dce2-b93d-4112-451c" in combined, "main must PRINT the report, not just compute it"
