"""Discoverability guard for the metrics surface (ticket 121b).

Tier: scripts. Asserts the `rebar metrics` surface is referenced where agents and
contributors will find it: the rebar-janitor discovery phase, the user guide, the
reuse-surface API doc, and the AGENTS.md "Where to read" list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.scripts

_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_janitor_discovery_cites_rebar_metrics():
    text = _read("examples/agent-skills/rebar-janitor/phases/discovery.md")
    assert "rebar metrics" in text, "janitor discovery must tell agents to consult `rebar metrics`"
    assert "unavailable" in text, "discovery must note the `unavailable` state so absence != zero"


def test_user_guide_has_metrics_section():
    text = _read("docs/user-guide.md")
    assert "## Metrics" in text or "# Metrics" in text, "user-guide needs a Metrics section"
    assert "rebar metrics" in text
    # names the actual lens values shipped by the registry
    for lens in ("agent_process", "code_health", "delivery", "gate_economics"):
        assert lens in text, f"user-guide Metrics section must name the {lens} lens"
    assert "unavailable" in text


def test_reuse_surface_references_metrics_registry():
    text = _read("docs/reuse-surface.md")
    assert "rebar.metrics" in text, "reuse-surface must reference the rebar.metrics subsystem"


def test_agents_where_to_read_has_metrics_pointer():
    text = _read("AGENTS.md")
    # a bullet in the Where-to-read list mentioning metrics
    assert "Metrics" in text and "rebar metrics" in text, "AGENTS.md needs a metrics pointer"


def test_metrics_surface_documented():
    reuse_surface = _read("docs/reuse-surface.md")
    for prerequisite in ("[metrics]", "scc", "jscpd"):
        assert prerequisite in reuse_surface, (
            f"reuse-surface must document the {prerequisite} analyzer prerequisite"
        )

    config = _read("docs/config.md")
    assert "[code_health]" in config
    for key in ("enabled", "scan_roots", "analyzers", "size_cap", "size_near_fraction"):
        assert key in config, f"config reference must document code_health.{key}"

    user_guide = _read("docs/user-guide.md")
    for prerequisite in ("rebar[metrics]", "scc", "jscpd"):
        assert prerequisite in user_guide, (
            f"user guide must document the {prerequisite} analyzer prerequisite"
        )
    assert "Unavailable" in user_guide
    assert any(
        phrase in user_guide.lower()
        for phrase in ("no analyzer", "an analyzer is absent", "analyzers are absent")
    )
