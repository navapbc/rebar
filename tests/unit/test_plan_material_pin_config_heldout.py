"""Held-out unreadable-config behavior."""

import logging

import pytest

from rebar import config
from rebar.llm.plan_review import attest


def test_unreadable_optional_config_warns_structurally_and_returns_false(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        config,
        "load_config",
        lambda *a, **k: (_ for _ in ()).throw(config.ConfigError("bad bool")),
    )
    reader = getattr(attest, "_read_enforce_plan_material_pins", None)
    assert callable(reader), "enforcement config read boundary is absent"
    with caplog.at_level(logging.WARNING):
        assert reader("/repo") is False
    record = next(r for r in caplog.records if getattr(r, "event", None))
    assert record.event == "plan_material_pin_config_unreadable"
