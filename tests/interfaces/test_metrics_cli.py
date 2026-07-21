"""Happy-path contract for the `rebar metrics` command (ticket 9a5a).

Tier: interface (real CLI subprocess + temp store). Pins the capstone contract:
the command emits JSON whose `metrics` map contains an entry for EVERY registered
metric id, each a value or an `unavailable` object. Text format / isolation held out.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date

import pytest

import rebar
import rebar.metrics  # noqa: F401 — hydrate REGISTRY via package __init__ (side-effect import)
from rebar.metrics.registry import REGISTRY

pytestmark = pytest.mark.interface


def _cli(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args], capture_output=True, text=True, cwd=cwd
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
