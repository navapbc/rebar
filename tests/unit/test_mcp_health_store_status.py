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

import logging
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


def test_autodeploy_gate_names_the_storeless_refusal() -> None:
    """The gate must have a NAMED refusal reason, so a blocked promotion is greppable.

    Deliberately narrow: this asserts only that the marker name exists. An earlier version
    grepped the script for the verbatim predicate `st.get("expected") and not st.get("present")`,
    which pinned an implementation detail — any equivalent rewrite would have failed it while
    the behaviour was unchanged. The BEHAVIOUR is covered where it should be, by tests that
    drive the real script through the harness with a stubbed /health body:
    `test_storeless_container_is_not_promoted_when_a_store_is_expected`,
    `test_storeless_container_IS_promoted_when_no_store_is_expected`, and
    `test_health_without_a_store_field_still_promotes` in
    tests/scripts/test_autodeploy_mcp_bluegreen.py.
    """
    autodeploy = (
        Path(__file__).resolve().parents[2] / "infra" / "scripts" / "autodeploy.sh"
    ).read_text(encoding="utf-8")

    assert "mcp-store-missing" in autodeploy, (
        "the mcp readiness gate must refuse a storeless container under a named reason"
    )


def test_boot_sweep_warns_naming_the_path_when_the_store_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing store at boot must SAY SO, naming the path.

    The sweep is deliberately log-and-continue — a missing store must not abort boot — but
    before this it emitted nothing at all on that branch. Combined with a /health probe that
    could not see the store, that silence is why a storeless deployment ran unobserved: there
    was no line to grep for and no signal to alarm on. The path is in the message because
    "no store" is not actionable without knowing WHICH path was checked.
    """
    from rebar._mcp_health import run_startup_store_sweep

    missing = tmp_path / "absent-store"
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(missing))

    with caplog.at_level(logging.WARNING, logger="rebar"):
        run_startup_store_sweep()

    assert any(str(missing) in r.getMessage() for r in caplog.records), (
        "the boot sweep must emit a WARNING naming the absent tracker path"
    )


def test_boot_sweep_is_quiet_and_does_not_raise_when_the_store_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half: a present store must not produce the missing-store warning.

    Without this, a warning hard-coded on every boot would look identical to the real signal
    and train operators to ignore it.
    """
    from rebar._mcp_health import run_startup_store_sweep

    present = tmp_path / "store"
    present.mkdir()
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(present))

    # Liveness anchor: prove the PRESENT-store branch actually ran. Asserting only the
    # absence of a warning would pass just as happily if the function returned early, or
    # never resolved the path at all — the test would be green for the wrong reason.
    swept: list[str] = []
    import rebar._store.ensures as _ensures

    monkeypatch.setattr(_ensures, "run_ensures", lambda t, **kw: swept.append(t))

    with caplog.at_level(logging.WARNING, logger="rebar"):
        run_startup_store_sweep()  # must not raise

    assert swept == [str(present)], (
        f"the present-store branch must actually run the ensure sweep on {present}; got {swept}"
    )
    assert not any("no ticket store at" in r.getMessage() for r in caplog.records), (
        "a present store must not emit the missing-store warning"
    )
