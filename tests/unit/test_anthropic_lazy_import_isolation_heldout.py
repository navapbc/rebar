"""Held-out oracle for Anthropic SDK class monkeypatch import isolation."""

from __future__ import annotations

from pathlib import Path

import pytest
from _nested_pytest import run_nested_pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

_TARGETS = (
    "test_conflicting_anthropic_auth_fails_before_client_build",
    "test_empty_anthropic_carrier_fails_closed",
    "test_secret_absent_from_conflict_error_message",
    # Negative control: this node already preserves the SDK symbol's class contract.
    "test_only_selected_provider_carrier_consumed",
)


@pytest.mark.parametrize("target", _TARGETS)
def test_anthropic_auth_oracle_isolated_from_provider_import_order(
    target: str, tmp_path: Path
) -> None:
    """Each auth oracle must pass when the provider has not already been imported.

    A fresh interpreter IS that condition, so this goes through
    :func:`run_nested_pytest` rather than hand-rolling the launch.  It used to spell the
    same operation as ``python -c "... pytest.main(...)"`` with no ``--basetemp``, which
    the uniqueness guard could not see: one serial run of this module allocated four
    children into the SHARED numbered temp root and deleted four other sessions' roots
    (bug 16e1-237d).
    """
    node = f"tests/unit/test_rp04_s4_llm_runtime_heldout.py::{target}"
    completed = run_nested_pytest(tmp_path, "-q", node, env=subprocess_env())

    assert completed.returncode == 0, completed.stdout + completed.stderr
