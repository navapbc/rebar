"""Held-out oracle for Anthropic SDK class monkeypatch import isolation."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

_TARGETS = (
    "test_conflicting_anthropic_auth_fails_before_client_build",
    "test_empty_anthropic_carrier_fails_closed",
    "test_secret_absent_from_conflict_error_message",
    # Negative control: this node already preserves the SDK symbol's class contract.
    "test_only_selected_provider_carrier_consumed",
)


@pytest.mark.parametrize("target", _TARGETS)
def test_anthropic_auth_oracle_isolated_from_provider_import_order(target: str) -> None:
    """Each auth oracle must pass when the provider has not already been imported."""
    node = f"tests/unit/test_rp04_s4_llm_runtime_heldout.py::{target}"
    script = (
        "import sys; "
        "assert 'pydantic_ai.providers.anthropic' not in sys.modules; "
        "import pytest; "
        f"raise SystemExit(pytest.main(['-q', {node!r}]))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
