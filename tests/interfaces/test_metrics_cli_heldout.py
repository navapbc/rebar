"""Held-out contracts for the `rebar metrics` command (ticket 9a5a). WITHHELD.

- an unpopulated metric renders as an `unavailable` object carrying a reason,
- `--output text` produces a per-metric human summary (one line per registered id),
- the portability guard: _commands/metrics.py does not import rebar.metrics.adapters.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import rebar.metrics  # noqa: F401 — hydrate REGISTRY via package __init__ (side-effect import)
from rebar.metrics.registry import REGISTRY

pytestmark = pytest.mark.interface

_ROOT = Path(__file__).resolve().parents[2]


def _cli(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args], capture_output=True, text=True, cwd=cwd
    )


def test_unpopulated_metric_is_unavailable(rebar_repo):
    repo = str(rebar_repo)
    p = _cli(
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
    p = _cli(
        "metrics", "--since", "2026-01-01", "--until", "2026-07-01", "--output", "text", cwd=repo
    )
    assert p.returncode == 0, p.stderr
    # One line per registered metric id (a human summary, not JSON).
    for spec in REGISTRY:
        assert spec.id in p.stdout, f"text output missing metric {spec.id}"
    assert "unavailable" in p.stdout  # a fresh store renders unavailables


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
