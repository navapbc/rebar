"""Contract tests for the targeted mutation-gate driver."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
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


def test_select_reports_the_git_diff_selection_as_stable_json(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        completed = subprocess.run(
            ("git", *args),
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    git("init", "--quiet")
    git("config", "user.name", "Mutation Gate Test")
    git("config", "user.email", "mutation-gate@example.test")
    (repo / "src").mkdir()
    (repo / "src" / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "src" / "beta.py").write_text("VALUE = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "--quiet", "-m", "base")
    base = git("rev-parse", "HEAD")

    (repo / "src" / "alpha.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_unmapped.py").write_text("def test_placeholder(): pass\n")
    git("add", ".")
    git("commit", "--quiet", "-m", "head")
    head = git("rev-parse", "HEAD")

    manifest = tmp_path / "mutation-shards.toml"
    manifest.write_text(
        """
version = 1
global_support = []

[[shards]]
name = "alpha"
mode = "advisory"
source = "src/alpha.py"
tests = ["tests/test_alpha.py"]
support = []
survivors_max = 0
no_tests_max = 0
timeouts_max = 0
score_floor = 100.0
equivalent_fingerprints = []

[[shards]]
name = "beta"
mode = "advisory"
source = "src/beta.py"
tests = ["tests/test_beta.py"]
support = []
survivors_max = 0
no_tests_max = 0
timeouts_max = 0
score_floor = 100.0
equivalent_fingerprints = []
""".strip()
        + "\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            sys.executable,
            str(SCRIPT),
            "select",
            "--repo",
            str(repo),
            "--manifest",
            str(manifest),
            "--base",
            base,
            "--head",
            head,
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "base": base,
        "head": head,
        "changed_paths": ["src/alpha.py", "tests/test_unmapped.py"],
        "selected_shards": ["alpha"],
        "unmatched_tests": ["tests/test_unmapped.py"],
        "empty_selection": False,
    }


def test_select_all_shards_overrides_an_empty_diff() -> None:
    completed = subprocess.run(
        (
            sys.executable,
            str(SCRIPT),
            "select",
            "--all-shards",
            "--repo",
            str(ROOT),
            "--manifest",
            str(MANIFEST),
            "--base",
            "HEAD",
            "--head",
            "HEAD",
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "base": "HEAD",
        "head": "HEAD",
        "changed_paths": [],
        "selected_shards": [
            "signing",
            "reducer-processors",
            "next-batch",
            "gates",
            "validate",
            "compact-policy",
            "push-policy",
        ],
        "unmatched_tests": [],
        "empty_selection": False,
    }


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


def test_all_manifest_shards_start_in_advisory_mode() -> None:
    gate = _load()
    manifest = gate.load_manifest(MANIFEST)

    assert all(shard.mode is gate.EnforcementMode.ADVISORY for shard in manifest.shards.values())


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
mode = "advisory"
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
mode = "advisory"
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
mode = "advisory"
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
        "mode": gate.EnforcementMode.ADVISORY,
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


def test_advisory_mode_runs_only_head_mutation_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _load()
    shard = _shard(gate, mode=gate.EnforcementMode.ADVISORY)
    manifest = gate.Manifest(shards={shard.name: shard}, global_support=())
    evidence_requests: list[tuple[str, str]] = []
    monkeypatch.setattr(gate, "load_manifest", lambda path: manifest)
    monkeypatch.setattr(gate, "changed_paths", lambda repo, base, head: {shard.source})
    monkeypatch.setattr(
        gate,
        "_extract_ref",
        lambda repo, ref, destination: evidence_requests.append(("archive", ref)),
    )

    def execute_shard(root, selected_shard, artifact_dir, label):
        evidence_requests.append(("execute", label))
        return _run(
            gate,
            {"pkg.demo_f__mutmut_1": "killed"},
            hashes={"pkg.demo_f": "head"},
        )

    monkeypatch.setattr(gate, "execute_shard", execute_shard)
    args = argparse.Namespace(
        repo=tmp_path,
        output_dir=tmp_path / "artifacts",
        manifest=tmp_path / "mutation-shards.toml",
        base="base-ref",
        head="head-ref",
        shard=(shard.name,),
        all_shards=False,
    )

    gate.run_gate(args)
    summary = json.loads((args.output_dir / "summary.json").read_text(encoding="utf-8"))
    shard_summary = summary["shards"][shard.name]

    assert (
        evidence_requests,
        shard_summary.get("base_collected"),
        shard_summary["base_counts"],
        shard_summary["removed_units"],
        shard_summary["added_units"],
    ) == (
        [("archive", "head-ref"), ("execute", "head")],
        False,
        None,
        [],
        [],
    )


def test_advisory_mode_reports_mutation_quality_without_blocking() -> None:
    gate = _load()
    quality_head = _run(
        gate,
        {
            "pkg.x_f__mutmut_1": "survived",
            "pkg.x_f__mutmut_2": "no tests",
            "pkg.x_f__mutmut_3": "timeout",
        },
    )
    incomplete_head = _run(gate, {"pkg.x_f__mutmut_4": "not checked"})
    empty_head = _run(gate, {})
    failure_codes = tuple(
        tuple(failure.code for failure in comparison.failures)
        for comparison in (
            gate.evaluate_runs(_shard(gate), _run(gate, {}), quality_head),
            gate.evaluate_runs(_shard(gate), _run(gate, {}), incomplete_head),
            gate.evaluate_runs(_shard(gate), _run(gate, {}), empty_head),
        )
    )

    assert failure_codes == ((), ("unexpected-status",), ("zero-mutants",))


def test_every_failure_code_declares_whether_it_blocks_advisory_mode() -> None:
    gate = _load()

    assert {code.value: code.blocks_advisory for code in gate.FailureCode} == {
        "zero-mutants": True,
        "unexpected-status": True,
        "survivor-budget": False,
        "unknown-survivor": False,
        "no-tests-budget": False,
        "timeout-budget": False,
        "score-budget": False,
        "missing-mutant": False,
        "killed-regression": False,
    }


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

    failures = gate.compare_runs(_shard(gate, mode=gate.EnforcementMode.RATCHET), base, head)
    assert "killed-regression" in {failure.code for failure in failures}


def test_unchanged_unit_blocks_a_missing_base_mutant() -> None:
    gate = _load()
    mutant = "pkg.x_f__mutmut_1"
    base = _run(gate, {mutant: "killed"}, hashes={"pkg.x_f": "same"})
    head = _run(gate, {}, hashes={"pkg.x_f": "same"})

    failures = gate.compare_runs(_shard(gate, mode=gate.EnforcementMode.RATCHET), base, head)
    assert "missing-mutant" in {failure.code for failure in failures}


def test_ratchet_grandfathers_legacy_debt_and_reports_new_no_tests() -> None:
    gate = _load()
    shard = _shard(
        gate,
        mode=gate.EnforcementMode.RATCHET,
        survivors_max=0,
        no_tests_max=0,
        score_floor=100.0,
    )
    hashes = {"pkg.x_f": "same"}
    killed = "pkg.x_f__mutmut_1"
    debt = "pkg.x_f__mutmut_2"
    legacy_survivor = gate.evaluate_runs(
        shard,
        _run(gate, {killed: "killed", debt: "survived"}, hashes=hashes),
        _run(
            gate,
            {killed: "killed", debt: "survived"},
            hashes=hashes,
            fingerprints={debt: "legacy"},
        ),
    )
    legacy_no_tests = gate.evaluate_runs(
        shard,
        _run(gate, {killed: "killed", debt: "no tests"}, hashes=hashes),
        _run(gate, {killed: "killed", debt: "no tests"}, hashes=hashes),
    )
    new_no_tests = gate.evaluate_runs(
        shard,
        _run(gate, {killed: "killed"}, hashes=hashes),
        _run(gate, {killed: "killed", debt: "no tests"}, hashes=hashes),
    )

    assert (
        (legacy_survivor.failures, legacy_survivor.advisories),
        (legacy_no_tests.failures, legacy_no_tests.advisories),
        (new_no_tests.failures, tuple(advisory.code for advisory in new_no_tests.advisories)),
    ) == (
        ((), ()),
        ((), ()),
        ((), (gate.FailureCode.NO_TESTS_BUDGET,)),
    )


def test_ratchet_blocks_new_survivors_and_timeouts_but_accepts_reviewed_equivalents() -> None:
    gate = _load()
    shard = _shard(
        gate,
        mode=gate.EnforcementMode.RATCHET,
        survivors_max=99,
        no_tests_max=99,
        timeouts_max=99,
        score_floor=1.0,
        equivalent_fingerprints=frozenset({"reviewed"}),
    )
    killed = "pkg.x_f__mutmut_1"
    new = "pkg.x_f__mutmut_2"
    stable_base = _run(gate, {killed: "killed"}, hashes={"pkg.x_f": "stable"})
    changed_base = _run(gate, {killed: "killed"}, hashes={"pkg.x_f": "before"})
    outcomes = tuple(
        tuple(failure.code for failure in comparison.failures)
        for comparison in (
            gate.evaluate_runs(
                shard,
                stable_base,
                _run(
                    gate,
                    {killed: "killed", new: "survived"},
                    hashes={"pkg.x_f": "stable"},
                    fingerprints={new: "unreviewed-stable"},
                ),
            ),
            gate.evaluate_runs(
                shard,
                changed_base,
                _run(
                    gate,
                    {killed: "killed", new: "survived"},
                    hashes={"pkg.x_f": "after"},
                    fingerprints={new: "unreviewed-changed"},
                ),
            ),
            gate.evaluate_runs(
                shard,
                changed_base,
                _run(
                    gate,
                    {killed: "killed", new: "survived"},
                    hashes={"pkg.x_f": "after"},
                    fingerprints={new: "reviewed"},
                ),
            ),
            gate.evaluate_runs(
                shard,
                stable_base,
                _run(
                    gate,
                    {killed: "killed", new: "timeout"},
                    hashes={"pkg.x_f": "stable"},
                ),
            ),
        )
    )

    assert outcomes == (
        (gate.FailureCode.UNKNOWN_SURVIVOR,),
        (gate.FailureCode.UNKNOWN_SURVIVOR,),
        (),
        (gate.FailureCode.TIMEOUT_BUDGET,),
    )


def test_strict_ignores_historical_budgets_and_requires_decisive_evidence() -> None:
    gate = _load()
    mutant = "pkg.x_f__mutmut_1"
    shard = _shard(
        gate,
        mode=gate.EnforcementMode.STRICT,
        survivors_max=0,
        no_tests_max=99,
        timeouts_max=99,
        score_floor=100.0,
        equivalent_fingerprints=frozenset({"reviewed"}),
    )
    outcomes = tuple(
        tuple(failure.code for failure in gate.compare_runs(shard, _run(gate, {}), head))
        for head in (
            _run(gate, {mutant: "killed"}),
            _run(gate, {mutant: "survived"}, fingerprints={mutant: "reviewed"}),
            _run(gate, {mutant: "survived"}, fingerprints={mutant: "unreviewed"}),
            _run(gate, {mutant: "no tests"}),
            _run(gate, {mutant: "timeout"}),
        )
    )

    assert outcomes == (
        (),
        (),
        (gate.FailureCode.UNKNOWN_SURVIVOR,),
        (gate.FailureCode.NO_TESTS_BUDGET,),
        (gate.FailureCode.TIMEOUT_BUDGET,),
    )


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

    comparison = gate.evaluate_runs(_shard(gate, mode=gate.EnforcementMode.RATCHET), base, head)
    assert comparison.failures == ()
    assert comparison.removed_units == ("pkg.x_foo",)
    assert comparison.added_units == ("pkg.x_bar",)


def test_known_equivalent_survivor_must_match_fingerprint() -> None:
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
        mode=gate.EnforcementMode.STRICT,
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
    shard = _shard(gate, mode=gate.EnforcementMode.STRICT)
    failures = gate.compare_runs(shard, _run(gate, {}), _run(gate, statuses))
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
