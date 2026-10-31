"""Removed ``resolved_statuses`` keys are ordinary unknown keys.

Outbound logic owns resolved-state discrimination, so the old Jira and reconciler
keys were retired with no replacement. They carried warn-class tombstones until the
warn class was retired wholesale: an inert retired name tells an operator nothing a
plain unknown-key warning does not, so it goes through the normal unknown-key path
and obeys ``REBAR_CONFIG_UNKNOWN_KEYS``."""

from __future__ import annotations

import logging

import pytest

from rebar._config_coercion import ConfigError
from rebar._config_schema import JiraConfig, ReconcilerConfig, coerce_sparse
from rebar._deprecations import REGISTRY, tombstone_for

REMOVED = [
    ("jira", "resolved_statuses"),
    ("reconciler", "resolved_statuses"),
]

REMOVED_ENV = ["REBAR_JIRA_RESOLVED_STATUSES", "REBAR_RECONCILER_RESOLVED_STATUSES"]


@pytest.mark.parametrize("cls", [JiraConfig, ReconcilerConfig])
def test_the_schema_field_is_gone(cls: type) -> None:
    """The removal proper: neither dataclass carries the field any more, so nothing can
    read a value that has had no meaning since task f020."""
    assert not hasattr(cls(), "resolved_statuses")


@pytest.mark.parametrize(("section", "key"), REMOVED)
def test_the_key_is_not_tombstoned(section: str, key: str) -> None:
    """No tombstone row survives. The registry is now error-class only — it is reserved
    for retired inputs that still change behaviour if honoured, which these cannot."""
    assert tombstone_for("cfg", f"{section}.{key}") is None


@pytest.mark.parametrize("name", REMOVED_ENV)
def test_the_auto_derived_env_twin_is_not_tombstoned(name: str) -> None:
    """The env twins auto-derived as REBAR_<SECTION>_<KEY>. They are never read, so a
    stale export is inert and carries no row either."""
    assert tombstone_for("env", name) is None


@pytest.mark.parametrize(("section", "key"), REMOVED)
def test_the_deprecation_row_is_gone(section: str, key: str) -> None:
    """The key is retired, not deprecated: leaving the 549c row behind would keep claiming
    a removal horizon for something already removed."""
    assert f"cfg:{section}.{key}" not in REGISTRY


@pytest.mark.parametrize(("section", "key"), REMOVED)
def test_a_config_still_setting_the_key_loads_and_drops_it(section: str, key: str) -> None:
    """An existing pyproject.toml keeps loading under the default policy. The key is
    unknown, so it is dropped rather than reaching the coercers."""
    out = coerce_sparse({section: {key: ["Shipped", "Abandoned"], "project": "DIG"}})

    assert key not in out.get(section, {})


@pytest.mark.parametrize(("section", "key"), REMOVED)
def test_setting_the_key_warns_as_an_unknown_key(
    section: str, key: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A silently-ignored dead key is still the defect to prevent — the operator must be
    told the line does nothing so they can delete it. The channel is now the unknown-key
    warning rather than a bespoke tombstone message."""
    with caplog.at_level(logging.WARNING, logger="rebar"):
        coerce_sparse({section: {key: ["Done"]}})

    messages = [r.getMessage() for r in caplog.records]
    assert any(f"{section}.{key}" in m and "unknown key" in m for m in messages), messages


@pytest.mark.parametrize(("section", "key"), REMOVED)
def test_the_key_obeys_the_strict_unknown_key_policy(section: str, key: str) -> None:
    """The deliberate behaviour change: an operator who opted into
    REBAR_CONFIG_UNKNOWN_KEYS=error now gets a hard failure naming the dead key, instead
    of the warn-class tombstone suppressing it."""
    with pytest.raises(ConfigError, match=f"{section}.{key}"):
        coerce_sparse({section: {key: ["Done"]}}, strict=True)


def test_an_unrelated_key_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """Guards the lookup in `coerce_sparse` against over-firing."""
    with caplog.at_level(logging.WARNING, logger="rebar"):
        coerce_sparse({"jira": {"project": "DIG"}})

    assert not any("was removed in" in r.getMessage() for r in caplog.records)
