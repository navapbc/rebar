"""Held-out contracts for analyzer-backed module-size metrics. WITHHELD."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from _subprocess_env import subprocess_env

from rebar.metrics import git_metrics
from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.registry import REGISTRY, Unavailable, evaluate

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")


def _commit_at(repo: Path, msg: str, iso_date: str) -> str:
    """Commit with a fixed author/committer date for deterministic history tests."""
    env = subprocess_env(GIT_AUTHOR_DATE=iso_date, GIT_COMMITTER_DATE=iso_date)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", msg], cwd=repo, check=True, capture_output=True, env=env
    )
    return _git(repo, "rev-parse", "HEAD").strip()


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def test_near_cap_boundary():
    loc = {
        "files": {"a": 720, "b": 800, "c": 719, "d": 801},
        "max_loc": 801,
    }

    assert git_metrics.module_size_distribution(loc, 800, 0.1) == {
        "count": 4,
        "near_cap_count": 2,
        "over_cap_count": 1,
        "max_loc": 801,
    }


def test_no_cap():
    loc = {"files": {"a": 720, "b": 801}, "max_loc": 801}

    assert git_metrics.module_size_distribution(loc, None, 0.1) == {
        "count": 2,
        "near_cap_count": None,
        "over_cap_count": None,
        "max_loc": 801,
    }
    oversized = getattr(git_metrics, "oversized_module_count", None)
    assert callable(oversized), "the analyzer-backed oversized metric must be public"
    assert oversized(loc, None, 0.1) is None


def test_analyzer_specs_use_context_config(monkeypatch, tmp_path):
    calls: list[tuple[Path, list[str]]] = []

    def analyze(root: Path, scan_roots: list[str]) -> AnalyzerResult:
        calls.append((root, scan_roots))
        return AnalyzerResult(loc={"files": {"src/a.py": 801}, "max_loc": 801})

    analyzer_module = getattr(git_metrics, "scc_loc", None)
    assert analyzer_module is not None, "module-size specs must use the scc adapter"
    monkeypatch.setattr(analyzer_module, "analyze", analyze)
    specs = {spec.id: spec for spec in REGISTRY}
    ctx = SimpleNamespace(
        repo_root=str(tmp_path),
        scan_roots=["src", "web"],
        size_cap=800,
        size_near_fraction=0.1,
        analysis_cache={},
    )

    distribution = evaluate(specs["module_size_distribution"], ctx)
    oversized = evaluate(specs["oversized_module_count"], ctx)

    assert distribution.value["over_cap_count"] == 1
    assert oversized.value == 1
    assert calls == [(tmp_path, ["src", "web"])]


def test_analyzer_unavailable_reason_is_preserved(monkeypatch, tmp_path):
    analyzer_module = getattr(git_metrics, "scc_loc", None)
    assert analyzer_module is not None, "module-size specs must use the scc adapter"
    monkeypatch.setattr(
        analyzer_module,
        "analyze",
        lambda *_args, **_kwargs: Unavailable(
            reason="could not run scc: [Errno 2] missing",
            accruing_since="2026-01-01T00:00:00+00:00",
        ),
    )
    spec = next(spec for spec in REGISTRY if spec.id == "module_size_distribution")
    result = evaluate(
        spec,
        SimpleNamespace(
            repo_root=str(tmp_path),
            scan_roots=[],
            size_cap=800,
            size_near_fraction=0.1,
            analysis_cache={},
        ),
    )

    assert isinstance(result, Unavailable)
    assert result.reason == "could not run scc: [Errno 2] missing"


def test_refactor_ratio_none_on_zero_insertions(tmp_path):
    # An empty/no-insertions range must return None (=> Unavailable), not raise ZeroDivisionError.
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "seed.py").write_text("x\n", encoding="utf-8")
    _commit(repo, "seed")
    # A future-only window has no commits -> zero insertions -> None.
    assert git_metrics.refactor_to_addition_ratio(str(repo), "2099-01-01", "2100-01-01") is None


def test_provenance_and_no_dup_seed():
    for metric_id in ("module_size_distribution", "oversized_module_count"):
        matches = [spec for spec in REGISTRY if spec.id == metric_id]
        assert len(matches) == 1
        assert matches[0].lens == "code_health"
        assert matches[0].source == "structural"
        assert matches[0].confidence == "high"
        assert matches[0].accruing_since == "2026-01-01T00:00:00+00:00"


# ── module_size_trend / cap_change_events (ticket 21de-f9d9) ────────────────


def test_module_size_trend_and_cap_change_events_provenance():
    for metric_id in ("module_size_trend", "cap_change_events"):
        matches = [spec for spec in REGISTRY if spec.id == metric_id]
        assert len(matches) == 1
        assert matches[0].lens == "code_health"
        assert matches[0].source == "git"
        assert matches[0].confidence == "high"


def test_module_size_trend_unavailable_below_two_qualified_revisions(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    (repo / ".github").mkdir()
    (repo / ".github" / "module-size-limit.txt").write_text("800\n", encoding="utf-8")
    (repo / "src" / "rebar").mkdir(parents=True)
    (repo / "src" / "rebar" / "mod.py").write_text("x\n", encoding="utf-8")
    _commit_at(repo, "only qualified commit", "2026-01-01T00:00:00+00:00")

    result = git_metrics.module_size_trend(str(repo), "2000-01-01", "2100-01-01")

    assert isinstance(result, Unavailable)
    assert "at least two qualified revisions" in result.reason


def test_cap_change_events_unavailable_below_two_qualified_revisions(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    (repo / ".github").mkdir()
    (repo / ".github" / "module-size-limit.txt").write_text("800\n", encoding="utf-8")
    (repo / "src" / "rebar").mkdir(parents=True)
    (repo / "src" / "rebar" / "mod.py").write_text("x\n", encoding="utf-8")
    _commit_at(repo, "only qualified commit", "2026-01-01T00:00:00+00:00")

    result = git_metrics.cap_change_events(str(repo), "2000-01-01", "2100-01-01")

    assert isinstance(result, Unavailable)
    assert "at least two qualified revisions" in result.reason


def test_module_size_trend_unavailable_missing_cap_blob(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "src" / "rebar").mkdir(parents=True)
    (repo / "src" / "rebar" / "mod.py").write_text("x\n", encoding="utf-8")
    _commit_at(repo, "module, no cap ever", "2026-01-01T00:00:00+00:00")
    (repo / "src" / "rebar" / "mod.py").write_text("x\ny\n", encoding="utf-8")
    _commit_at(repo, "grow, still no cap", "2026-01-02T00:00:00+00:00")

    result = git_metrics.module_size_trend(str(repo), "2000-01-01", "2100-01-01")

    assert isinstance(result, Unavailable)
    assert "cap" in result.reason


def test_module_size_trend_unavailable_no_qualifying_python_modules(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    (repo / ".github").mkdir()
    (repo / ".github" / "module-size-limit.txt").write_text("800\n", encoding="utf-8")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _commit_at(repo, "cap, no python modules", "2026-01-01T00:00:00+00:00")
    (repo / "README.md").write_text("hi again\n", encoding="utf-8")
    _commit_at(repo, "still no python modules", "2026-01-02T00:00:00+00:00")

    result = git_metrics.module_size_trend(str(repo), "2000-01-01", "2100-01-01")

    assert isinstance(result, Unavailable)
    assert "python" in result.reason.lower() or "module" in result.reason.lower()


def test_module_size_trend_unavailable_outside_date_range(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    (repo / ".github").mkdir()
    (repo / ".github" / "module-size-limit.txt").write_text("800\n", encoding="utf-8")
    (repo / "src" / "rebar").mkdir(parents=True)
    (repo / "src" / "rebar" / "mod.py").write_text("x\n", encoding="utf-8")
    _commit_at(repo, "qualified but out of range", "2026-01-01T00:00:00+00:00")
    (repo / "src" / "rebar" / "mod.py").write_text("x\ny\n", encoding="utf-8")
    _commit_at(repo, "second qualified, out of range", "2026-01-02T00:00:00+00:00")

    result = git_metrics.module_size_trend(str(repo), "2030-01-01", "2030-12-31")

    assert isinstance(result, Unavailable)


def test_module_size_trend_unavailable_non_git_repository(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    result = git_metrics.module_size_trend(str(not_a_repo), None, None)

    assert isinstance(result, Unavailable)
    assert result.reason


def test_cap_change_events_unavailable_non_git_repository(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    result = git_metrics.cap_change_events(str(not_a_repo), None, None)

    assert isinstance(result, Unavailable)
    assert result.reason


def test_module_size_trend_samples_at_most_50_including_endpoints(tmp_path):
    from datetime import date, timedelta

    repo = tmp_path / "repo"
    _init(repo)
    (repo / ".github").mkdir()
    (repo / ".github" / "module-size-limit.txt").write_text("800\n", encoding="utf-8")
    (repo / "src" / "rebar").mkdir(parents=True)

    total = 55
    start = date(2026, 1, 1)
    shas: list[str] = []
    for i in range(total):
        (repo / "src" / "rebar" / "mod.py").write_text(f"line{i}\n" * (i + 1), encoding="utf-8")
        iso_date = f"{(start + timedelta(days=i)).isoformat()}T00:00:00+00:00"
        shas.append(_commit_at(repo, f"revision {i}", iso_date))

    result = git_metrics.module_size_trend(str(repo), "2000-01-01", "2100-01-01")

    assert result["qualified_revisions"] == total
    assert result["sampled_revisions"] == 50
    samples = result["samples"]
    assert len(samples) == 50
    # The first and last qualified revisions are always retained.
    assert samples[0]["sha"] == shas[0]
    assert samples[-1]["sha"] == shas[-1]
    # Samples must stay in oldest-to-newest order (no re-shuffling from sampling).
    shas_index = {sha: i for i, sha in enumerate(shas)}
    sampled_positions = [shas_index[s["sha"]] for s in samples]
    assert sampled_positions == sorted(sampled_positions)
    assert len(set(sampled_positions)) == 50


# Held-out oracle re-applied after implementation.
def _oracle_write_cap(repo: Path, cap: int | str) -> None:
    cap_file = repo / ".github" / "module-size-limit.txt"
    cap_file.parent.mkdir(parents=True, exist_ok=True)
    cap_file.write_text(f"{cap}\n", encoding="utf-8")


def _oracle_write_module(repo: Path, name: str, line_count: int) -> None:
    module = repo / "src" / "rebar" / name
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("".join(f"line_{i}\n" for i in range(line_count)), encoding="utf-8")


def test_oracle_git_history_metrics_return_specific_unavailable_reasons(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init(repo)
    _oracle_write_cap(repo, 800)
    _oracle_write_module(repo, "only.py", 1)
    _commit_at(repo, "only one qualified revision", "2026-01-01T00:00:00+00:00")

    result = git_metrics.module_size_trend(str(repo), "2000-01-01", "2100-01-01")
    assert isinstance(result, Unavailable)
    assert "at least two qualified revisions" in result.reason

    no_modules = tmp_path / "no_modules"
    _init(no_modules)
    _oracle_write_cap(no_modules, 800)
    _commit_at(no_modules, "cap only", "2026-01-01T00:00:00+00:00")
    _oracle_write_cap(no_modules, 801)
    _commit_at(no_modules, "cap only again", "2026-01-02T00:00:00+00:00")
    result = git_metrics.cap_change_events(str(no_modules), "2000-01-01", "2100-01-01")
    assert isinstance(result, Unavailable)
    assert "no tracked src/rebar Python modules" in result.reason

    bad_cap = tmp_path / "bad_cap"
    _init(bad_cap)
    _oracle_write_cap(bad_cap, "not-an-int")
    _oracle_write_module(bad_cap, "m.py", 1)
    _commit_at(bad_cap, "bad cap", "2026-01-01T00:00:00+00:00")
    result = git_metrics.module_size_trend(str(bad_cap), "2000-01-01", "2100-01-01")
    assert isinstance(result, Unavailable)
    assert "invalid module-size cap" in result.reason

    monkeypatch.setattr(
        git_metrics, "_git", lambda *_args: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    result = git_metrics.cap_change_events(str(repo), "2000-01-01", "2100-01-01")
    assert isinstance(result, Unavailable)
    assert result.reason == "git history unavailable: boom"


def test_oracle_git_history_metric_specs_are_registered_once_with_git_provenance():
    specs = {spec.id: spec for spec in REGISTRY}
    for metric_id in ("module_size_trend", "cap_change_events"):
        matches = [spec for spec in REGISTRY if spec.id == metric_id]
        assert len(matches) == 1
        spec = specs[metric_id]
        assert spec.lens == "code_health"
        assert spec.source == "git"
        assert spec.confidence == "high"
