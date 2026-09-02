"""Oracle for the mechanism-delta ratchet.

Ticket 9ca8-675e-4dfb-427d (unblacked-loveless-toad).

56% of sampled fixes add a new mechanism against 30% pure logic fixes, so the surface
that produces future defect classes grows every cycle. This is the counter-pressure: a
shrink-only ratchet over a committed per-``(kind, name)`` baseline, modelled on
``scripts/check_complexity_baseline.py`` — whose four-bucket ``compare()`` and
``has_regression`` are ported unchanged because they are proven.

Census counts are derived LIVE from the tree here, never hardcoded, following the
discipline ``tests/unit/test_complexity_baseline.py`` states for itself.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_mechanism_delta.py"
BASELINE = REPO_ROOT / ".github" / "mechanism-baseline.json"
MAKEFILE = REPO_ROOT / "Makefile"
LIMIT_FILE = REPO_ROOT / ".github" / "module-size-limit.txt"


def _load():
    spec = importlib.util.spec_from_file_location("check_mechanism_delta", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ratchet = _load()


def test_detect_all_returns_every_kind():
    """All seven kinds are detected, and each yields a non-empty name set on this tree."""
    found = ratchet.detect_all(REPO_ROOT)
    assert set(found) == set(ratchet.KINDS)
    for kind in ratchet.KINDS:
        assert found[kind], f"{kind} detected nothing on the live tree"


def test_baseline_round_trips_canonically():
    entries = {"lock::b.lock": 1, "lock::a.lock": 1}
    text = ratchet.render_baseline(entries)
    assert text.endswith("\n")
    parsed = json.loads(text)
    assert list(parsed["mechanisms"]) == sorted(parsed["mechanisms"])
    assert ratchet.parse_baseline(text) == entries


def test_compare_buckets_an_unchanged_tree_as_active():
    entries = {"lock::a.lock": 1, "env_var::REBAR_X": 1}
    counters = ratchet.compare(entries, entries)
    assert sorted(counters.active) == sorted(entries)
    assert counters.new == []
    assert counters.increased == []
    assert not counters.has_regression


def test_evaluate_passes_when_the_tree_matches_the_baseline():
    entries = {"lock::a.lock": 1}
    code, _ = ratchet.evaluate(entries, entries, {})
    assert code == 0


def test_check_passes_on_the_committed_tree(capsys):
    """The gate is green at rest, and says so with the complexity baseline's summary
    shape — a silent exit 0 cannot be told from a gate that scanned nothing."""
    assert ratchet.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "new=0" in out and "increased=0" in out, out


# ---------------------------------------------------------------------------
# the partition: one definition site, one entry
# ---------------------------------------------------------------------------


def test_the_seven_detector_name_sets_are_pairwise_disjoint():
    """AC1. If two detectors claim one site, that site needs two justifications and a
    per-kind marker cannot express it."""
    found = ratchet.detect_all(REPO_ROOT)
    kinds = sorted(found)
    for i, a in enumerate(kinds):
        for b in kinds[i + 1 :]:
            overlap = found[a] & found[b]
            assert overlap == set(), f"{a} and {b} both claim {sorted(overlap)[:5]}"


def test_config_key_and_feature_flag_split_the_config_surface():
    """AC2/AC3. Derived live: the boolean-coerced entries belong to feature_flag and the
    remainder to config_key, and together they account for every key exactly once."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from rebar._config_sections import _SECTIONS

    total = sum(len(keys) for keys in _SECTIONS.values())
    found = ratchet.detect_all(REPO_ROOT)
    assert len(found["config_key"]) + len(found["feature_flag"]) == total
    assert found["config_key"] & found["feature_flag"] == set()


def test_config_names_are_section_qualified_so_repeated_keys_do_not_collapse():
    """The fourth plan-review round caught this. `_SECTIONS` repeats key names across
    sections, so a bare-key baseline merges distinct definition sites: removing one
    section's key while the other persists would show no delta at all."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from rebar._config_sections import _SECTIONS

    repeated = {
        key
        for key in {k for keys in _SECTIONS.values() for k in keys}
        if sum(1 for keys in _SECTIONS.values() if key in keys) > 1
    }
    assert repeated, "expected at least one repeated key name to guard against"

    found = ratchet.detect_all(REPO_ROOT)
    names = found["config_key"] | found["feature_flag"]
    for key in repeated:
        owners = [s for s, keys in _SECTIONS.items() if key in keys]
        for section in owners:
            assert f"{section}.{key}" in names, (
                f"{section}.{key} is missing — a bare-key name would collapse "
                f"{len(owners)} definition sites of `{key}` into one entry"
            )


# ---------------------------------------------------------------------------
# a new mechanism of EVERY kind must fail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(ratchet.KINDS))
def test_a_new_mechanism_of_each_kind_fails(kind: str):
    """AC5. Seeded per kind against a baseline that lacks it."""
    baseline = {f"{kind}::already_known": 1}
    current = dict(baseline)
    current[f"{kind}::brand_new_thing"] = 1
    code, messages = ratchet.evaluate(current, baseline, {})
    assert code != 0, f"a new {kind} slipped through"
    assert any("brand_new_thing" in m for m in messages), messages


def test_the_failure_names_the_marker_as_the_remedy():
    baseline: dict[str, int] = {}
    code, messages = ratchet.evaluate({"lock::new.lock": 1}, baseline, {})
    assert code != 0
    blob = "\n".join(messages)
    assert ratchet.MARKER in blob, f"failure output should name the marker, got: {blob}"


# ---------------------------------------------------------------------------
# the justified-exception path
# ---------------------------------------------------------------------------


def test_a_marker_with_a_reason_admits_a_new_mechanism():
    """AC6."""
    markers = {"lock::new.lock": "bounded by the drain lock it replaces, ticket 9ca8"}
    code, _ = ratchet.evaluate({"lock::new.lock": 1}, {}, markers)
    assert code == 0


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_marker_without_a_real_reason_does_not_admit(reason: str):
    """AC9. A bare marker is itself an error — the discipline check_config_reads.py
    applies to its own `# read-via:` markers."""
    code, messages = ratchet.evaluate({"lock::new.lock": 1}, {}, {"lock::new.lock": reason})
    assert code != 0
    assert any("new.lock" in m for m in messages), messages


def test_a_marker_admits_only_the_mechanism_it_names():
    """A marker is not a blanket opt-out for its kind."""
    markers = {"lock::declared.lock": "declared for a reason"}
    current = {"lock::declared.lock": 1, "lock::undeclared.lock": 1}
    code, messages = ratchet.evaluate(current, {}, markers)
    assert code != 0
    assert any("undeclared.lock" in m for m in messages), messages


# ---------------------------------------------------------------------------
# the two marker placements that are NOT anchored to a Python def line
#
# The tests above drive `evaluate()` with synthetic keys, which proves the
# admission RULE but never exercises marker HARVESTING off a real tree. These
# two seed an actual repo root so the detector, the placement rule, and the
# admission all run end to end — the only way to cover the filename-glob and
# YAML-step shapes, whose markers do not sit on a definition line.
# ---------------------------------------------------------------------------


def test_a_marker_in_a_new_gate_scripts_head_admits_it(tmp_path):
    """AC7. Filename-glob shape: a glob match has no definition line to anchor to, so its
    marker lives anywhere in the file's first HEAD_LINES lines."""
    name = "scripts/check_seeded_gate.py"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    gate = scripts / "check_seeded_gate.py"

    def write(marker_line: str, pad: int = 0) -> None:
        body = ['"""A gate seeded by this test."""']
        body.extend(f"# filler {i}" for i in range(pad))
        if marker_line:
            body.append(marker_line)
        gate.write_text("\n".join(body) + "\n", encoding="utf-8")

    # Without a marker the gate is detected and unjustified — this is the control that
    # proves the admission below is the marker's doing, not the detector missing the file.
    write("")
    current = ratchet.census(tmp_path)
    assert f"ci_gate::{name}" in current, current
    code, messages = ratchet.evaluate(current, {}, ratchet.markers_for(tmp_path))
    assert code != 0, "an unjustified new gate script should fail"

    write(f"# mechanism-ok: ci_gate {name} — seeded by this test")
    code, messages = ratchet.evaluate(ratchet.census(tmp_path), {}, ratchet.markers_for(tmp_path))
    assert code == 0, messages

    # ...and the head window is a real bound, not decoration.
    write(f"# mechanism-ok: ci_gate {name} — seeded by this test", pad=ratchet_head_lines())
    code, messages = ratchet.evaluate(ratchet.census(tmp_path), {}, ratchet.markers_for(tmp_path))
    assert code != 0, "a marker past the head window should not admit"


def test_a_marker_above_a_workflow_run_step_admits_it(tmp_path):
    """AC8. YAML-step shape: the marker sits on the step's `- name:`/`run:` line or the one
    before it. The step name carries spaces, as every real workflow step's does."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    workflow = workflows / "seeded.yml"
    name = ".github/workflows/seeded.yml::seeded step"

    def write(marker_line: str) -> None:
        indent = "      "
        lines = ["jobs:", "  build:", "    steps:"]
        if marker_line:
            lines.append(f"{indent}{marker_line}")
        lines += [f"{indent}- name: seeded step", f"{indent}  run: echo hi"]
        workflow.write_text("\n".join(lines) + "\n", encoding="utf-8")

    write("")
    current = ratchet.census(tmp_path)
    assert f"ci_gate::{name}" in current, current
    code, _ = ratchet.evaluate(current, {}, ratchet.markers_for(tmp_path))
    assert code != 0, "an unjustified new workflow step should fail"

    write(f"# mechanism-ok: ci_gate {name} — seeded by this test")
    markers = ratchet.markers_for(tmp_path)
    assert f"ci_gate::{name}" in markers, (
        "the marker must be harvested under the step's FULL name; a name with a space that "
        f"gets truncated cannot ever be justified. harvested: {markers}"
    )
    code, messages = ratchet.evaluate(ratchet.census(tmp_path), {}, markers)
    assert code == 0, messages


def test_a_marker_admits_a_step_whose_name_carries_a_flag(tmp_path):
    """AC8, the sharp edge. An UNNAMED step is named for its `run:` snippet, so its name
    routinely contains a flag (`pytest -q`). The reason must therefore begin only at a
    separator with space on BOTH sides, or a name like this truncates at the flag and the
    step can never be justified."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    name = ".github/workflows/flagged.yml::pytest -q tests"
    (workflows / "flagged.yml").write_text(
        "jobs:\n  build:\n    steps:\n"
        f"      # mechanism-ok: ci_gate {name} — seeded by this test\n"
        "      - run: pytest -q tests\n",
        encoding="utf-8",
    )
    current = ratchet.census(tmp_path)
    assert f"ci_gate::{name}" in current, current
    markers = ratchet.markers_for(tmp_path)
    assert f"ci_gate::{name}" in markers, f"the name truncated at the flag; harvested: {markers}"
    code, messages = ratchet.evaluate(current, {}, markers)
    assert code == 0, messages


def ratchet_head_lines() -> int:
    """The filename-glob head window, read from the module rather than duplicated."""
    from scripts._mechanism_delta.markers import HEAD_LINES

    return HEAD_LINES + 1


# ---------------------------------------------------------------------------
# shrink-only semantics
# ---------------------------------------------------------------------------


def test_removing_a_mechanism_passes():
    """AC10. The ratchet is shrink-only: losing a mechanism is the improvement."""
    baseline = {"lock::a.lock": 1, "lock::b.lock": 1}
    code, _ = ratchet.evaluate({"lock::a.lock": 1}, baseline, {})
    assert code == 0


def test_a_removed_mechanism_is_reported_stale_not_new():
    baseline = {"lock::a.lock": 1, "lock::gone.lock": 1}
    counters = ratchet.compare({"lock::a.lock": 1}, baseline)
    assert "lock::gone.lock" in counters.stale
    assert counters.new == []
    assert not counters.has_regression


def test_update_stale_refuses_to_write_while_a_regression_stands(tmp_path, monkeypatch):
    """AC11. `--update-stale` is maintenance, never a way to bless a new mechanism."""
    baseline = tmp_path / "mechanism-baseline.json"
    original = ratchet.render_baseline({"lock::a.lock": 1})
    baseline.write_text(original)
    monkeypatch.setattr(ratchet, "BASELINE_PATH", str(baseline), raising=False)
    monkeypatch.setattr(
        ratchet, "detect_all", lambda root: {"lock": {"a.lock", "surprise.lock"}}, raising=False
    )
    rc = ratchet.main(["--update-stale"])
    assert rc != 0, "--update-stale must refuse while new>0"
    assert baseline.read_text() == original, "the baseline must be byte-identical after a refusal"


# ---------------------------------------------------------------------------
# the shipped artefacts
# ---------------------------------------------------------------------------


def test_the_committed_baseline_matches_the_live_tree():
    """AC4, from the other side: the baseline is locked to what is actually there."""
    baseline = ratchet.parse_baseline(BASELINE.read_text())
    live = {f"{k}::{n}" for k, names in ratchet.detect_all(REPO_ROOT).items() for n in names}
    assert set(baseline) == live


def test_make_lint_runs_the_ratchet():
    """AC12."""
    text = MAKEFILE.read_text()
    lint = text.split("lint:", 1)[1].split("\n\n", 1)[0]
    assert "check_mechanism_delta.py" in lint, "the ratchet is not wired into `make lint`"


def test_the_shipped_modules_respect_the_module_size_cap():
    """AC15. Neither repo limit-gate scans `scripts/`, so this suite asserts it."""
    limit = int(LIMIT_FILE.read_text().strip())
    shipped = [SCRIPT_PATH, *sorted((REPO_ROOT / "scripts" / "_mechanism_delta").glob("*.py"))]
    for path in shipped:
        n = len(path.read_text().splitlines())
        assert n <= limit, f"{path.name} is {n} lines, over the {limit} cap"


@pytest.mark.allow_unharnessed_subprocess(
    "lints the real committed scripts/ modules; a sandbox copy would assert nothing"
)
def test_the_shipped_modules_clear_the_complexity_ceiling():
    """AC16. The complexity ratchet scans `src/rebar` only, so this suite asserts it."""
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "C901",
            "scripts/check_mechanism_delta.py",
            "scripts/_mechanism_delta",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_docs_describe_every_kind_and_every_marker_placement():
    """AC13/AC14. A ratchet nobody can satisfy is a ratchet that gets disabled."""
    docs = "\n".join(
        p.read_text() for p in (REPO_ROOT / "docs").rglob("*.md") if "mechanism" in p.read_text()
    )
    assert docs, "no doc mentions the mechanism ratchet"
    for kind in ratchet.KINDS:
        assert kind in docs, f"docs do not name the `{kind}` kind"
    for shape in ("definition line", "string literal", "filename glob", "YAML step"):
        assert shape in docs, f"docs do not name the `{shape}` marker placement"
