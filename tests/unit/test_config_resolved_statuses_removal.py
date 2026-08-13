"""The `resolved_statuses` config keys are REMOVED but still load, warning (task f408).

Task f020 deleted the inbound absence-probe port, orphaning the operator-facing
`jira.resolved_statuses` / `reconciler.resolved_statuses` keys. Task 549c decided
DEPRECATE-not-remove and executed the deprecation half; the removal half was held for
operator sign-off, which was given on 2026-08-12, and this module replaces the
deprecation module that pinned the interim state.

Resolved/unresolved discrimination is outbound-owned (ADR 0028), so there is no
replacement key — an operator simply deletes the line. The tombstones are therefore
`warn`, not `error`: the keys are INERT (nothing read them, and the behaviour they
configured no longer exists), so an existing `pyproject.toml` carrying one must keep
loading. Both halves of THAT are silently reversible, which is what these tests pin:
the key must not hard-fail, and the warning must actually fire.
"""

from __future__ import annotations

import logging

import pytest

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
def test_the_key_is_tombstoned_warn_class(section: str, key: str) -> None:
    """`warn` rather than `error` is the load-bearing decision here: an inert key must not
    abort the command. Pinned so a later edit cannot quietly promote it to a hard failure
    and start breaking every config that still carries the line."""
    ri = tombstone_for("cfg", f"{section}.{key}")

    assert ri is not None, "the removed key must be tombstoned, not silently unknown"
    assert ri.behavior == "warn"
    assert ri.replacement == "", "discrimination is outbound-owned — nothing supersedes it"


@pytest.mark.parametrize("name", REMOVED_ENV)
def test_the_auto_derived_env_twin_is_tombstoned_warn_class(name: str) -> None:
    """The env twins auto-derived as REBAR_<SECTION>_<KEY>, so an operator may have
    exported one; it is retired on the same terms as the config key."""
    ri = tombstone_for("env", name)

    assert ri is not None
    assert ri.behavior == "warn"


@pytest.mark.parametrize(("section", "key"), REMOVED)
def test_the_deprecation_row_is_gone(section: str, key: str) -> None:
    """The key is retired, not deprecated: leaving the 549c row behind would keep claiming
    a removal horizon for something already removed."""
    assert f"cfg:{section}.{key}" not in REGISTRY


@pytest.mark.parametrize(("section", "key"), REMOVED)
def test_a_config_still_setting_the_key_loads_and_drops_it(section: str, key: str) -> None:
    """The point of a warn-class tombstone: an existing pyproject.toml keeps loading. The
    key is consumed by the tombstone path, so it never reaches the coercers."""
    out = coerce_sparse({section: {key: ["Shipped", "Abandoned"], "project": "DIG"}})

    assert key not in out.get(section, {})


@pytest.mark.parametrize(("section", "key"), REMOVED)
def test_setting_the_key_warns_that_it_was_removed(
    section: str, key: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A silently-ignored removed key is the defect the tombstone registry exists to
    prevent — the operator must be told the line is dead so they can delete it."""
    with caplog.at_level(logging.WARNING, logger="rebar"):
        coerce_sparse({section: {key: ["Done"]}})

    messages = [r.getMessage() for r in caplog.records]
    assert any(f"{section}.{key}" in m and "was removed in" in m for m in messages), messages


@pytest.mark.parametrize(("section", "key"), REMOVED)
def test_the_key_is_not_reported_as_unknown(
    section: str, key: str, caplog: pytest.LogCaptureFixture
) -> None:
    """It must report as REMOVED, not unrecognised — the two mean different things to an
    operator, and the unknown-key path also hard-errors under
    REBAR_CONFIG_UNKNOWN_KEYS=error, which would defeat the warn-class choice."""
    with caplog.at_level(logging.WARNING, logger="rebar"):
        coerce_sparse({section: {key: ["Done"]}}, strict=True)

    assert not any("unknown key" in r.getMessage() for r in caplog.records)


def test_an_unrelated_key_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """Guards the tombstone lookup in `coerce_sparse` against over-firing."""
    with caplog.at_level(logging.WARNING, logger="rebar"):
        coerce_sparse({"jira": {"project": "DIG"}})

    assert not any("was removed in" in r.getMessage() for r in caplog.records)
