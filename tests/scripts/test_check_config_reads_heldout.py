"""Held-out edge suite for the config-is-read gate (ticket 6754, corrected by bug 2c58).

Pins the OWNER-RESOLVED detection shapes (bug 2c58-e710-275e-4ff7), marker
placement/validation edges, plumbing exclusion, exit codes, the name-collision
contract, the real schema's string-read negative fixtures, CI wiring, and
tree-cleanliness of the real repo at landing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.scripts

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "check_config_reads.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_config_reads_heldout", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The root ``Config`` binding is what makes owner resolution possible: ``Config.s:
# SectionConfig`` is the edge that tells the gate a read off ``<expr>.s`` belongs to
# ``SectionConfig``.
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


# The baseline reader for the "this knob is unread" edges below: it touches no knob.
_READS_SECTION_ONLY = "def f():\n    return None\n"


def _tree(tmp_path: Path, schema_body: str, reader_body: str) -> tuple[Path, Path]:
    schema = tmp_path / "_config_schema.py"
    schema.write_text(schema_body, encoding="utf-8")
    root = tmp_path / "srcroot"
    root.mkdir()
    (root / "consumer.py").write_text(reader_body, encoding="utf-8")
    return schema, root


# ── owner-resolvable receiver shapes (bug 2c58) ──────────────────────────────────
# Each shape is a real pattern in src/rebar. Owner resolution must bind the receiver
# through a DIFFERENT route in each case; a resolver that only understands one of
# them would false-fire on live fields and break CI.
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
def test_every_real_receiver_shape_counts_as_a_read(gate, tmp_path, label, reader):
    schema, root = _tree(tmp_path, _SCHEMA, reader)
    assert gate.check(schema, root) == [], f"shape {label!r} must resolve to its owner"


def test_store_context_assignment_is_not_a_read(gate, tmp_path):
    schema, root = _tree(tmp_path, _SCHEMA, "def f(o):\n    o.ghost_knob = True\n")
    errors = gate.check(schema, root)
    assert len(errors) == 1 and "ghost_knob" in errors[0]


def test_unrelated_same_named_attribute_no_longer_satisfies_the_field(gate, tmp_path):
    """Ignore same-named reads on unrelated objects when attributing a config field.

    An inert field must still fail even when another object has that attribute name.
    """
    schema, root = _tree(tmp_path, _SCHEMA, "def f(unrelated):\n    return unrelated.ghost_knob\n")
    errors = gate.check(schema, root)
    assert len(errors) == 1 and "ghost_knob" in errors[0]


def test_a_read_is_credited_to_exactly_one_owner(gate, tmp_path):
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
    schema, root = _tree(tmp_path, body, "def f(cfg: Config):\n    return cfg.live.shared_name\n")
    errors = gate.check(schema, root)
    assert len(errors) == 1
    assert "InertConfig" in errors[0] and "LiveConfig" not in errors[0]


# ── marker placement + validation edges ──────────────────────────────────────────
def test_marker_on_the_preceding_line_is_honoured(gate, tmp_path):
    body = _SCHEMA.replace(
        "    ghost_knob: bool = False",
        "    # read-via: getattr dispatch in consumer.py\n    ghost_knob: bool = False",
    )
    schema, root = _tree(tmp_path, body, _READS_SECTION_ONLY)
    assert gate.check(schema, root) == []


def test_whitespace_only_pointer_is_rejected_as_bare(gate, tmp_path):
    body = _SCHEMA.replace(
        "    ghost_knob: bool = False",
        "    ghost_knob: bool = False  # read-via:   ",
    )
    schema, root = _tree(tmp_path, body, _READS_SECTION_ONLY)
    errors = gate.check(schema, root)
    assert len(errors) == 1 and "ghost_knob" in errors[0]


def test_every_dead_field_is_reported_not_just_the_first(gate, tmp_path):
    body = _SCHEMA.replace(
        "    ghost_knob: bool = False",
        "    ghost_knob: bool = False\n    phantom_knob: int = 3",
    )
    schema, root = _tree(tmp_path, body, _READS_SECTION_ONLY)
    errors = gate.check(schema, root)
    assert len(errors) == 2
    joined = "\n".join(errors)
    assert "ghost_knob" in joined and "phantom_knob" in joined


def test_non_dataclass_annotations_are_not_collected(gate, tmp_path):
    body = "class Plain:\n    ghost_knob: bool = False\n"
    schema, root = _tree(tmp_path, body, _READS_SECTION_ONLY)
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
    schema, root = _tree(tmp_path, _SCHEMA, _READS_SECTION_ONLY)
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


# Ticket-pointer reports do not query ticket status. The next lines show sample marker text.
# ticket is open -- `CodeHealthConfig.analyzers` carried `# read-via: inert pending bug
# dce2-...`. The fixture checks reporting when the repository schema has none.
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


def test_a_marker_pointing_at_a_ticket_is_reported(gate, tmp_path):
    schema, _root = _tree(tmp_path, _TICKET_MARKER_SCHEMA, _READS_SECTION_ONLY)
    reports = gate.ticket_pointer_reports(schema)
    assert len(reports) == 1, f"exactly the ticket-pointing marker reports; got {reports!r}"
    assert "parked_knob" in reports[0] and "SectionConfig" in reports[0]
    assert "dce2-b93d-4112-451c" in reports[0], "the report must name the ticket"
    assert "real_reader_knob" not in reports[0], "a reader pointer is not a ticket pointer"


def test_the_short_ticket_id_form_is_reported_too(gate, tmp_path):
    body = _TICKET_MARKER_SCHEMA.replace("dce2-b93d-4112-451c", "dce2-b93d")
    schema, _root = _tree(tmp_path, body, _READS_SECTION_ONLY)
    reports = gate.ticket_pointer_reports(schema)
    assert len(reports) == 1 and "dce2-b93d" in reports[0]


def test_ticket_pointer_reporting_never_changes_the_exit_code(gate, tmp_path, capsys):
    """The report is advisory: a parked marker is surfaced, not failed. Whether it is
    still justified depends on the ticket's state, which this gate cannot know."""
    schema, root = _tree(tmp_path, _TICKET_MARKER_SCHEMA, _READS_SECTION_ONLY)
    rc = gate.main(["--schema", str(schema), "--root", str(root)])
    combined = "".join(capsys.readouterr())
    assert rc == 0, "a ticket-pointing marker must not fail the gate"
    assert "dce2-b93d-4112-451c" in combined, "main must PRINT the report, not just compute it"
