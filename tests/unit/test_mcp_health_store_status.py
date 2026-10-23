"""Health reports whether the ticket store is present and expected.

The route once reported only in_flight, so both the container healthcheck and
blue-green readiness gate accepted a server with no store. Boot sweep was also
silent (kilted-nuclear-bronco / mobile-groovy-badger). Health reports rather
than fails. A missing store is a fault when a tracker directory was configured.
Otherwise expected is false. Readiness uses these fields so deploying before
store provisioning does not mark a serving container unhealthy."""

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
    """Require a greppable refusal marker.

    The script harness tests promotion with a missing expected store, no expected
    store, and a legacy health body. This avoids pinning the shell predicate's spelling.
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
    """Boot continues when the store is missing, but warns with the checked path.
    Silence alongside the old health response hid a storeless deployment. The
    path makes the warning actionable."""
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


def test_a_removed_load_bearing_input_fails_hard_instead_of_being_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RemovedInputError escapes the health probe. Ordinary resolution faults are
    reported as degraded to avoid healthcheck crash loops. A retired but set
    TICKETS_TRACKER_DIR instead means the server may read an unexpected store.
    Reporting it as absent would hide that misconfiguration. The explicit
    propagation also guards against a future broader exception handler."""
    from rebar._deprecations import RemovedInputError

    def _boom() -> str:
        raise RemovedInputError("TICKETS_TRACKER_DIR is set but was removed")

    # store_status imports `rebar.config` INSIDE the function, so patch the module itself.
    import rebar.config as _cfg

    monkeypatch.setattr(_cfg, "tracker_dir_override", _boom)

    with pytest.raises(RemovedInputError):
        store_status()
