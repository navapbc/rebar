"""Contract tests for the targeted mutation-gate driver."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path

import pytest
import tomllib

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
timeout_constant = 1.0
timeout_multiplier = 15.0
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
timeout_constant = 1.0
timeout_multiplier = 15.0
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


def test_manifest_conftest_support_matches_selected_test_ancestry() -> None:
    gate = _load()
    manifest = gate.load_manifest(MANIFEST)
    tests_root = ROOT / "tests"
    expected_shard_inputs = {
        "signing": (
            "src/rebar/signing.py",
            ("tests/unit/test_signing.py", "tests/interfaces/lifecycle/test_signature.py"),
        ),
        "reducer-processors": (
            "src/rebar/reducer/_processors.py",
            ("tests/scripts/reducer/", "tests/interfaces/store/test_reducer_single_source.py"),
        ),
        "next-batch": (
            "src/rebar/_engine_support/next_batch.py",
            (
                "tests/interfaces/queries/test_next_batch_compute.py",
                "tests/interfaces/queries/test_next_batch_behavior.py",
            ),
        ),
        "gates": (
            "src/rebar/_engine_support/gates.py",
            (
                "tests/interfaces/lifecycle/test_gate_rubric_consistency.py",
                "tests/interfaces/queries/test_ws5d_quality_fileimpact.py",
                "tests/interfaces/lifecycle/test_close_gate_story_epic.py",
            ),
        ),
        "validate": (
            "src/rebar/_engine_support/validate.py",
            ("tests/interfaces/queries/test_validate_compute.py",),
        ),
        "compact-policy": (
            "src/rebar/_commands/_compact_policy.py",
            ("tests/unit/test_compact_policy.py",),
        ),
        "push-policy": (
            "src/rebar/_store/_push_policy.py",
            ("tests/unit/test_push_policy.py",),
        ),
    }
    expected_conftest_owners = {
        "tests/unit/conftest.py": frozenset({"signing", "compact-policy", "push-policy"}),
        "tests/interfaces/conftest.py": frozenset(
            {"signing", "reducer-processors", "gates", "next-batch", "validate"}
        ),
        "tests/interfaces/store/conftest.py": frozenset({"reducer-processors"}),
        "tests/scripts/conftest.py": frozenset({"reducer-processors"}),
        "tests/scripts/reducer/conftest.py": frozenset({"reducer-processors"}),
    }
    derived_conftest_owners: dict[str, set[str]] = {}
    for name, shard in manifest.shards.items():
        selected_tests: list[Path] = []
        for configured_test in shard.tests:
            selected_path = ROOT / configured_test
            selected_tests.extend(
                sorted(selected_path.rglob("test_*.py"))
                if selected_path.is_dir()
                else [selected_path]
            )
        for selected_test in selected_tests:
            for directory in selected_test.parents:
                if directory == tests_root:
                    break
                conftest = directory / "conftest.py"
                if conftest.is_file():
                    relative = conftest.relative_to(ROOT).as_posix()
                    derived_conftest_owners.setdefault(relative, set()).add(name)

    manifest_conftest_owners: dict[str, set[str]] = {}
    for name, shard in manifest.shards.items():
        for support in shard.support:
            if support.endswith("conftest.py"):
                manifest_conftest_owners.setdefault(support, set()).add(name)

    unrelated_conftest = "tests/external/live_jira_dc/conftest.py"
    selector_paths = (
        unrelated_conftest,
        *expected_conftest_owners,
        "tests/conftest.py",
    )
    actual = {
        "global_support": manifest.global_support,
        "shard_inputs": {
            name: (shard.source, shard.tests) for name, shard in manifest.shards.items()
        },
        "non_conftest_shard_support": {
            name: tuple(path for path in shard.support if not path.endswith("conftest.py"))
            for name, shard in manifest.shards.items()
        },
        "derived_conftest_owners": {
            path: frozenset(owners) for path, owners in derived_conftest_owners.items()
        },
        "manifest_conftest_owners": {
            path: frozenset(owners) for path, owners in manifest_conftest_owners.items()
        },
        "selector_owners": {
            path: frozenset(gate.select_shards(manifest, {path}).names) for path in selector_paths
        },
    }
    expected = {
        "global_support": (
            ".github/mutation-shards.toml",
            ".github/workflows/_mutation.yml",
            ".github/workflows/mutation.yml",
            ".github/workflows/gerrit-verify.yaml",
            ".github/workflows/test.yml",
            "pyproject.toml",
            "scripts/mutation_gate.py",
            "tests/conftest.py",
            "uv.lock",
        ),
        "shard_inputs": expected_shard_inputs,
        "non_conftest_shard_support": {name: () for name in expected_shard_inputs},
        "derived_conftest_owners": expected_conftest_owners,
        "manifest_conftest_owners": expected_conftest_owners,
        "selector_owners": {
            unrelated_conftest: frozenset(),
            **expected_conftest_owners,
            "tests/conftest.py": frozenset(expected_shard_inputs),
        },
    }

    assert actual == expected


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
timeout_constant = 1.0
timeout_multiplier = 15.0
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
timeout_constant = 1.0
timeout_multiplier = 15.0
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
timeout_constant = 1.0
timeout_multiplier = 15.0
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


def test_ratchet_serializes_new_no_tests_advisory_without_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _load()
    shard = _shard(gate, mode=gate.EnforcementMode.RATCHET)
    manifest = gate.Manifest(shards={shard.name: shard}, global_support=())
    mutant = "pkg.demo_f__mutmut_1"
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
        statuses = {} if label == "base" else {mutant: "no tests"}
        return _run(gate, statuses, hashes={"pkg.demo_f": label})

    monkeypatch.setattr(gate, "execute_shard", execute_shard)
    args = argparse.Namespace(
        repo=tmp_path,
        output_dir=tmp_path / "artifacts",
        manifest=tmp_path / "mutation-shards.toml",
        base="base-ref",
        head="head-ref",
        shard=(),
        all_shards=False,
    )

    gate.run_gate(args)
    summary = json.loads((args.output_dir / "summary.json").read_text(encoding="utf-8"))
    shard_summary = summary["shards"][shard.name]

    assert (
        evidence_requests,
        summary["outcome"],
        shard_summary["failures"],
        shard_summary["advisories"],
    ) == (
        [
            ("archive", "base-ref"),
            ("archive", "head-ref"),
            ("execute", "base"),
            ("execute", "head"),
        ],
        "success",
        [],
        [
            asdict(
                gate.Failure(
                    gate.FailureCode.NO_TESTS_BUDGET,
                    f"{mutant}: no tests",
                )
            )
        ],
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


def test_manifest_timeout_policy_drives_generated_mutmut_config(tmp_path: Path) -> None:
    gate = _load()
    canonical_text = MANIFEST.read_text(encoding="utf-8")
    manifest_chunks = canonical_text.split("[[shards]]")
    signing_index = next(
        index
        for index, chunk in enumerate(manifest_chunks[1:], start=1)
        if tomllib.loads("[[shards]]" + chunk)["shards"][0]["name"] == "signing"
    )
    signing_block = manifest_chunks[signing_index]
    replacement_counts = (
        signing_block.count("timeout_constant = 1.0"),
        signing_block.count("timeout_multiplier = 15.0"),
    )
    manifest_chunks[signing_index] = signing_block.replace(
        "timeout_constant = 1.0", "timeout_constant = 2.5"
    ).replace("timeout_multiplier = 15.0", "timeout_multiplier = 9.0")
    probe_text = "[[shards]]".join(manifest_chunks)
    probe_path = tmp_path / "mutation-shards.toml"
    probe_path.write_text(probe_text, encoding="utf-8")

    canonical_manifest = tomllib.loads(canonical_text)
    probe_manifest = tomllib.loads(probe_text)
    canonical_signing = next(
        row for row in canonical_manifest["shards"] if row["name"] == "signing"
    )
    probe_signing = next(row for row in probe_manifest["shards"] if row["name"] == "signing")
    canonical_shard = gate.load_manifest(MANIFEST).shards["signing"]
    probe_shard = gate.load_manifest(probe_path).shards["signing"]
    canonical_config = tomllib.loads(
        gate.render_mutmut_config(canonical_shard, basetemp="/tmp/canonical")
    )
    probe_config = tomllib.loads(gate.render_mutmut_config(probe_shard, basetemp="/tmp/probe"))
    repository_config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    driver_tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    driver_functions = {
        node.name: node for node in driver_tree.body if isinstance(node, ast.FunctionDef)
    }

    def mutmut_run_concurrency(
        function_name: str,
    ) -> tuple[tuple[tuple[str, str | None], ...], ...]:
        calls = []
        for node in ast.walk(driver_functions[function_name]):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_run"
                and node.args
                and isinstance(node.args[0], ast.Tuple)
            ):
                continue
            literal_args = tuple(
                element.value
                for element in node.args[0].elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
            if literal_args[:3] != ("-m", "mutmut", "run"):
                continue
            calls.append(
                tuple(
                    (
                        argument,
                        literal_args[index + 1] if index + 1 < len(literal_args) else None,
                    )
                    for index, argument in enumerate(literal_args)
                    if argument == "--max-children"
                )
            )
        return tuple(calls)

    assert (
        replacement_counts,
        (canonical_signing.get("timeout_constant"), canonical_signing.get("timeout_multiplier")),
        (probe_signing.get("timeout_constant"), probe_signing.get("timeout_multiplier")),
        (
            canonical_config["tool"]["mutmut"]["timeout_constant"],
            canonical_config["tool"]["mutmut"]["timeout_multiplier"],
        ),
        (
            probe_config["tool"]["mutmut"]["timeout_constant"],
            probe_config["tool"]["mutmut"]["timeout_multiplier"],
        ),
        "mutmut" in repository_config.get("tool", {}),
        {
            "main": mutmut_run_concurrency("execute_shard"),
            "diagnostic": mutmut_run_concurrency("_diagnose_non_killed"),
        },
    ) == (
        (1, 1),
        (1.0, 15.0),
        (2.5, 9.0),
        (1.0, 15.0),
        (2.5, 9.0),
        False,
        {
            "main": ((("--max-children", "1"),),),
            "diagnostic": ((("--max-children", "1"),),),
        },
    )


@pytest.mark.parametrize(
    ("semantic_type_name", "candidate"),
    [
        pytest.param("TimeoutConstant", 0, id="constant-zero"),
        pytest.param("TimeoutConstant", -1.0, id="constant-negative"),
        pytest.param("TimeoutConstant", True, id="constant-bool"),
        pytest.param("TimeoutConstant", "1.0", id="constant-string"),
        pytest.param("TimeoutConstant", float("nan"), id="constant-nan"),
        pytest.param("TimeoutConstant", float("inf"), id="constant-positive-infinity"),
        pytest.param("TimeoutConstant", float("-inf"), id="constant-negative-infinity"),
        pytest.param("TimeoutMultiplier", 0, id="multiplier-zero"),
        pytest.param("TimeoutMultiplier", -1.0, id="multiplier-negative"),
        pytest.param("TimeoutMultiplier", True, id="multiplier-bool"),
        pytest.param("TimeoutMultiplier", "1.0", id="multiplier-string"),
        pytest.param("TimeoutMultiplier", float("nan"), id="multiplier-nan"),
        pytest.param("TimeoutMultiplier", float("inf"), id="multiplier-positive-infinity"),
        pytest.param("TimeoutMultiplier", float("-inf"), id="multiplier-negative-infinity"),
    ],
)
def test_timeout_policy_value_objects_reject_invalid_values(
    semantic_type_name: str, candidate: object
) -> None:
    gate = _load()
    semantic_type = getattr(gate, semantic_type_name)

    with pytest.raises(gate.GateError):
        semantic_type(candidate)


def test_fingerprint_ignores_diff_locations_but_not_the_mutation() -> None:
    gate = _load()
    first = """--- demo.py\n+++ demo.py\n@@ -10,2 +10,2 @@\n-return x >= 2\n+return x > 2\n"""
    moved = """--- demo.py\n+++ demo.py\n@@ -80,2 +80,2 @@\n-return x >= 2\n+return x > 2\n"""
    different = """--- demo.py\n+++ demo.py\n@@ -10,2 +10,2 @@\n-return x >= 2\n+return x < 2\n"""

    assert gate.fingerprint_diff(first) == gate.fingerprint_diff(moved)
    assert gate.fingerprint_diff(first) != gate.fingerprint_diff(different)


def test_mutation_testing_docs_define_phased_policy_without_legacy_budgets() -> None:
    markdown = (ROOT / "docs" / "mutation-testing.md").read_text(encoding="utf-8")

    def normalize(value: str) -> str:
        return re.sub(
            r"\s+",
            " ",
            value.lower().replace("‑", "-").replace("–", "-").replace("`", ""),
        ).strip()

    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    heading_stack: list[tuple[int, str]] = []
    heading_titles: list[str] = []
    section_lines: dict[tuple[str, ...], list[str]] = {}
    current_path: tuple[str, ...] = ()
    for line in markdown.splitlines():
        heading = heading_pattern.match(line)
        if heading:
            level = len(heading.group(1))
            title = normalize(heading.group(2).strip("`*_"))
            heading_titles.append(title)
            if level == 1:
                heading_stack = []
                current_path = ()
                continue
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            current_path = tuple(item[1] for item in heading_stack)
            section_lines.setdefault(current_path, [])
        elif current_path:
            section_lines[current_path].append(line)

    def section(*path: str) -> str:
        return normalize("\n".join(section_lines.get(path, [])))

    def section_tree(*path: str) -> str:
        prefix = tuple(path)
        return normalize(
            "\n".join(
                "\n".join(lines)
                for candidate, lines in section_lines.items()
                if candidate[: len(prefix)] == prefix
            )
        )

    def contains_all(text: str, *phrases: str) -> bool:
        return all(phrase in text for phrase in phrases)

    advisory = section("enforcement modes", "advisory")
    ratchet = section("enforcement modes", "ratchet")
    strict = section("enforcement modes", "strict")
    enforcement = section_tree("enforcement modes")
    manifest = section("manifest authority")
    promotion = section("promotion evidence")
    selection = section("selection and ci cadence")
    fresh = section("fresh execution and evidence")
    diagnostics = section("false positives and nondeterminism")
    local_use = section("local use")
    recovery = section("emergency recovery")
    normative_policy = normalize("\n".join((manifest, enforcement, promotion, selection, fresh)))
    timeout_authority_policy = normalize("\n".join((manifest, fresh)))

    stale_score_budget_heading_patterns = (
        re.compile(
            r"^(?:historical\s+)?(?:mapped\s+|mutation\s+)?scores?"
            r"(?:\s+budgets?)?(?:\s*\([^)]*\))?$"
        ),
        re.compile(r"^(?:historical\s+)?(?:score\s+)?budgets?(?:\s*\([^)]*\))?$"),
        re.compile(r"^(?:score|budget)\s+history(?:\s*\([^)]*\))?$"),
    )
    stale_score_budget_headings = {
        title
        for title in heading_titles
        if any(pattern.fullmatch(title) for pattern in stale_score_budget_heading_patterns)
    }

    def markdown_table_cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    def is_markdown_table_separator(line: str) -> bool:
        cells = markdown_table_cells(line)
        return len(cells) >= 2 and all(
            re.fullmatch(r":?-{3,}:?", cell) is not None for cell in cells
        )

    policy_measurement_column_patterns = (
        re.compile(r"\b(?:mapped|mutation)\s+score\b"),
        re.compile(r"\bsurvivors?\s+(?:ceiling|max(?:imum)?)\b"),
        re.compile(r"\bno[\s-]*tests?\s+(?:ceiling|max(?:imum)?)\b"),
        re.compile(r"\btimeouts?\s+(?:ceiling|max(?:imum)?)\b"),
        re.compile(r"\bbudgets?\b"),
    )
    markdown_lines = markdown.splitlines()
    stale_policy_table_headers: set[str] = set()
    for header_line, separator_line in pairwise(markdown_lines):
        header_cells = markdown_table_cells(header_line)
        separator_cells = markdown_table_cells(separator_line)
        if (
            "|" not in header_line
            or not is_markdown_table_separator(separator_line)
            or len(header_cells) != len(separator_cells)
        ):
            continue
        normalized_cells = [normalize(cell.strip("`*_")) for cell in header_cells]
        if any(
            pattern.search(cell)
            for cell in normalized_cells
            for pattern in policy_measurement_column_patterns
        ):
            stale_policy_table_headers.add(" | ".join(normalized_cells))

    required_paths = {
        ("manifest authority",),
        ("enforcement modes",),
        ("enforcement modes", "advisory"),
        ("enforcement modes", "ratchet"),
        ("enforcement modes", "strict"),
        ("promotion evidence",),
        ("selection and ci cadence",),
        ("fresh execution and evidence",),
        ("false positives and nondeterminism",),
        ("local use",),
        ("emergency recovery",),
    }
    legacy_field_identifiers = {
        "score_floor",
        "survivors_max",
        "no_tests_max",
        "timeouts_max",
    }
    legacy_ceiling_phrases = {
        "score/count ceilings",
        "head shard budgets",
        "survivor ceiling",
        "no tests has a separate ceiling",
        "timeouts have a zero ceiling",
    }
    copied_timeout_patterns = {
        "copied per-mutant timeout formula": re.compile(
            r"(?:\+|plus)\s*(?:1|one)\s*seconds?[\s),;:]*"
            r"(?:times|x|×|\*)\s*(?:15|fifteen)\b"
        ),
        "direct numeric timeout field assignment": re.compile(
            r"\btimeout[_ -]?(?:constant|multiplier)\b\s*(?:=|:|\bis\b)\s*"
            r"(?:\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|"
            r"ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
            r"eighteen|nineteen|twenty|thirty)\b"
        ),
    }
    requirements = {
        **{
            f"heading path {' > '.join(path)!r} exists": path in section_lines
            for path in required_paths
        },
        "manifest names the authoritative shard manifest and its policy inputs": contains_all(
            manifest,
            ".github/mutation-shards.toml",
            "authoritative",
            "source",
            "tests",
            "support",
            "mode",
            "equivalent fingerprints",
            "timeout",
        ),
        "advisory is head-only and preserves non-blocking observable quality evidence": (
            contains_all(
                advisory,
                "head-only",
                "survived",
                "no tests",
                "timeout",
                "non-blocking",
                "counts",
                "raw evidence",
                "survivor fingerprints",
            )
            and re.search(r"not serialized (?:(?:as|into) (?:comparison )?)?findings", advisory)
            is not None
        ),
        "advisory keeps an unreviewed parsed survivor non-blocking": (
            contains_all(advisory, "parsed", "without", "reviewed", "fingerprint", "non-blocking")
            and ("survived" in advisory or "survivor" in advisory)
        ),
        "advisory blocks missing/zero evidence and non-decisive statuses": contains_all(
            advisory,
            "zero mutants",
            "missing evidence",
            "non-decisive",
            "unrecognized status",
            "configuration failure",
            "execution failure",
            "block",
        ),
        "ratchet collects fresh base/head and only grandfathers AST-identical legacy debt": (
            contains_all(
                ratchet,
                "fresh",
                "base and head",
                "ast-identical",
                "legacy survivor",
                "legacy no tests",
                "grandfathered",
                "only",
            )
        ),
        "ratchet blocks every head timeout": (
            contains_all(ratchet, "head timeout", "block")
            and ("every head timeout" in ratchet or "all head timeouts" in ratchet)
        ),
        "ratchet blocks new survivors, incomplete evidence, and killed/missing regressions": (
            contains_all(
                ratchet,
                "new survivor",
                "incomplete",
                "killed regression",
                "missing killed",
                "block",
            )
        ),
        "ratchet keeps new no-tests advisory": contains_all(ratchet, "new no tests", "advisory"),
        "ratchet permits only the reviewed equivalent-fingerprint exception": contains_all(
            ratchet, "reviewed equivalent fingerprint", "exception"
        ),
        "strict requires every result to be killed or a reviewed survivor": (
            contains_all(
                strict,
                "every result",
                "killed",
                "survived",
                "reviewed equivalent fingerprint",
                "only",
                "no tests",
                "timeout",
                "incomplete",
                "zero mutants",
                "block",
            )
        ),
        "strict preserves AST-identical base-killed mutants despite reviewed fingerprints": (
            contains_all(
                strict,
                "ast-identical",
                "killed on base",
                "remain killed",
                "reviewed equivalent fingerprint",
            )
            and (
                "even if" in strict or "cannot override" in strict or "does not override" in strict
            )
        ),
        "promotion requires three retained fresh base/head pilots with stable shard mappings": (
            re.search(r"\b(?:three|3)\b", promotion) is not None
            and "successful" in promotion
            and contains_all(
                promotion,
                "retained",
                "fresh",
                "pilot",
                "summary",
                "artifact",
            )
            and ("per candidate shard" in promotion or "for each candidate shard" in promotion)
            and ("base/head" in promotion or "base and head" in promotion)
            and ("stable mapping" in promotion or "mapping stability" in promotion)
        ),
        "promotion keeps each candidate at or below 24 minutes inside a 30-minute job": (
            "30-minute job" in promotion
            and re.search(
                r"(?:maximum|max runtime|at most|<=|≤).{0,30}24 minutes|"
                r"24-minute (?:maximum|max)",
                promotion,
            )
            is not None
        ),
        "promotion narrows scope when over budget and delays strict until debt resolution": (
            contains_all(
                promotion,
                "narrow",
                "otherwise",
                "strict",
                "debt",
                "resolved",
                "reviewed",
            )
        ),
        "selector JSON covers targeted/all-shard selection, renames, and deletions": (
            contains_all(selection, "selector json", "targeted", "--all-shards", "renames")
            and "both sides" in selection
            and "deletions" in selection
        ),
        "selector explicitly skips empty diffs and fails unresolved refs": contains_all(
            selection, "explicit", "empty", "skip", "unresolved refs", "fail"
        ),
        "unmatched Python tests are advisory": contains_all(
            selection, "unmatched python tests", "advisory"
        ),
        "CI uses the exact Gerrit patchset with push/PR parity": (
            "exact gerrit patchset" in selection
            and ("push/pr" in selection or "push and pr" in selection)
            and ("parity" in selection or "same reusable workflow" in selection)
        ),
        "CI weekly/manual sweeps cancel stale runs": contains_all(
            selection, "weekly", "manual", "all-shard", "cancel"
        ),
        "CI runs one secretless 30-minute job per shard": contains_all(
            selection, "one", "30-minute job", "per shard", "no secrets"
        ),
        "current mutation evidence is Ubuntu-only and portability is unestablished": (
            contains_all(
                normalize("\n".join((selection, fresh))),
                "current mutation evidence",
                "github-hosted",
                "ubuntu-latest",
                "windows",
                "macos",
                "not established",
            )
        ),
        "fresh execution forbids verdict caches and requires clean-test preflight": (
            contains_all(fresh, "fresh archives", "no verdict cache", "clean-test preflight")
        ),
        "fresh execution generates config and pins the runtime/concurrency contract": (
            contains_all(
                fresh,
                "manifest-generated configuration",
                "python 3.12",
                "mutmut==3.7.0",
                "--max-children 1",
            )
        ),
        "timeout values remain solely in the authoritative manifest": contains_all(
            fresh, "timeout values", "authoritative manifest"
        ),
        "artifact upload is attempted with exact always semantics for a running shard job": (
            contains_all(fresh, "if: always()", "attempt", "artifact")
            and re.search(
                r"(?:when|once|for)\s+(?:a|each|every)?\s*shard matrix job"
                r"(?: that)? runs",
                fresh,
            )
            is not None
            and "always published" not in fresh
            and "unconditionally published" not in fresh
        ),
        "artifact absence is explained for all pre-upload termination paths": contains_all(
            fresh,
            "evidence can be absent",
            "hard timeout",
            "cancellation",
            "selector failure",
            "empty selection",
            "before matrix creation",
        ),
        "diagnostic reruns cannot turn red into green": contains_all(
            diagnostics, "diagnostic rerun", "cannot", "red", "green"
        ),
        "equivalent fingerprints are individually reviewed and semantically stable": (
            contains_all(
                diagnostics,
                "equivalent fingerprints",
                "individually reviewed",
                "location-insensitive",
                "mutation-sensitive",
            )
        ),
        "local use documents targeted, all-shard, and evidence inspection commands": (
            contains_all(local_use, "scripts/mutation_gate.py", "--all-shards", "summary.json")
        ),
        "emergency recovery retains the two-vote rollback runbook": (
            contains_all(
                recovery,
                "infra/runbooks/two-vote-gate-rollback.md",
                "two-vote",
                "operator",
            )
        ),
        "stale score/budget historical-table headings are absent globally": (
            not stale_score_budget_headings
        ),
        "stale policy-measurement Markdown table headers are absent globally": (
            not stale_policy_table_headers
        ),
        **{
            f"legacy normative field identifier {identifier!r} is absent": (
                identifier not in normative_policy
            )
            for identifier in legacy_field_identifiers
        },
        **{
            f"legacy normative ceiling phrase {phrase!r} is absent": (
                phrase not in normative_policy
            )
            for phrase in legacy_ceiling_phrases
        },
        **{
            f"{description} is absent from manifest/fresh policy sections": (
                pattern.search(timeout_authority_policy) is None
            )
            for description, pattern in copied_timeout_patterns.items()
        },
    }
    failures = [description for description, satisfied in requirements.items() if not satisfied]

    assert failures == [], "documentation contract violations:\n- " + "\n- ".join(failures)
