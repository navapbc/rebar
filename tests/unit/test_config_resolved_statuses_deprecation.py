"""The retired `resolved_statuses` config keys warn but keep loading (task 549c).

Task f020 deleted the inbound absence-probe port, orphaning the operator-facing
`jira.resolved_statuses` / `reconciler.resolved_statuses` keys. 549c decided DEPRECATE
rather than remove: the key was behaviour-affecting as recently as shipped v0.10.1 and the
docs of the day told self-hosted operators to set it, so an existing `pyproject.toml`
carrying it must not start hard-failing on upgrade. Hard removal (delete the field, add a
warn-class tombstone) is deferred to task f408-64ad-ee41-46b6, pending operator sign-off.

These tests pin BOTH halves of that decision, because each half is silently reversible:
loading must stay unchanged, and the warning must actually fire.
"""

from __future__ import annotations

import logging

import pytest

from rebar._config_schema import coerce_sparse
from rebar._deprecations import REGISTRY

DEPRECATED = [
    ("jira", "resolved_statuses"),
    ("reconciler", "resolved_statuses"),
]


@pytest.mark.parametrize(("section", "key"), DEPRECATED)
def test_the_key_is_registered_as_a_scheduled_retirement(section: str, key: str) -> None:
    """`warn_deprecated` raises KeyError on an unregistered surface, so the registry row
    is what makes the emission below possible at all."""
    dep = REGISTRY[f"cfg:{section}.{key}"]

    assert not dep.permanent, "this is a retirement with a removal horizon, not a rename"
    assert dep.replacement == "", "nothing supersedes it — the behaviour is simply gone"
    assert dep.remove_in


@pytest.mark.parametrize(("section", "key"), DEPRECATED)
def test_setting_the_key_still_loads_and_validates_unchanged(section: str, key: str) -> None:
    """The whole point of deprecating rather than removing: an existing config that sets
    the key keeps parsing, with the value coerced exactly as before."""
    out = coerce_sparse({section: {key: ["Shipped", "Abandoned"]}})

    assert out[section][key] == ["Shipped", "Abandoned"]


@pytest.mark.parametrize(("section", "key"), DEPRECATED)
def test_setting_the_key_emits_the_deprecation_warning(
    section: str, key: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A silent no-op key is the defect being fixed: an operator who set it must be told
    it does nothing and when it disappears."""
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        coerce_sparse({section: {key: ["Done"]}})

    messages = [r.getMessage() for r in caplog.records]
    assert any(f"{section}.{key}" in m and "deprecated" in m for m in messages), messages
    assert any("no longer has any effect" in m for m in messages), messages
    assert any(REGISTRY[f"cfg:{section}.{key}"].remove_in in m for m in messages), messages


@pytest.mark.parametrize(("section", "key"), DEPRECATED)
def test_the_key_is_not_reported_as_unknown(
    section: str, key: str, caplog: pytest.LogCaptureFixture
) -> None:
    """It must warn as DEPRECATED, not as an unrecognised key — the two mean different
    things to an operator, and the unknown-key path also hard-errors under
    REBAR_CONFIG_UNKNOWN_KEYS=error."""
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        coerce_sparse({section: {key: ["Done"]}}, strict=True)

    assert not any("unknown key" in r.getMessage() for r in caplog.records)


def test_an_unrelated_key_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """Guards the registry lookup in `coerce_sparse` against over-firing."""
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        coerce_sparse({"jira": {"project": "DIG"}})

    assert not any("deprecated" in r.getMessage() for r in caplog.records)
