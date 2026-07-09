"""Tests for the env-var registry generator (story 0f21 / audit maintainability #3)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN_PATH = REPO_ROOT / "scripts" / "gen_env_registry.py"


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_env_registry", GEN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load_gen()


def test_positive_capture_direct_helper_and_llm():
    reads, _dynamic = gen.scan(gen.DEFAULT_SCAN_ROOT)
    # a direct os.environ read
    assert "GERRIT_BOT_TOKEN" in reads
    # a _rebar_env("SUFFIX") reconciler read resolved with the REBAR_ prefix
    assert "REBAR_RECONCILER_VERBOSE" in reads
    # a _llm_int(table, cli, "REBAR_LLM_TIMEOUT", ...) read
    assert "REBAR_LLM_TIMEOUT" in reads
    # a _severities_env review-bot read
    assert "BLOCKING_SEVERITIES" in reads


def test_aliases_present_and_removed_vars_absent():
    doc = gen.render()
    # a live permanent alias appears with its annotation
    assert "REBAR_NO_SYNC" in doc
    assert "permanent alias of `REBAR_SYNC_PULL`" in doc
    # vars removed pre-1.0 are NOT emitted (no phantom rows)
    assert "`REBAR_PUSH`" not in doc
    assert "REBAR_MCP_ALLOW_RECONCILE_LIVE" not in doc


def test_drift_is_detected_for_a_new_read(tmp_path: Path):
    # A synthetic module with a NEW env read inside the scanned tree must be picked up,
    # so the drift gate genuinely detects an un-regenerated addition.
    pkg = tmp_path / "rebar_fake"
    pkg.mkdir()
    (pkg / "mod.py").write_text('import os\nX = os.environ["REBAR_FAKE_NEW"]\n')
    reads, _ = gen.scan(tmp_path)
    assert "REBAR_FAKE_NEW" in reads


def test_check_mode_clean_against_committed_tree():
    # The committed docs/env-vars.md must match the generator output (exit 0).
    assert gen.main(["--check"]) == 0
