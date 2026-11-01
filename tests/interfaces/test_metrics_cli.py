"""Happy-path contract for the `rebar metrics` command (ticket 9a5a).

Tier: interface (real CLI subprocess + temp store). Pins the capstone contract:
the command emits JSON whose `metrics` map contains an entry for EVERY registered
metric id, each a value or an `unavailable` object. Text format / isolation held out.

Held-out oracles merged from test_metrics_cli_heldout: Held-out contracts for the `rebar
metrics` command (ticket 9a5a). WITHHELD.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar
import rebar.metrics
from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.analyzers import jscpd_dup, lizard_complexity, scc_loc
from rebar.metrics.registry import REGISTRY, MetricSpec, Unavailable

pytestmark = pytest.mark.interface


def _cli(*args: str, cwd: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )


def test_metrics_json_covers_every_registered_id(rebar_repo):
    repo = str(rebar_repo)
    p = _cli(
        "metrics", "--since", "2026-01-01", "--until", "2026-07-01", "--output", "json", cwd=repo
    )
    assert p.returncode == 0, p.stderr

    doc = json.loads(p.stdout)
    metrics = doc["metrics"]
    registered = {spec.id for spec in REGISTRY}
    # REGISTRY must be hydrated (readers registered) — else the >= below is vacuous.
    assert registered, "REGISTRY should be non-empty (import rebar.metrics hydrates it)"
    assert set(metrics.keys()) >= registered, (
        f"missing metric ids: {registered - set(metrics.keys())}"
    )
    # Each entry is either a value (carrying the spec lens + source/confidence) or an
    # unavailable object (carrying a non-empty reason).
    for mid in registered:
        entry = metrics[mid]
        if "value" in entry:
            assert entry.get("lens"), f"{mid} value entry needs a lens: {entry}"
            assert entry.get("source"), f"{mid} value entry needs a source: {entry}"
            assert entry.get("confidence"), f"{mid} value entry needs a confidence: {entry}"
        else:
            assert entry.get("unavailable", {}).get("reason"), (
                f"{mid} unavailable needs reason: {entry}"
            )


def test_metrics_without_dates_uses_a_real_default_window(rebar_repo):
    p = _cli("metrics", "--output", "json", cwd=str(rebar_repo))
    assert p.returncode == 0, p.stderr

    doc = json.loads(p.stdout)
    since = date.fromisoformat(doc["since"])
    until = date.fromisoformat(doc["until"])
    assert (until - since).days == 30
    assert all("Invalid isoformat" not in str(entry) for entry in doc["metrics"].values())


def test_metrics_context_carries_code_health_config(rebar_repo, monkeypatch, capsys):
    from rebar import config
    from rebar._commands.metrics import metrics_cli

    (rebar_repo / "pyproject.toml").write_text(
        "[tool.rebar.code_health]\n"
        'scan_roots = ["src", "web"]\n'
        "size_cap = 800\n"
        "size_near_fraction = 0.15\n",
        encoding="utf-8",
    )
    config.reset_config_cache()
    probe = MetricSpec(
        id="context_probe",
        lens="code_health",
        source="structural",
        confidence="high",
        compute=lambda ctx: {
            "scan_roots": ctx.scan_roots,
            "size_cap": ctx.size_cap,
            "size_near_fraction": ctx.size_near_fraction,
        },
        accruing_since="2026-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(rebar.metrics.registry, "REGISTRY", [probe])

    assert metrics_cli(["--output", "json"], repo_root=str(rebar_repo)) == 0
    entry = json.loads(capsys.readouterr().out)["metrics"]["context_probe"]
    assert entry["value"] == {
        "scan_roots": ["src", "web"],
        "size_cap": 800,
        "size_near_fraction": 0.15,
    }


def test_module_size_trend_and_cap_change_events_registered_live():
    """Ticket 21de-f9d9: these ids are no longer permanently-unavailable placeholders —
    they must be live specs with real ``code_health``/``git``/``high`` provenance."""
    ids = {spec.id: spec for spec in REGISTRY}
    for metric_id in ("module_size_trend", "cap_change_events"):
        assert metric_id in ids, f"{metric_id} must be registered"
        spec = ids[metric_id]
        assert spec.lens == "code_health"
        assert spec.source == "git"
        assert spec.confidence == "high"


def test_metrics_cli_surfaces_module_size_trend_and_cap_change_events(rebar_repo):
    """Exercise the real ``rebar metrics`` CLI over a small Git-derived history."""

    def _commit_at(msg: str, iso_date: str) -> None:
        subprocess.run(
            ["git", "add", "-A"], cwd=rebar_repo, check=True, capture_output=True, text=True
        )
        env = subprocess_env()
        env["GIT_AUTHOR_DATE"] = iso_date
        env["GIT_COMMITTER_DATE"] = iso_date
        subprocess.run(
            ["git", "commit", "-q", "-m", msg],
            cwd=rebar_repo,
            check=True,
            capture_output=True,
            env=env,
        )

    (rebar_repo / ".github").mkdir()
    (rebar_repo / ".github" / "module-size-limit.txt").write_text("800\n", encoding="utf-8")
    (rebar_repo / "src" / "rebar").mkdir(parents=True)
    (rebar_repo / "src" / "rebar" / "mod.py").write_text("x\n", encoding="utf-8")
    _commit_at("add cap + module", "2026-01-01T00:00:00+00:00")
    (rebar_repo / "src" / "rebar" / "mod.py").write_text("x\ny\n", encoding="utf-8")
    _commit_at("grow module", "2026-01-02T00:00:00+00:00")

    p = _cli(
        "metrics",
        "--since",
        "2000-01-01",
        "--until",
        "2100-01-01",
        "--output",
        "json",
        cwd=str(rebar_repo),
    )
    assert p.returncode == 0, p.stderr
    metrics = json.loads(p.stdout)["metrics"]

    trend = metrics["module_size_trend"]
    assert trend["lens"] == "code_health"
    assert trend["source"] == "git"
    assert trend["confidence"] == "high"
    assert trend["value"]["qualified_revisions"] == 2
    assert len(trend["value"]["samples"]) == 2

    events = metrics["cap_change_events"]
    assert events["value"] == {"events": [], "qualified_revisions": 2}


def test_foreign_repo_honest_unavailable(rebar_repo):
    (rebar_repo / "pyproject.toml").write_text(
        '[tool.rebar.code_health]\nscan_roots = ["src"]\nsize_cap = 800\n',
        encoding="utf-8",
    )
    (rebar_repo / "src").mkdir()
    (rebar_repo / "src" / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    env = subprocess_env()
    env["PATH"] = ""

    completed = _cli("metrics", "--output", "json", cwd=str(rebar_repo), env=env)

    assert completed.returncode == 0, completed.stderr
    metrics = json.loads(completed.stdout)["metrics"]
    for metric_id in ("module_size_distribution", "oversized_module_count"):
        entry = metrics[metric_id]
        assert "unavailable" in entry
        reason = entry["unavailable"]["reason"]
        assert "Errno" not in reason
        assert "module-size-limit.txt" not in reason


def test_metrics_cli_emits_composed_code_health_values(rebar_repo, monkeypatch, capsys):
    from rebar import config
    from rebar._commands.metrics import metrics_cli

    (rebar_repo / "pyproject.toml").write_text(
        '[tool.rebar.code_health]\nscan_roots = ["src"]\nsize_cap = 800\n',
        encoding="utf-8",
    )
    config.reset_config_cache()
    monkeypatch.setattr(
        scc_loc,
        "analyze",
        lambda *_args, **_kwargs: AnalyzerResult(
            loc={"files": {"src/app.py": 801}, "max_loc": 801}
        ),
    )
    monkeypatch.setattr(
        lizard_complexity,
        "analyze",
        lambda *_args, **_kwargs: AnalyzerResult(
            complexity={"files": {}, "functions": 1, "total_ccn": 3, "max_ccn": 3}
        ),
    )
    monkeypatch.setattr(
        jscpd_dup,
        "analyze",
        lambda *_args, **_kwargs: AnalyzerResult(duplication={"clones": 2, "percentage": 4.5}),
    )

    assert metrics_cli(["--output", "json"], repo_root=str(rebar_repo)) == 0
    metrics = json.loads(capsys.readouterr().out)["metrics"]

    assert metrics["module_size_distribution"]["value"]["over_cap_count"] == 1
    assert metrics["oversized_module_count"]["value"] == 1
    assert metrics["complexity_summary"]["value"]["max_ccn"] == 3
    assert metrics["duplication_summary"]["value"] == {
        "clones": 2,
        "percentage": 4.5,
    }


def test_metrics_cli_names_missing_producers(rebar_repo, monkeypatch, capsys):
    from rebar._commands.metrics import metrics_cli

    def missing(reason):
        return lambda *_args, **_kwargs: Unavailable(
            reason=reason,
            accruing_since="2026-01-01T00:00:00+00:00",
        )

    monkeypatch.setattr(scc_loc, "analyze", missing("could not run scc: missing"))
    monkeypatch.setattr(
        lizard_complexity,
        "analyze",
        missing("optional dependency 'lizard' is missing"),
    )
    monkeypatch.setattr(jscpd_dup, "analyze", missing("could not run jscpd: missing"))

    assert metrics_cli(["--output", "json"], repo_root=str(rebar_repo)) == 0
    metrics = json.loads(capsys.readouterr().out)["metrics"]

    assert metrics["module_size_distribution"]["unavailable"]["reason"] == (
        "could not run scc: missing"
    )
    assert metrics["oversized_module_count"]["unavailable"]["reason"] == (
        "could not run scc: missing"
    )
    assert metrics["complexity_summary"]["unavailable"]["reason"] == (
        "optional dependency 'lizard' is missing"
    )
    assert metrics["duplication_summary"]["unavailable"]["reason"] == (
        "could not run jscpd: missing"
    )


def test_oracle_git_history_metrics_are_registered_and_rendered_by_cli(rebar_repo):
    p = _cli(
        "metrics",
        "--since",
        "2026-01-01",
        "--until",
        "2026-01-31",
        "--output",
        "json",
        cwd=str(rebar_repo),
    )
    assert p.returncode == 0, p.stderr
    metrics = json.loads(p.stdout)["metrics"]

    for metric_id in ("module_size_trend", "cap_change_events"):
        assert metric_id in metrics
        entry = metrics[metric_id]
        if "value" in entry:
            assert entry["lens"] == "code_health"
            assert entry["source"] == "git"
            assert entry["confidence"] == "high"
        else:
            assert entry["unavailable"]["reason"]


# ── held-out oracles (merged from test_metrics_cli_heldout) ──

_ROOT = Path(__file__).resolve().parents[2]


def _cli_heldout(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )


def test_unpopulated_metric_is_unavailable(rebar_repo):
    repo = str(rebar_repo)
    p = _cli_heldout(
        "metrics", "--since", "2026-01-01", "--until", "2026-07-01", "--output", "json", cwd=repo
    )
    assert p.returncode == 0, p.stderr
    metrics = json.loads(p.stdout)["metrics"]
    # A fresh store has no accrued data, so at least one metric must be `unavailable`
    # with a non-empty reason.
    unavail = [m for m in metrics.values() if "unavailable" in m]
    assert unavail, "a fresh store should report at least one unavailable metric"
    assert unavail[0]["unavailable"].get("reason")


def test_text_output_lists_every_metric(rebar_repo):
    repo = str(rebar_repo)
    p = _cli_heldout(
        "metrics", "--since", "2026-01-01", "--until", "2026-07-01", "--output", "text", cwd=repo
    )
    assert p.returncode == 0, p.stderr
    # One line per registered metric id (a human summary, not JSON).
    for spec in REGISTRY:
        assert spec.id in p.stdout, f"text output missing metric {spec.id}"
    assert "unavailable" in p.stdout


def test_command_does_not_import_adapters():
    # Portability guard: the core command must stay harness-agnostic — no adapter IMPORT.
    # Parse the AST and inspect import statements (a docstring/comment mentioning the
    # adapters is fine — only an actual import breaks isolation).
    import ast

    src = (_ROOT / "src" / "rebar" / "_commands" / "metrics.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not any("adapters" in mod for mod in imported), (
        f"metrics.py imports an adapter: {imported}"
    )
