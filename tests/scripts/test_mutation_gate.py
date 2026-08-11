"""Contract tests for the targeted mutation-gate driver."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.scripts

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "mutation_gate.py"
MANIFEST = ROOT / ".github" / "mutation-shards.toml"


def _load():
    spec = importlib.util.spec_from_file_location("mutation_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_is_canonical_and_complete() -> None:
    gate = _load()
    manifest = gate.load_manifest(MANIFEST)

    assert set(manifest.shards) == {
        "signing",
        "reducer-processors",
        "next-batch",
        "gates",
        "validate",
        "compact-policy",
        "push-policy",
    }
    assert len({shard.source for shard in manifest.shards.values()}) == 7
    assert all(shard.tests for shard in manifest.shards.values())
    assert all(shard.score_floor > 0 for shard in manifest.shards.values())
    assert "scripts/mutation_gate.py" in manifest.global_support


def test_name_status_parser_keeps_both_sides_of_renames() -> None:
    gate = _load()
    changed = gate.parse_name_status(
        "M\ttests/unit/test_signing.py\n"
        "A\ttests/unit/test_new.py\n"
        "D\ttests/unit/test_old.py\n"
        "R100\ttests/unit/test_before.py\ttests/unit/test_after.py\n"
    )

    assert changed == {
        "tests/unit/test_signing.py",
        "tests/unit/test_new.py",
        "tests/unit/test_old.py",
        "tests/unit/test_before.py",
        "tests/unit/test_after.py",
    }


def test_selection_maps_source_tests_support_and_global_inputs(tmp_path: Path) -> None:
    gate = _load()
    manifest_path = tmp_path / "shards.toml"
    manifest_path.write_text(
        """
version = 1
global_support = ["uv.lock"]

[[shards]]
name = "alpha"
source = "src/alpha.py"
tests = ["tests/test_alpha.py"]
support = ["tests/conftest.py"]
survivors_max = 0
no_tests_max = 0
timeouts_max = 0
score_floor = 100.0
equivalent_fingerprints = []

[[shards]]
name = "beta"
source = "src/beta.py"
tests = ["tests/beta/"]
support = []
survivors_max = 0
no_tests_max = 0
timeouts_max = 0
score_floor = 100.0
equivalent_fingerprints = []
""".strip()
        + "\n"
    )
    manifest = gate.load_manifest(manifest_path)

    assert gate.select_shards(manifest, {"src/alpha.py"}).names == ("alpha",)
    assert gate.select_shards(manifest, {"tests/test_alpha.py"}).names == ("alpha",)
    assert gate.select_shards(manifest, {"tests/conftest.py"}).names == ("alpha",)
    assert gate.select_shards(manifest, {"tests/beta/test_one.py"}).names == ("beta",)
    assert gate.select_shards(manifest, {"uv.lock"}).names == ("alpha", "beta")


def test_selection_reports_unmatched_tests_and_empty_selection(tmp_path: Path) -> None:
    gate = _load()
    manifest_path = tmp_path / "shards.toml"
    manifest_path.write_text(
        """
version = 1
global_support = []
[[shards]]
name = "alpha"
source = "src/alpha.py"
tests = ["tests/test_alpha.py"]
support = []
survivors_max = 0
no_tests_max = 0
timeouts_max = 0
score_floor = 100.0
equivalent_fingerprints = []
""".strip()
        + "\n"
    )
    manifest = gate.load_manifest(manifest_path)

    selection = gate.select_shards(manifest, {"tests/test_other.py", "README.md"})
    assert selection.names == ()
    assert selection.unmatched_tests == ("tests/test_other.py",)


def test_results_parser_accepts_every_mutmut_370_status() -> None:
    gate = _load()
    statuses = [
        "killed",
        "survived",
        "no tests",
        "check was interrupted by user",
        "not checked",
        "skipped",
        "suspicious",
        "timeout",
        "caught by type check",
        "segfault",
    ]
    text = "\n".join(
        f"    pkg.x_f__mutmut_{index}: {status}" for index, status in enumerate(statuses)
    )

    parsed = gate.parse_results(text)
    assert list(parsed.values()) == statuses


def _shard(gate, **overrides):
    values = {
        "name": "demo",
        "source": "src/demo.py",
        "tests": ("tests/test_demo.py",),
        "support": (),
        "survivors_max": 0,
        "no_tests_max": 0,
        "timeouts_max": 0,
        "score_floor": 100.0,
        "equivalent_fingerprints": frozenset(),
    }
    values.update(overrides)
    return gate.Shard(**values)


def _run(gate, statuses, *, hashes=None, fingerprints=None):
    return gate.RunResults(
        statuses=statuses,
        unit_hashes=hashes or {},
        fingerprints=fingerprints or {},
    )


def test_unchanged_unit_blocks_killed_to_non_killed_transition() -> None:
    gate = _load()
    mutant = "pkg.x_f__mutmut_1"
    base = _run(gate, {mutant: "killed"}, hashes={"pkg.x_f": "same"})
    head = _run(
        gate,
        {mutant: "survived"},
        hashes={"pkg.x_f": "same"},
        fingerprints={mutant: "new"},
    )

    failures = gate.compare_runs(_shard(gate), base, head)
    assert "killed-regression" in {failure.code for failure in failures}


def test_unchanged_unit_blocks_a_missing_base_mutant() -> None:
    gate = _load()
    mutant = "pkg.x_f__mutmut_1"
    base = _run(gate, {mutant: "killed"}, hashes={"pkg.x_f": "same"})
    head = _run(gate, {}, hashes={"pkg.x_f": "same"})

    failures = gate.compare_runs(_shard(gate), base, head)
    assert "missing-mutant" in {failure.code for failure in failures}


def test_renamed_units_are_reported_and_use_head_budgets() -> None:
    gate = _load()
    base = _run(
        gate,
        {"pkg.x_foo__mutmut_1": "killed"},
        hashes={"pkg.x_foo": "old"},
    )
    head = _run(
        gate,
        {"pkg.x_bar__mutmut_1": "killed"},
        hashes={"pkg.x_bar": "new"},
    )

    comparison = gate.evaluate_runs(_shard(gate), base, head)
    assert comparison.failures == ()
    assert comparison.removed_units == ("pkg.x_foo",)
    assert comparison.added_units == ("pkg.x_bar",)


def test_known_equivalent_survivor_must_match_fingerprint_and_ceiling() -> None:
    gate = _load()
    mutant = "pkg.x_f__mutmut_1"
    head = _run(
        gate,
        {mutant: "survived", "pkg.x_f__mutmut_2": "killed"},
        hashes={"pkg.x_f": "changed"},
        fingerprints={mutant: "known"},
    )
    shard = _shard(
        gate,
        survivors_max=1,
        score_floor=50.0,
        equivalent_fingerprints=frozenset({"known"}),
    )
    assert gate.compare_runs(shard, _run(gate, {}), head) == ()

    unknown = _run(
        gate,
        head.statuses,
        hashes=head.unit_hashes,
        fingerprints={mutant: "unknown"},
    )
    failures = gate.compare_runs(shard, _run(gate, {}), unknown)
    assert "unknown-survivor" in {failure.code for failure in failures}


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ({}, "zero-mutants"),
        ({"pkg.x_f__mutmut_1": "not checked"}, "unexpected-status"),
        ({"pkg.x_f__mutmut_1": "no tests"}, "no-tests-budget"),
        ({"pkg.x_f__mutmut_1": "timeout"}, "timeout-budget"),
    ],
)
def test_head_budget_failures(statuses: dict[str, str], expected: str) -> None:
    gate = _load()
    failures = gate.compare_runs(_shard(gate), _run(gate, {}), _run(gate, statuses))
    assert expected in {failure.code for failure in failures}


def test_mutmut_config_is_generated_from_one_shard() -> None:
    gate = _load()
    config = gate.render_mutmut_config(_shard(gate), basetemp="/tmp/rebar-mut-demo")

    assert 'source_paths = ["src/rebar"]' in config
    assert 'only_mutate = ["src/demo.py"]' in config
    assert 'also_copy = [".github/"]' in config
    assert 'pytest_add_cli_args_test_selection = ["tests/test_demo.py"]' in config
    assert "timeout_constant = 1.0" in config
    assert "timeout_multiplier = 15.0" in config


def test_fingerprint_ignores_diff_locations_but_not_the_mutation() -> None:
    gate = _load()
    first = """--- demo.py\n+++ demo.py\n@@ -10,2 +10,2 @@\n-return x >= 2\n+return x > 2\n"""
    moved = """--- demo.py\n+++ demo.py\n@@ -80,2 +80,2 @@\n-return x >= 2\n+return x > 2\n"""
    different = """--- demo.py\n+++ demo.py\n@@ -10,2 +10,2 @@\n-return x >= 2\n+return x < 2\n"""

    assert gate.fingerprint_diff(first) == gate.fingerprint_diff(moved)
    assert gate.fingerprint_diff(first) != gate.fingerprint_diff(different)
