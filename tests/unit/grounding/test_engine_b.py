"""Unit tests for Engine B — the detector registry + evaluator (story 48d7).

Uses the REAL semgrep + ast-grep binaries against a tiny fixture repo. Pins:

* a built-in smell detector MATCHES via the real engine and normalizes to a valid
  evidence record (``ev.validate`` passes);
* a schema-INVALID detector is quarantined as ``abstain(invalid_detector)`` and
  does NOT abort the scan (spike E1 — the key test);
* a detector for an absent language/file is skipped with a coverage record;
* a missing metric binary (scc/lizard) -> ``abstain(no_tool)``;
* project-local ``.rebar/detectors/`` override works (last-wins);
* the registry caches and rebuilds on a detector-dir mtime change;
* a missing engine binary still fails open to ``abstain(no_tool)``.

Every emitted record must pass ``ev.validate``.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from rebar.grounding import engine_b
from rebar.grounding import evidence as ev
from rebar.grounding.detectors import registry as reg_mod

pytestmark = pytest.mark.unit

_HAVE_SEMGREP = shutil.which("opengrep") or shutil.which("semgrep")
_HAVE_ASTGREP = shutil.which("ast-grep") or shutil.which("sg")


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_registry_cache() -> None:
    reg_mod.clear_cache()
    yield
    reg_mod.clear_cache()


@pytest.fixture
def js_repo(tmp_path: Path) -> Path:
    """A tiny repo with a .js file containing console.log + debugger."""
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "app.js").write_text("function f(){ console.log('x'); debugger; }\n")
    return tmp_path


def _all_valid(records) -> None:
    for rec in records:
        ev.validate(rec)


def _project_detectors_dir(repo: Path) -> Path:
    d = repo / ".rebar" / "detectors"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── registry: discovery, parse, cache, override ──────────────────────────────


def test_builtins_discovered() -> None:
    registry = reg_mod.load_registry()
    ids = {d.id for d in registry}
    assert "rebar.builtin.smell.js-console-log" in ids
    assert "rebar.builtin.applies.web-frontend" in ids
    # at least one ast-grep + one metric built-in are routed correctly
    assert any(d.backend == reg_mod.BACKEND_ASTGREP for d in registry)
    assert any(d.backend == reg_mod.BACKEND_METRIC for d in registry)


def test_absent_project_dir_is_fail_open(tmp_path: Path) -> None:
    # No .rebar/detectors/ -> no error, just the built-ins.
    registry = reg_mod.load_registry(tmp_path)
    assert len(registry) >= 4


def test_registry_is_cached_and_rebuilds_on_mtime_change(tmp_path: Path) -> None:
    d = _project_detectors_dir(tmp_path)
    first = reg_mod.load_registry(tmp_path)
    second = reg_mod.load_registry(tmp_path)
    assert first is second  # process-local cache: same snapshot object

    time.sleep(0.01)
    (d / "extra.yaml").write_text(
        "rules:\n  - id: project.extra.rule\n    languages: [python]\n"
        "    severity: INFO\n    message: m\n    pattern: foo(...)\n"
    )
    third = reg_mod.load_registry(tmp_path)
    assert third is not first  # mtime bumped -> rebuilt
    assert third.get("project.extra.rule") is not None


def test_project_override_last_wins(tmp_path: Path) -> None:
    d = _project_detectors_dir(tmp_path)
    # Re-declare a built-in id from the project tree -> project wins.
    (d / "override.yaml").write_text(
        "rules:\n  - id: rebar.builtin.smell.js-console-log\n    languages: [python]\n"
        "    severity: INFO\n    message: overridden\n    pattern: bar(...)\n"
    )
    registry = reg_mod.load_registry(tmp_path)
    det = registry.get("rebar.builtin.smell.js-console-log")
    assert det is not None
    assert det.languages == ("python",)  # project override took effect
    assert "override.yaml" in det.source_path


def test_unparseable_file_is_dropped_not_raised(tmp_path: Path) -> None:
    d = _project_detectors_dir(tmp_path)
    (d / "garbage.yaml").write_text(": this is : not : valid yaml :\n  - [\n")
    registry = reg_mod.load_registry(tmp_path)
    # The bad file is recorded as a parse drop; the scan/registry never raised.
    assert any("garbage.yaml" in p for p, _ in registry.parse_drops)


# ── OpenGrep evaluator (real engine) ─────────────────────────────────────────


@pytest.mark.skipif(not _HAVE_SEMGREP, reason="opengrep/semgrep not installed")
def test_builtin_smell_matches_via_real_engine(js_repo: Path) -> None:
    result = engine_b.scan(js_repo)
    _all_valid(result.records)
    matches = [m for m in result.matches() if m.get("detector_id") == "rebar.builtin.smell.js-console-log"]
    assert matches, "expected the console.log built-in to match"
    m = matches[0]
    assert m["outcome"] == ev.OUTCOME_MATCH
    assert m["job"] == ev.JOB_SMELL
    assert m["provenance_tier"] == ev.TIER_T1
    assert m["coverage"]["backend"] == engine_b.BACKEND_OPENGREP
    assert m["coverage"]["status"] == ev.STATUS_RAN
    assert m["location"]["file"].endswith("app.js")


@pytest.mark.skipif(not _HAVE_SEMGREP, reason="opengrep/semgrep not installed")
def test_invalid_detector_is_quarantined_and_does_not_abort_scan(js_repo: Path) -> None:
    # The KEY test (spike E1): a schema-invalid rule alongside good ones must be
    # quarantined as abstain(invalid_detector) WITHOUT aborting the whole scan.
    d = _project_detectors_dir(js_repo)
    (d / "broken.yaml").write_text(
        "rules:\n  - id: project.broken.rule\n    languages: [javascript]\n"
        '    severity: INFO\n    message: m\n    pattern: "(((unbalanced"\n'
    )
    result = engine_b.scan(js_repo)
    _all_valid(result.records)
    quarantined = [
        r for r in result.abstains()
        if r.get("detector_id") == "project.broken.rule" and r.get("reason") == "invalid_detector"
    ]
    assert quarantined, "invalid detector must be quarantined as abstain(invalid_detector)"
    # The scan did NOT abort: the good built-in still matched.
    assert any(m.get("detector_id") == "rebar.builtin.smell.js-console-log" for m in result.matches())


@pytest.mark.skipif(not _HAVE_SEMGREP, reason="opengrep/semgrep not installed")
def test_absent_language_detector_skipped_with_coverage(tmp_path: Path) -> None:
    # A repo with NO javascript -> the JS built-ins are skipped (not run) with a
    # recorded coverage abstain.
    (tmp_path / "only.py").write_text("x = 1\n")
    result = engine_b.scan(tmp_path)
    _all_valid(result.records)
    skips = [
        r for r in result.abstains()
        if r.get("detector_id") == "rebar.builtin.smell.js-console-log"
        and r.get("reason") == "unsupported_lang"
    ]
    assert skips, "a JS detector in a non-JS repo must be skipped + coverage recorded"
    assert skips[0]["coverage"]["status"] == ev.STATUS_SKIPPED


@pytest.mark.skipif(not _HAVE_SEMGREP, reason="opengrep/semgrep not installed")
def test_non_relative_paths_glob_is_quarantined_not_a_crash(js_repo: Path) -> None:
    # B1 regression: a project detector whose envelope declares a NON-RELATIVE
    # `paths` glob makes Path.glob raise — the whole scan must NOT crash; the bad
    # detector is quarantined (invalid_detector) and good detectors still run.
    d = _project_detectors_dir(js_repo)
    (d / "badpath.yaml").write_text(
        "rules:\n  - id: project.badpath.rule\n    languages: [javascript]\n"
        "    severity: INFO\n    message: m\n    pattern: console.log(...)\n"
        "    metadata: {rebar_envelope: {paths: ['/etc/*']}}\n"
    )
    result = engine_b.scan(js_repo)  # must not raise
    _all_valid(result.records)
    bad = [
        r for r in result.abstains()
        if r.get("detector_id") == "project.badpath.rule" and r.get("reason") == "invalid_detector"
    ]
    assert bad, "a detector with a non-relative paths glob must be quarantined, not crash the scan"
    assert any(m.get("detector_id") == "rebar.builtin.smell.js-console-log" for m in result.matches())


def test_suffix_ambiguity_attributes_to_most_specific_detector() -> None:
    # M1 regression: when one declared id is a dotted suffix of another, the
    # MOST-SPECIFIC (longest) declared id must win — never a wrong-detector envelope.
    from rebar.grounding.detectors.registry import Detector

    def _det(did: str) -> Detector:
        return Detector(
            id=did, backend=reg_mod.BACKEND_OPENGREP, namespace="project",
            source_path="x.yaml", rule={"languages": ["javascript"]},
            envelope={"job": ev.JOB_SMELL, "tier": ev.TIER_T1},
        )

    by_id = {"log": _det("log"), "console.log": _det("console.log")}
    engine_id = "src.proj.cfg.console.log"  # ends with BOTH ".log" and ".console.log"
    matched = engine_b._match_declared(engine_id, by_id)
    assert matched is not None and matched.id == "console.log"  # most-specific wins


@pytest.mark.skipif(not _HAVE_ASTGREP, reason="ast-grep not installed")
def test_astgrep_backend_matches(js_repo: Path) -> None:
    result = engine_b.scan(js_repo)
    _all_valid(result.records)
    ag = [m for m in result.matches() if m.get("detector_id") == "rebar.builtin.smell.js-debugger"]
    assert ag, "expected the ast-grep debugger built-in to match"
    assert ag[0]["coverage"]["backend"] == engine_b.BACKEND_ASTGREP


# ── metric backend (scc/lizard absent -> no_tool) ────────────────────────────


def test_metric_backend_no_tool_when_absent(js_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the metric tools to be 'absent' so the fail-open path is deterministic
    # regardless of the host.
    real_which = shutil.which

    def fake_which(name: str, *a, **k):
        if name in ("scc", "lizard"):
            return None
        return real_which(name, *a, **k)

    monkeypatch.setattr(engine_b.shutil, "which", fake_which)
    records = engine_b._run_metric(list(reg_mod.load_registry().by_backend(reg_mod.BACKEND_METRIC)), js_repo)
    _all_valid(records)
    assert records
    assert all(r["reason"] == "no_tool" and r["coverage"]["backend"] == engine_b.BACKEND_METRIC for r in records)


# ── missing engine binary still fails open ───────────────────────────────────


def test_missing_opengrep_binary_fails_open(js_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_b, "_OPENGREP_CANDIDATES", ("definitely-not-a-real-binary-xyz",))
    dets = list(reg_mod.load_registry().by_backend(reg_mod.BACKEND_OPENGREP))
    records = engine_b._run_opengrep(dets, js_repo)
    _all_valid(records)
    assert records
    assert all(r["reason"] == "no_tool" and r["coverage"]["backend"] == engine_b.BACKEND_OPENGREP for r in records)


def test_missing_astgrep_binary_fails_open(js_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_b, "_ASTGREP_CANDIDATES", ("definitely-not-a-real-binary-xyz",))
    dets = list(reg_mod.load_registry().by_backend(reg_mod.BACKEND_ASTGREP))
    records = engine_b._run_astgrep(dets, js_repo)
    _all_valid(records)
    assert all(r["reason"] == "no_tool" for r in records)


# ── full scan: every record validates ────────────────────────────────────────


@pytest.mark.skipif(not (_HAVE_SEMGREP and _HAVE_ASTGREP), reason="engines not installed")
def test_full_scan_every_record_is_valid_evidence(js_repo: Path) -> None:
    result = engine_b.scan(js_repo)
    assert result.records
    _all_valid(result.records)
    # The account is complete: matches + abstains, no silent no-ops.
    outcomes = {r["outcome"] for r in result.records}
    assert ev.OUTCOME_MATCH in outcomes
