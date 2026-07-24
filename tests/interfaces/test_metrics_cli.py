"""Happy-path contract for the `rebar metrics` command (ticket 9a5a).

Tier: interface (real CLI subprocess + temp store). Pins the capstone contract:
the command emits JSON whose `metrics` map contains an entry for EVERY registered
metric id, each a value or an `unavailable` object. Text format / isolation held out.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date

import pytest

import rebar
import rebar.metrics  # noqa: F401 — hydrate REGISTRY via package __init__ (side-effect import)
from rebar.metrics.registry import REGISTRY, MetricSpec

pytestmark = pytest.mark.interface


def _cli(*args: str, cwd: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
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


def test_cap_change_events_deregistered():
    assert "cap_change_events" not in {spec.id for spec in REGISTRY}


def test_foreign_repo_honest_unavailable(rebar_repo):
    (rebar_repo / "pyproject.toml").write_text(
        '[tool.rebar.code_health]\nscan_roots = ["src"]\nsize_cap = 800\n',
        encoding="utf-8",
    )
    (rebar_repo / "src").mkdir()
    (rebar_repo / "src" / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    env = os.environ.copy()
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
