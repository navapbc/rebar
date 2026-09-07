"""Contract tests for deterministic Claude transcript mining.

``mine_transcript`` extracts environment-failure records with ``kind``, ``signature``, and
``ts`` fields. It labels them with ``source="backfill_classified"`` and
``confidence="classified"``.
"""

from __future__ import annotations

import json

import pytest

from rebar.metrics.adapters.claude_transcripts import mine_transcript

pytestmark = pytest.mark.unit


def _write_jsonl(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")


def test_env_failure_signature_classified(tmp_path):
    t = tmp_path / "session.jsonl"
    _write_jsonl(
        t,
        [
            {"role": "user", "text": "run the tests", "ts": "2026-03-01T00:00:00+00:00"},
            {
                "role": "tool",
                "text": "ModuleNotFoundError: No module named 'rebar'",
                "ts": "2026-03-01T00:01:00+00:00",
            },
            {"role": "assistant", "text": "let me fix the venv", "ts": "2026-03-01T00:02:00+00:00"},
        ],
    )

    records = mine_transcript(str(t))
    assert records, "an env-failure signature must be mined"
    rec = records[0]
    assert rec["kind"] == "env_setup"  # ModuleNotFoundError -> env_setup
    assert rec["source"] == "backfill_classified"
    assert rec["confidence"] == "classified"


def test_clean_transcript_yields_nothing(tmp_path):
    t = tmp_path / "clean.jsonl"
    _write_jsonl(
        t,
        [
            {"role": "user", "text": "hello", "ts": "2026-03-01T00:00:00+00:00"},
            {"role": "assistant", "text": "all tests passed", "ts": "2026-03-01T00:01:00+00:00"},
        ],
    )
    assert mine_transcript(str(t)) == []
