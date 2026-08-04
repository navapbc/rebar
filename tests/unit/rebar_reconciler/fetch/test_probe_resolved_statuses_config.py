"""Story e34a: the Cloud absence probe's resolved-status set is configurable.

Parity with the DC side, which already sources its set from
``reconciler.resolved_statuses`` (``adapters/jira_datacenter/settings.py``). Cloud
must read a ``jira.resolved_statuses`` config key that DEFAULTS to the historical
three names, so a tenant whose workflow uses non-standard resolved-status names
(e.g. ``Closed``/``Complete``) classifies a still-present resolved issue as
PRESENT_RESOLVED — not PRESENT_FILTERED — WITHOUT a code change.

These pin OBSERVABLE classifier behaviour through ``jira.probe.classify_probe_response``
(the Cloud-bound function the real probe and dispatch use), driving config via a
monkeypatched ``rebar.config.load_config`` (the import in the probe is FUNCTION-LOCAL,
so the name resolves against ``rebar.config`` at call time — patch it there, mirroring
``test_jira_dc_config_settings.py``).
"""

from __future__ import annotations

import pytest

from rebar_reconciler.adapters.jira import probe as jira_probe
from rebar_reconciler.inbound_probe import ProbeBranch

pytestmark = pytest.mark.unit


def _payload(status_name: str) -> dict:
    return {"fields": {"status": {"name": status_name}}}


def _patch_jira_resolved_statuses(monkeypatch, value: list[str]) -> None:
    """Point ``rebar.config.load_config().jira.resolved_statuses`` at ``value``."""
    import rebar.config as _config
    from rebar._config_schema import Config, JiraConfig

    cfg = Config(jira=JiraConfig(resolved_statuses=value))
    monkeypatch.setattr(_config, "load_config", lambda *a, **k: cfg)


# --- AC: a custom workflow's resolved names reach the Cloud classifier --------------------


def test_a_custom_resolved_status_name_classifies_present_resolved(monkeypatch) -> None:
    """A tenant configuring ["Closed","Complete"] gets a "Closed" issue as PRESENT_RESOLVED."""
    _patch_jira_resolved_statuses(monkeypatch, ["Closed", "Complete"])

    result = jira_probe.classify_probe_response("PROJ-1", 200, _payload("Closed"))

    assert result.branch is ProbeBranch.PRESENT_RESOLVED


def test_the_configured_set_replaces_rather_than_extends_the_default(monkeypatch) -> None:
    """Under a custom set, a historical name NOT in it classifies PRESENT_FILTERED.

    Proves the configured value is actually consulted — a value that were merged with
    (or ignored in favour of) the hardcoded default would leave "Done" resolved.
    """
    _patch_jira_resolved_statuses(monkeypatch, ["Closed", "Complete"])

    result = jira_probe.classify_probe_response("PROJ-2", 200, _payload("Done"))

    assert result.branch is ProbeBranch.PRESENT_FILTERED


# --- AC: default fallback when unset / empty ---------------------------------------------


@pytest.mark.parametrize("status_name", ["Resolved", "Done", "Cancelled"])
def test_unset_config_preserves_the_historical_default(monkeypatch, status_name: str) -> None:
    """With the key at its default, the three historical names stay PRESENT_RESOLVED."""
    import rebar.config as _config
    from rebar._config_schema import Config

    monkeypatch.setattr(_config, "load_config", lambda *a, **k: Config())

    result = jira_probe.classify_probe_response("PROJ-3", 200, _payload(status_name))

    assert result.branch is ProbeBranch.PRESENT_RESOLVED


def test_an_explicitly_empty_list_falls_back_to_the_default(monkeypatch) -> None:
    """An empty configured list must NOT bind an empty set (which would classify every
    resolved issue PRESENT_FILTERED — the bug inverted). It falls back to the default,
    mirroring DC's ``... if reconciler.resolved_statuses else DEFAULT`` guard."""
    _patch_jira_resolved_statuses(monkeypatch, [])

    result = jira_probe.classify_probe_response("PROJ-4", 200, _payload("Done"))

    assert result.branch is ProbeBranch.PRESENT_RESOLVED


def test_a_malformed_config_falls_back_rather_than_raising(monkeypatch) -> None:
    """A ConfigError while loading must degrade to the module default, not break the
    probe pass (a probe classification is not the place to surface a config typo)."""
    import rebar.config as _config
    from rebar.config import ConfigError

    def _raise(*_a, **_k):
        raise ConfigError("jira.resolved_statuses: malformed value")

    monkeypatch.setattr(_config, "load_config", _raise)

    result = jira_probe.classify_probe_response("PROJ-5", 200, _payload("Done"))

    assert result.branch is ProbeBranch.PRESENT_RESOLVED
