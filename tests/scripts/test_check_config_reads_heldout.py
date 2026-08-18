"""Held-out edge suite for the config-is-read gate (ticket 6754, corrected by bug 2c58).

Pins the OWNER-RESOLVED detection shapes (bug 2c58-e710-275e-4ff7), marker
placement/validation edges, plumbing exclusion, exit codes, the name-collision
contract, the real schema's string-read negative fixtures, CI wiring, and
tree-cleanliness of the real repo at landing.
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
_MAKEFILE = REPO_ROOT / "Makefile"


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
    """Bug 2c58: this test previously asserted the OPPOSITE and pinned the defect.

    It was written as an "accepted fail-quiet trade-off": a read of the same bare name
    on an unrelated object suppressed the firing. That is precisely the false-negative
    class this gate exists to prevent — it is why an inert `CodeHealthConfig.enabled`
    passed for as long as it did, while its uniquely-named sibling `analyzers` tripped
    the gate. Catching an inert field only when its name happens to be globally unique
    is luck, not a check. The expectation is therefore INVERTED: an unresolvable /
    unrelated receiver contributes to no field, so the field still fires.
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


# ── the real tree at landing ───────────────────────────────────────────────────────
@pytest.mark.repo_policy
def test_real_repo_is_clean_with_default_paths(gate):
    assert gate.main([]) == 0


@pytest.mark.repo_policy
def test_real_schema_markers_all_carry_pointers(gate):
    """Every `# read-via:` marker in the real schema must justify itself with a
    non-empty pointer/reason (bare markers are exactly what the gate rejects)."""
    text = _REAL_SCHEMA.read_text(encoding="utf-8")
    markers = re.findall(r"#\s*read-via:(.*)$", text, flags=re.MULTILINE)
    assert markers, "landing enumerated indirect fields — expected at least one marker"
    for pointer in markers:
        assert pointer.strip(), "bare read-via marker committed to the real schema"


# ── required negative fixtures: legitimate STRING-keyed reads (bug 2c58) ──────────
# An owning-dataclass resolver must still tolerate fields consumed through a string
# key or getattr, where no attribute read exists for any resolver to find. These are
# LIVE fields; a gate that fires on them would be a false positive that breaks CI.
# They stay green via their `# read-via:` markers, which the sweep must not disturb.
@pytest.mark.repo_policy
@pytest.mark.parametrize(
    ("field_name", "reader_hint"),
    [
        # getattr dispatch — _engine/rebar_reconciler/_advisory_lock.py
        ("lock_lease_secs", "getattr"),
        # gate_enabled(root, "<key>", ...) string keys — _commands/gates.py
        ("require_plan_review_for_close", "gate_enabled"),
        ("require_plan_review_for_claim", "string key"),
        ("require_completion_verification_for_close", "string key"),
    ],
)
def test_string_read_fields_keep_their_marker_and_do_not_fire(gate, field_name, reader_hint):
    text = _REAL_SCHEMA.read_text(encoding="utf-8")
    assert f"{field_name}:" in text, f"{field_name} must still exist in the real schema"
    # the field is protected by a marker (own line or the line immediately above)
    lines = text.splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.strip().startswith(f"{field_name}:"))
    window = "\n".join(lines[max(0, idx - 1) : idx + 1])
    assert "read-via:" in window, f"{field_name} lost its read-via marker"
    # and the real tree as a whole is clean, so it is not firing
    assert gate.check(_REAL_SCHEMA, REPO_ROOT / "src" / "rebar") == []


# ── ticket-pointer marker reporting (bug 2c58, AC7) ──────────────────────────────
# A marker that points at a TICKET rather than at a reader is justified only while that
# ticket is open -- `CodeHealthConfig.analyzers` carried `# read-via: inert pending bug
# dce2-...` and dce2 has since closed. The reporter surfaces those. Its correct output
# on the real tree is currently EMPTY, which is precisely why it needs a positive test:
# a reporter that has never emitted anything looks identical to one that cannot.
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


@pytest.mark.repo_policy
def test_the_real_schema_parks_no_markers_against_a_ticket(gate):
    """Today's expected state, pinned so a newly-parked marker surfaces in review.

    The schema used to park exactly two: task f020 deleted the inbound absence-probe port
    and with it the ONLY reader of ``JiraConfig.resolved_statuses``, then task 549c removed
    the write-only DC plumbing that had been the last read of
    ``ReconcilerConfig.resolved_statuses``, leaving both fields inert behind a marker
    pointing at the hard-removal follow-up f408-64ad-ee41-46b6. That removal has now landed
    under operator sign-off — the fields and their markers are gone — so the parked set is
    empty again. Pinned at EXACTLY zero rather than as a bare bound: a loosened assertion
    would let an unrelated field be parked against a ticket without anyone noticing, which
    is the whole point of reporting them.
    """
    reports = gate.ticket_pointer_reports(_REAL_SCHEMA)
    assert reports == [], f"no marker should be parked against a ticket; got {reports!r}"


@pytest.mark.repo_policy
def test_ci_wires_the_gate(gate):
    # RP-04 S7.2 (ticket 735b): the gate moved from a standalone workflow step into the
    # portable `make lint` target, which CI inherits — so assert the Makefile wires it.
    body = _MAKEFILE.read_text(encoding="utf-8")
    assert "check_config_reads.py" in body
