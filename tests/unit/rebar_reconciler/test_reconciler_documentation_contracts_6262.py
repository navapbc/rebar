"""Documentation contracts for reconciler rollout/rollback/operator prose.

Ticket 6262 corrects maintained source headers and operator docs whose wording
drifted after the reconciler rollout cutovers landed. These tests pin the
promises in the maintained text against the current architecture so the same
stale prose cannot silently return.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MAPPING_CONFIG = REPO_ROOT / "src/rebar/_engine/rebar_reconciler/mapping_config.py"
ADAPTERS_INIT = REPO_ROOT / "src/rebar/_engine/rebar_reconciler/adapters/__init__.py"
APPLY_OUTBOUND = REPO_ROOT / "src/rebar/_engine/rebar_reconciler/apply_outbound.py"
OUTBOUND_DIFFER = REPO_ROOT / "src/rebar/_engine/rebar_reconciler/outbound_differ.py"
JIRA_SYNC_SETUP = REPO_ROOT / "docs/jira-sync-setup.md"
ENV_VARS = REPO_ROOT / "docs/env-vars.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _squash_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_rollout_headers_describe_the_live_reconciler_surfaces() -> None:
    mapping = _text(MAPPING_CONFIG)
    mapping_flat = _squash_ws(mapping)
    assert "used by the live reconciler" in mapping_flat
    assert "per-project overlays" in mapping_flat
    assert "walking-skeleton foundation every other mapping child stands on" not in mapping
    assert "NO axis is wired into the reconciler here -- later stories" not in mapping

    adapters = _text(ADAPTERS_INIT)
    assert "live vendor backends" in adapters
    assert "Phase 1 relocates only the loader-safe, low-reference" not in adapters

    outbound = _text(APPLY_OUTBOUND)
    assert "create / update / delete / probe / conflict outbound leaf handlers" in outbound
    assert "currently stubbed" not in outbound


def test_outbound_diff_config_docs_cover_all_external_inputs_and_override_precedence() -> None:
    text = _text(OUTBOUND_DIFFER)
    for field in (
        "excluded_statuses",
        "local_label_intent",
        "client",
        "pass_id",
        "prev_snapshot",
        "conflict_sink",
        "dropped_field_sink",
        "projects_mapping",
        "repo_root",
    ):
        assert f"{field}:" in text, f"{field} is undocumented in OutboundDiffConfig"

    flat = _squash_ws(text)
    assert "Direct `mapping=` / `repo_root=` keyword arguments override" in flat


def test_operator_docs_name_rollback_values_defaults_and_consumer_specific_effects() -> None:
    guide = _text(JIRA_SYNC_SETUP)
    guide_flat = _squash_ws(guide)
    assert "`REBAR_RECONCILER_CREATE_ROUTE`" in guide
    assert "`REBAR_RECONCILER_WRITE_FACADE`" in guide
    for token in (
        "legacy",
        "coordinator",
        "`0` / `false` / `off` / `no`",
        "`1` / `true` / `on` / `yes`",
    ):
        assert token in guide_flat
    assert "only the outbound CREATE consumer" in guide_flat
    assert "legacy create+delete rollback path" in guide_flat
    assert "composed apply runtime" in guide_flat
    assert "never deletes a created issue on a post-create failure" in guide_flat

    env_vars = _text(ENV_VARS)
    assert "`REBAR_RECONCILER_CREATE_ROUTE`" in env_vars
    assert "`REBAR_RECONCILER_WRITE_FACADE`" in env_vars
