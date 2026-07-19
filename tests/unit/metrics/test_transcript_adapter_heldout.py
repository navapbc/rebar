"""Held-out contracts for the transcript adapter (ticket 538c). WITHHELD.

- the classification enum is exactly {env_setup, dependency, integration, tooling, none},
- an integration signature (ConnectionError/timeout) classifies as `integration`,
- the CORE metrics package does not import the adapter (portability guard),
- the backfill script is exercised end-to-end.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from rebar.metrics.adapters.claude_transcripts import CLASSES, mine_transcript

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]


def _write_jsonl(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")


def test_enum_values():
    assert set(CLASSES) == {"env_setup", "dependency", "integration", "tooling", "none"}


def test_integration_signature(tmp_path):
    t = tmp_path / "s.jsonl"
    _write_jsonl(
        t,
        [
            {
                "role": "tool",
                "text": "ConnectionError: connection refused",
                "ts": "2026-03-01T00:00:00+00:00",
            }
        ],
    )
    recs = mine_transcript(str(t))
    assert recs and recs[0]["kind"] == "integration"


def test_core_does_not_import_adapter():
    for rel in ("src/rebar/metrics/registry.py", "src/rebar/metrics/__init__.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "claude_transcripts" not in src, f"{rel} must not import the transcript adapter"


def test_backfill_script_exercised(tmp_path):
    # The script mines a directory of transcripts and returns the labeled records.
    script = _ROOT / "scripts" / "backfill_transcripts.py"
    spec = importlib.util.spec_from_file_location("backfill_transcripts", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _write_jsonl(
        tdir / "a.jsonl",
        [{"role": "tool", "text": "ModuleNotFoundError: x", "ts": "2026-03-01T00:00:00+00:00"}],
    )
    records = mod.backfill(str(tdir))
    assert any(r["kind"] == "env_setup" and r["source"] == "backfill_classified" for r in records)
