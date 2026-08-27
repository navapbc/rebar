#!/usr/bin/env python3
"""Run bounded base/head mutation checks for manifest-selected behavioral shards."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

import tomllib

# Sibling-module import: scripts/ is not a package, so a bare `import
# mutation_sandbox` resolves only when this file is RUN as a script. The
# import-walk gate (ticket 37b9) imports every scripts/*.py as a module, where
# that shape raises ModuleNotFoundError — and tests/scripts/conftest.py inserts
# scripts/ process-wide, so a subset test run hides it. Use the documented insert.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_sandbox

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / ".github" / "mutation-shards.toml"
RESULT_RE = re.compile(r"^\s*(\S+): (.+?)\s*$")
MUTANT_SUFFIX_RE = re.compile(r"__mutmut_\d+$")
ALL_STATUSES = frozenset(
    {
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
    }
)
DECISIVE_STATUSES = frozenset({"killed", "survived", "no tests", "timeout"})


class EnforcementMode(StrEnum):
    ADVISORY = "advisory"
    RATCHET = "ratchet"
    STRICT = "strict"


class FailureCode(StrEnum):
    ZERO_MUTANTS = "zero-mutants"
    UNEXPECTED_STATUS = "unexpected-status"
    SURVIVOR_BUDGET = "survivor-budget"
    UNKNOWN_SURVIVOR = "unknown-survivor"
    NO_TESTS_BUDGET = "no-tests-budget"
    TIMEOUT_BUDGET = "timeout-budget"
    SCORE_BUDGET = "score-budget"
    MISSING_MUTANT = "missing-mutant"
    KILLED_REGRESSION = "killed-regression"

    @property
    def blocks_advisory(self) -> bool:
        return self in {FailureCode.ZERO_MUTANTS, FailureCode.UNEXPECTED_STATUS}


@dataclass(frozen=True, init=False)
class TimeoutConstant:
    value: float

    def __init__(self, candidate: object) -> None:
        object.__setattr__(self, "value", _positive_finite_timeout(candidate, "timeout_constant"))


@dataclass(frozen=True, init=False)
class TimeoutMultiplier:
    value: float

    def __init__(self, candidate: object) -> None:
        object.__setattr__(self, "value", _positive_finite_timeout(candidate, "timeout_multiplier"))


def _positive_finite_timeout(candidate: object, field_name: str) -> float:
    detail = f"{field_name} must be a finite number greater than zero"
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise GateError("manifest", detail)
    try:
        value = float(candidate)
    except OverflowError:
        raise GateError("manifest", detail) from None
    if not math.isfinite(value) or value <= 0:
        raise GateError("manifest", detail)
    return value


@dataclass(frozen=True)
class Shard:
    name: str
    source: str
    tests: tuple[str, ...]
    support: tuple[str, ...]
    mode: EnforcementMode
    survivors_max: int
    no_tests_max: int
    timeouts_max: int
    score_floor: float
    equivalent_fingerprints: frozenset[str]
    timeout_constant: TimeoutConstant = TimeoutConstant(1.0)
    timeout_multiplier: TimeoutMultiplier = TimeoutMultiplier(15.0)


@dataclass(frozen=True)
class Manifest:
    shards: Mapping[str, Shard]
    global_support: tuple[str, ...]


@dataclass(frozen=True)
class Selection:
    names: tuple[str, ...]
    unmatched_tests: tuple[str, ...]


@dataclass(frozen=True)
class RunResults:
    statuses: Mapping[str, str]
    unit_hashes: Mapping[str, str]
    fingerprints: Mapping[str, str]


@dataclass(frozen=True)
class Failure:
    code: FailureCode
    detail: str


@dataclass(frozen=True)
class Comparison:
    failures: tuple[Failure, ...]
    removed_units: tuple[str, ...]
    added_units: tuple[str, ...]
    advisories: tuple[Failure, ...] = ()


class GateError(RuntimeError):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise GateError("manifest", f"{field} must be a list of non-empty strings")
    return tuple(value)


def load_manifest(path: Path = DEFAULT_MANIFEST) -> Manifest:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GateError("manifest", f"cannot load {path}: {exc}") from exc
    if raw.get("version") != 1:
        raise GateError("manifest", "mutation manifest version must be 1")
    global_support = _string_tuple(raw.get("global_support", []), "global_support")
    rows = raw.get("shards")
    if not isinstance(rows, list) or not rows:
        raise GateError("manifest", "manifest must declare at least one [[shards]] row")
    shards: dict[str, Shard] = {}
    sources: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise GateError("manifest", f"shards[{index}] must be a table")
        name = row.get("name")
        source = row.get("source")
        if not isinstance(name, str) or not name or name in shards:
            raise GateError("manifest", f"shards[{index}].name must be unique and non-empty")
        if not isinstance(source, str) or not source or source in sources:
            raise GateError("manifest", f"shards[{index}].source must be unique and non-empty")
        tests = _string_tuple(row.get("tests", []), f"{name}.tests")
        support = _string_tuple(row.get("support", []), f"{name}.support")
        mode_value = row.get("mode")
        try:
            mode = EnforcementMode(mode_value) if isinstance(mode_value, str) else None
        except ValueError as exc:
            allowed = ", ".join(mode.value for mode in EnforcementMode)
            raise GateError("manifest", f"{name}.mode must be one of: {allowed}") from exc
        if mode is None:
            allowed = ", ".join(member.value for member in EnforcementMode)
            raise GateError("manifest", f"{name}.mode must be one of: {allowed}")
        equivalents = frozenset(
            _string_tuple(row.get("equivalent_fingerprints", []), f"{name}.equivalent_fingerprints")
        )
        timeout_constant = TimeoutConstant(row.get("timeout_constant"))
        timeout_multiplier = TimeoutMultiplier(row.get("timeout_multiplier"))
        budgets = [row.get("survivors_max"), row.get("no_tests_max"), row.get("timeouts_max")]
        if not all(isinstance(value, int) and value >= 0 for value in budgets):
            raise GateError("manifest", f"{name} count budgets must be non-negative integers")
        survivors_max = cast(int, budgets[0])
        no_tests_max = cast(int, budgets[1])
        timeouts_max = cast(int, budgets[2])
        score_floor = row.get("score_floor")
        if not isinstance(score_floor, (int, float)) or not 0 < float(score_floor) <= 100:
            raise GateError("manifest", f"{name}.score_floor must be in (0, 100]")
        shards[name] = Shard(
            name=name,
            source=source,
            tests=tests,
            support=support,
            mode=mode,
            survivors_max=survivors_max,
            no_tests_max=no_tests_max,
            timeouts_max=timeouts_max,
            score_floor=float(score_floor),
            equivalent_fingerprints=equivalents,
            timeout_constant=timeout_constant,
            timeout_multiplier=timeout_multiplier,
        )
        sources.add(source)
    return Manifest(shards=shards, global_support=global_support)


def parse_name_status(text: str) -> set[str]:
    changed: set[str] = set()
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        status = fields[0]
        paths = fields[1:3] if status.startswith(("R", "C")) else fields[1:2]
        changed.update(path for path in paths if path)
    return changed


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(char in pattern for char in "*?["):
        return fnmatch.fnmatch(path, pattern)
    return path == pattern


def select_shards(manifest: Manifest, changed_paths: set[str]) -> Selection:
    if any(
        _matches(path, pattern) for path in changed_paths for pattern in manifest.global_support
    ):
        names = tuple(manifest.shards)
    else:
        names = tuple(
            name
            for name, shard in manifest.shards.items()
            if any(
                _matches(path, pattern)
                for path in changed_paths
                for pattern in (shard.source, *shard.tests, *shard.support)
            )
        )
    matched_tests = {
        path
        for path in changed_paths
        if path.startswith("tests/")
        and any(
            _matches(path, pattern)
            for shard in manifest.shards.values()
            for pattern in (*shard.tests, *shard.support)
        )
    }
    global_tests = {
        path
        for path in changed_paths
        if path.startswith("tests/")
        and any(_matches(path, pattern) for pattern in manifest.global_support)
    }
    unmatched = tuple(
        sorted(
            path
            for path in changed_paths
            if path.startswith("tests/")
            and path.endswith(".py")
            and path not in matched_tests
            and path not in global_tests
        )
    )
    return Selection(names=names, unmatched_tests=unmatched)


def parse_results(text: str) -> dict[str, str]:
    results: dict[str, str] = {}
    for line in text.splitlines():
        match = RESULT_RE.match(line)
        if match:
            results[match.group(1)] = match.group(2)
    return results


def unit_name(mutant_name: str) -> str:
    return MUTANT_SUFFIX_RE.sub("", mutant_name)


def fingerprint_diff(text: str) -> str:
    material = "\n".join(
        line.rstrip()
        for line in text.splitlines()
        if not line.startswith(("# ", "--- ", "+++ ", "@@ "))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def evaluate_runs(shard: Shard, base: RunResults | None, head: RunResults) -> Comparison:
    if shard.mode is EnforcementMode.ADVISORY:
        return Comparison(
            failures=tuple(
                failure
                for failure in _head_budget_failures(shard, head)
                if failure.code.blocks_advisory
            ),
            removed_units=(),
            added_units=(),
        )
    assert base is not None
    base_units = set(base.unit_hashes)
    head_units = set(head.unit_hashes)
    unchanged_units = {
        unit
        for unit in base.unit_hashes.keys() & head.unit_hashes.keys()
        if base.unit_hashes[unit] == head.unit_hashes[unit]
    }
    head_failures = tuple(
        _strict_head_failures(shard, head)
        if shard.mode is EnforcementMode.STRICT
        else _head_budget_failures(shard, head)
    )
    if shard.mode is EnforcementMode.RATCHET:
        failures = [failure for failure in head_failures if failure.code.blocks_advisory]
        for mutant, status in head.statuses.items():
            if (
                status == "survived"
                and head.fingerprints.get(mutant) not in shard.equivalent_fingerprints
                and not (
                    unit_name(mutant) in unchanged_units and base.statuses.get(mutant) == "survived"
                )
            ):
                failures.append(Failure(FailureCode.UNKNOWN_SURVIVOR, f"{mutant}: new survivor"))
            elif status == "timeout":
                failures.append(Failure(FailureCode.TIMEOUT_BUDGET, f"{mutant}: timeout"))
        advisories = [
            Failure(FailureCode.NO_TESTS_BUDGET, f"{mutant}: no tests")
            for mutant, status in head.statuses.items()
            if status == "no tests"
            and not (
                unit_name(mutant) in unchanged_units and base.statuses.get(mutant) == "no tests"
            )
        ]
    else:
        failures = list(head_failures)
        advisories = []
    for mutant, status in base.statuses.items():
        if status != "killed" or unit_name(mutant) not in unchanged_units:
            continue
        head_status = head.statuses.get(mutant)
        if head_status is None:
            failures.append(
                Failure(
                    FailureCode.MISSING_MUTANT,
                    f"{mutant} was killed on base but is absent on head",
                )
            )
        elif head_status != "killed":
            failures.append(
                Failure(FailureCode.KILLED_REGRESSION, f"{mutant}: killed -> {head_status}")
            )
    return Comparison(
        failures=tuple(failures),
        removed_units=tuple(sorted(base_units - head_units)),
        added_units=tuple(sorted(head_units - base_units)),
        advisories=tuple(advisories),
    )


def compare_runs(shard: Shard, base: RunResults, head: RunResults) -> tuple[Failure, ...]:
    return evaluate_runs(shard, base, head).failures


def _strict_head_failures(shard: Shard, head: RunResults) -> Iterable[Failure]:
    if not head.statuses:
        yield Failure(FailureCode.ZERO_MUTANTS, f"{shard.name}: no mutants were parsed")
        return
    for mutant, status in head.statuses.items():
        if status not in DECISIVE_STATUSES:
            yield Failure(FailureCode.UNEXPECTED_STATUS, f"{mutant}: {status}")
        elif (
            status == "survived"
            and head.fingerprints.get(mutant) not in shard.equivalent_fingerprints
        ):
            yield Failure(FailureCode.UNKNOWN_SURVIVOR, f"{mutant}: unreviewed survivor")
        elif status == "no tests":
            yield Failure(FailureCode.NO_TESTS_BUDGET, f"{mutant}: no tests")
        elif status == "timeout":
            yield Failure(FailureCode.TIMEOUT_BUDGET, f"{mutant}: timeout")


def _head_budget_failures(shard: Shard, head: RunResults) -> Iterable[Failure]:
    if not head.statuses:
        yield Failure(FailureCode.ZERO_MUTANTS, f"{shard.name}: no mutants were parsed")
        return
    counts = {status: 0 for status in ALL_STATUSES}
    for mutant, status in head.statuses.items():
        if status not in DECISIVE_STATUSES:
            yield Failure(FailureCode.UNEXPECTED_STATUS, f"{mutant}: {status}")
        counts[status] = counts.get(status, 0) + 1
    if counts["survived"] > shard.survivors_max:
        yield Failure(
            FailureCode.SURVIVOR_BUDGET,
            f"{counts['survived']} > {shard.survivors_max}",
        )
    for mutant, status in head.statuses.items():
        if (
            status == "survived"
            and head.fingerprints.get(mutant) not in shard.equivalent_fingerprints
        ):
            yield Failure(
                FailureCode.UNKNOWN_SURVIVOR,
                f"{mutant}: unrecognized equivalent fingerprint",
            )
    if counts["no tests"] > shard.no_tests_max:
        yield Failure(FailureCode.NO_TESTS_BUDGET, f"{counts['no tests']} > {shard.no_tests_max}")
    if counts["timeout"] > shard.timeouts_max:
        yield Failure(FailureCode.TIMEOUT_BUDGET, f"{counts['timeout']} > {shard.timeouts_max}")
    denominator = counts["killed"] + counts["survived"] + counts["timeout"]
    score = 100.0 * counts["killed"] / denominator if denominator else 0.0
    if score + 1e-9 < shard.score_floor:
        yield Failure(FailureCode.SCORE_BUDGET, f"{score:.3f}% < {shard.score_floor:.3f}%")


def render_mutmut_config(shard: Shard, *, basetemp: str) -> str:
    tests = ", ".join(json.dumps(path) for path in shard.tests)
    args = ", ".join(
        json.dumps(value) for value in ("-x", "-q", "-p", "no:randomly", f"--basetemp={basetemp}")
    )
    return (
        "[tool.mutmut]\n"
        'source_paths = ["src/rebar"]\n'
        f"only_mutate = [{json.dumps(shard.source)}]\n"
        'also_copy = [".github/"]\n'
        f"pytest_add_cli_args = [{args}]\n"
        f"pytest_add_cli_args_test_selection = [{tests}]\n"
        f"timeout_constant = {shard.timeout_constant.value!r}\n"
        f"timeout_multiplier = {shard.timeout_multiplier.value!r}\n"
    )


def _without_mutmut_table(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == "[tool.mutmut]":
            skipping = True
            continue
        if skipping and line.startswith("["):
            skipping = False
        if not skipping:
            output.append(line)
    return "\n".join(output).rstrip() + "\n"


def _run(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # raw-git-ok: generic runner; git only reads fresh archives
        argv, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def _git(repo: Path, *args: str) -> str:
    result = _run(("git", *args), cwd=repo)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed"
        raise GateError("git", detail)
    return result.stdout


def changed_paths(repo: Path, base: str, head: str) -> set[str]:
    _git(repo, "cat-file", "-e", f"{base}^{{commit}}")
    _git(repo, "cat-file", "-e", f"{head}^{{commit}}")
    return parse_name_status(_git(repo, "diff", "--name-status", "--find-renames", base, head))


def run_select(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest).resolve())
    paths = changed_paths(Path(args.repo).resolve(), args.base, args.head)
    selection = select_shards(manifest, paths)
    names = tuple(manifest.shards) if args.all_shards else selection.names
    print(
        json.dumps(
            {
                "base": args.base,
                "head": args.head,
                "changed_paths": sorted(paths),
                "selected_shards": list(names),
                "unmatched_tests": list(selection.unmatched_tests),
                "empty_selection": not names,
            },
            indent=2,
        )
    )
    return 0


def _extract_ref(repo: Path, ref: str, destination: Path) -> None:
    archive = destination.parent / f"{destination.name}.tar"
    result = _run(("git", "archive", "--format=tar", f"--output={archive}", ref), cwd=repo)
    if result.returncode:
        raise GateError("git", result.stderr.strip() or f"cannot archive {ref}")
    destination.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")


def _module_name(source: str) -> str:
    value = source[:-3].replace("/", ".") if source.endswith(".py") else source.replace("/", ".")
    if value.startswith("src."):
        value = value[4:]
    return value.removesuffix(".__init__")


def function_hashes(root: Path, source: str) -> dict[str, str]:
    tree = ast.parse((root / source).read_text(encoding="utf-8"))
    module = _module_name(source)
    hashes: dict[str, str] = {}

    def visit(body: Sequence[ast.stmt], classes: tuple[str, ...] = ()) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = f"xǁ{'.'.join(classes)}ǁ{node.name}" if classes else f"x_{node.name}"
                material = ast.dump(node, annotate_fields=False).encode("utf-8")
                digest = hashlib.sha256(material).hexdigest()[:12]
                hashes[f"{module}.{key}"] = digest
            elif isinstance(node, ast.ClassDef):
                visit(node.body, (*classes, node.name))

    visit(tree.body)
    return hashes


def _prepare_config(root: Path, shard: Shard, basetemp: Path) -> None:
    pyproject = root / "pyproject.toml"
    text = _without_mutmut_table(pyproject.read_text(encoding="utf-8"))
    config = text + "\n" + render_mutmut_config(shard, basetemp=str(basetemp))
    pyproject.write_text(config, encoding="utf-8")


def _command_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    src = str(root / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def execute_shard(root: Path, shard: Shard, artifact_dir: Path, label: str) -> RunResults:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    basetemp = root / ".mutation-pytest"
    _prepare_config(root, shard, basetemp)
    env = _command_env(root)
    clean_args = (
        sys.executable,
        "-m",
        "pytest",
        *shard.tests,
        "-x",
        "-q",
        "-p",
        "no:randomly",
        f"--basetemp={basetemp}",
    )
    # Sandbox BOTH shard-test-executing subprocesses. The baseline pytest below and the
    # `mutmut run` further down are the only two that execute shard code; `mutmut
    # results`/`show` are reporting and touch no test code. Sandboxing only the baseline
    # would leave `mutmut run` — the path the 2026-08-26 incident took — unprotected.
    sandbox_allow = (root, basetemp, Path(sys.prefix))
    env = mutation_sandbox.sandbox_env(env)
    clean = _run(
        mutation_sandbox.wrap(clean_args, allow=sandbox_allow, profile_dir=artifact_dir, env=env),
        cwd=root,
        env=env,
    )
    (artifact_dir / f"{label}-clean.txt").write_text(clean.stdout + clean.stderr, encoding="utf-8")
    if clean.returncode:
        raise GateError("baseline-test", f"{label}/{shard.name}: selected clean tests failed")
    mutation = _run(
        mutation_sandbox.wrap(
            (sys.executable, "-m", "mutmut", "run", "--max-children", "1"),
            allow=sandbox_allow,
            profile_dir=artifact_dir,
            env=env,
        ),
        cwd=root,
        env=env,
    )
    (artifact_dir / f"{label}-mutmut-run.txt").write_text(
        mutation.stdout + mutation.stderr, encoding="utf-8"
    )
    if mutation.returncode:
        raise GateError(
            "mutation-tool", f"{label}/{shard.name}: mutmut run exited {mutation.returncode}"
        )
    results = _run(
        (sys.executable, "-m", "mutmut", "results", "--all", "true"),
        cwd=root,
        env=env,
    )
    raw = results.stdout + results.stderr
    (artifact_dir / f"{label}-results.txt").write_text(raw, encoding="utf-8")
    if results.returncode:
        raise GateError(
            "mutation-tool", f"{label}/{shard.name}: mutmut results exited {results.returncode}"
        )
    statuses = parse_results(results.stdout)
    fingerprints: dict[str, str] = {}
    survivor_evidence: list[str] = []
    for mutant, status in statuses.items():
        if status != "survived":
            continue
        shown = _run((sys.executable, "-m", "mutmut", "show", mutant), cwd=root, env=env)
        if shown.returncode:
            raise GateError(
                "mutation-tool", f"{label}/{shard.name}: mutmut show failed for {mutant}"
            )
        fingerprint = fingerprint_diff(shown.stdout)
        fingerprints[mutant] = fingerprint
        survivor_evidence.extend(
            (f"# {mutant}", f"fingerprint: {fingerprint}", shown.stdout.rstrip(), "")
        )
    (artifact_dir / f"{label}-survivors.txt").write_text(
        "\n".join(survivor_evidence), encoding="utf-8"
    )
    return RunResults(
        statuses=statuses,
        unit_hashes=function_hashes(root, shard.source),
        fingerprints=fingerprints,
    )


def _diagnose_non_killed(root: Path, head: RunResults, artifact_dir: Path) -> str:
    mutants = [name for name, status in head.statuses.items() if status != "killed"]
    if not mutants:
        return "stable"
    env = _command_env(root)
    _run(
        (sys.executable, "-m", "mutmut", "run", *mutants, "--max-children", "1"),
        cwd=root,
        env=env,
    )
    rerun = _run((sys.executable, "-m", "mutmut", "results", "--all", "true"), cwd=root, env=env)
    (artifact_dir / "head-diagnostic-results.txt").write_text(
        rerun.stdout + rerun.stderr, encoding="utf-8"
    )
    after = parse_results(rerun.stdout)
    changed = any(after.get(name) != head.statuses[name] for name in mutants)
    return "nondeterminism" if changed else "stable"


def run_gate(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(Path(args.manifest).resolve())
    paths = changed_paths(repo, args.base, args.head)
    selection = select_shards(manifest, paths)
    requested = tuple(args.shard or ())
    if requested:
        unknown = sorted(set(requested) - set(manifest.shards))
        if unknown:
            raise GateError("configuration", f"unknown shard(s): {unknown}")
        names = requested
    elif args.all_shards:
        names = tuple(manifest.shards)
    else:
        names = selection.names
    shard_summaries: dict[str, object] = {}
    summary: dict[str, object] = {
        "base": args.base,
        "head": args.head,
        "changed_paths": sorted(paths),
        "selected_shards": list(names),
        "unmatched_tests": list(selection.unmatched_tests),
        "shards": shard_summaries,
    }
    if not names:
        summary["outcome"] = "skip"
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print("Mutation gate: no protected shard selected (successful skip).")
        for path in selection.unmatched_tests:
            print(f"::warning::unmatched mutation-test mapping: {path}")
        return 0
    failed = False
    with tempfile.TemporaryDirectory(prefix="rebar-mutation-") as temp_name:
        temp = Path(temp_name)
        for name in names:
            shard = manifest.shards[name]
            shard_artifacts = output / name
            base_root = temp / f"{name}-base"
            head_root = temp / f"{name}-head"
            if shard.mode is not EnforcementMode.ADVISORY:
                _extract_ref(repo, args.base, base_root)
            _extract_ref(repo, args.head, head_root)
            base_run: RunResults | None = None
            if shard.mode is not EnforcementMode.ADVISORY:
                base_run = execute_shard(base_root, shard, shard_artifacts, "base")
            head_run = execute_shard(head_root, shard, shard_artifacts, "head")
            comparison = evaluate_runs(shard, base_run, head_run)
            diagnostic = "not-needed"
            if comparison.failures:
                failed = True
                diagnostic = _diagnose_non_killed(head_root, head_run, shard_artifacts)
            shard_summaries[name] = {
                "base_collected": base_run is not None,
                "base_counts": None if base_run is None else _status_counts(base_run.statuses),
                "head_counts": _status_counts(head_run.statuses),
                "head_survivor_fingerprints": dict(sorted(head_run.fingerprints.items())),
                "removed_units": list(comparison.removed_units),
                "added_units": list(comparison.added_units),
                "failures": [asdict(item) for item in comparison.failures],
                "advisories": [asdict(item) for item in comparison.advisories],
                "diagnostic": diagnostic,
            }
    summary["outcome"] = "failure" if failed else "success"
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    for path in selection.unmatched_tests:
        print(f"::warning::unmatched mutation-test mapping: {path}")
    print(json.dumps(summary, indent=2))
    return 1 if failed else 0


def _status_counts(statuses: Mapping[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status in statuses.values():
        counts[status] = counts.get(status, 0) + 1
    return counts


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("select", "run", "smoke"):
        child = subparsers.add_parser(command)
        child.add_argument("--repo", default=str(ROOT))
        child.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
        child.add_argument("--base", default="HEAD^")
        child.add_argument("--head", default="HEAD")
        if command == "select":
            child.add_argument("--all-shards", action="store_true")
            continue
        child.add_argument("--output-dir", default="mutation-results")
        child.add_argument("--shard", action="append")
        child.add_argument("--all-shards", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run_select(args) if args.command == "select" else run_gate(args)
    except GateError as exc:
        output = Path(getattr(args, "output_dir", "mutation-results"))
        output.mkdir(parents=True, exist_ok=True)
        payload = {"outcome": "failure", "failure_class": exc.kind, "detail": exc.detail}
        (output / "summary.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"::error::{exc.kind}: {exc.detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
