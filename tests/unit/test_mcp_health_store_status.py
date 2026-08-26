"""`/health` must distinguish "serving with a store" from "serving with no store at all".

WHY THIS TEST EXISTS. The MCP `/health` route reported only `{"in_flight": N}`. It is BOTH the
container HEALTHCHECK (`infra/compose/Dockerfile.mcp`) and the blue-green readiness gate
(`infra/scripts/autodeploy.sh`), so a container with NO ticket store passed every check in the
pipeline. Combined with a boot sweep that skipped silently on a missing store, that is how a
deployed server answered every tracker query as though the store were merely empty, for weeks,
with nothing able to report it (bugs kilted-nuclear-bronco / mobile-groovy-badger).

THE DESIGN THIS PINS, and it is deliberately not the obvious one. `/health` REPORTS rather than
FAILS, and the discriminator is `expected`, not `present`. A missing store is a fault only for a
deployment that declared it has one; for a deployment that never configured a tracker dir it is
simply a fact. Keying the readiness gate on `present AND expected` is what lets this ship to a
box that currently has NO store without marking a working container unhealthy — and makes the
gate strict automatically once a tracker dir is configured. A test that asserted "no store => 503"
would look stricter and would have caused the outage it was meant to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar._mcp_health import store_status

pytestmark = pytest.mark.unit


def test_absent_store_is_reported_absent_and_expected_when_a_dir_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicitly-configured tracker dir that does not exist is the reportable fault."""
    missing = tmp_path / "not-there"
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(missing))

    status = store_status()

    assert status["present"] is False
    assert status["expected"] is True, (
        "a deployment that names its tracker dir has DECLARED a store; its absence is a fault "
        "the readiness gate must be able to act on"
    )
    assert status["path"] == str(missing), "the path must be named so the fault is greppable"


def test_present_store_is_reported_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The healthy case: a configured dir that exists."""
    present = tmp_path / "store"
    present.mkdir()
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(present))

    status = store_status()

    assert status["present"] is True
    assert status["expected"] is True


def test_no_configured_dir_is_not_expected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The safety property that makes this landable on a box that has no store today.

    With nothing configured, `expected` is False — so a readiness gate keyed on
    `present AND expected` cannot mark a currently-serving container unhealthy just because
    this change shipped before the store was provisioned.
    """
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    status = store_status()

    assert status["expected"] is False


def test_probe_never_raises_even_when_resolution_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A health probe that can throw is worse than one reporting a degraded field.

    If the route raised, the container HEALTHCHECK would fail and `restart: always` would
    turn a store-resolution problem into a crash loop — the opposite of reporting it.
    """
    import rebar.config as _config

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("resolution exploded")

    monkeypatch.setattr(_config, "tracker_dir", _boom)
    monkeypatch.setattr(_config, "tracker_dir_override", _boom)

    status = store_status()

    assert status["present"] is False
    assert "error" in status


def test_autodeploy_gate_refuses_a_storeless_container_only_when_a_store_is_expected() -> None:
    """The readiness gate must key on `expected`, not on `present` alone.

    This is the safety property that makes the change landable on a box that has NO store
    today. Gating on `present` alone would refuse to promote the currently-serving container
    and take the endpoint down in order to report a problem it was only meant to surface.
    """
    autodeploy = (
        Path(__file__).resolve().parents[2] / "infra" / "scripts" / "autodeploy.sh"
    ).read_text(encoding="utf-8")

    assert "mcp-store-missing" in autodeploy, (
        "the mcp readiness gate must be able to refuse a container that reports no store"
    )
    # The refusal condition must require BOTH: expected AND not present.
    assert 'st.get("expected") and not st.get("present")' in autodeploy, (
        "the gate must refuse only when a store was EXPECTED and is absent; keying on absence "
        "alone would block promotion for deployments that legitimately have no store"
    )
