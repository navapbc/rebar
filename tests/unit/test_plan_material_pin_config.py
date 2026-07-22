"""Configuration surface for optional plan-material-pin enforcement."""

from rebar import _config_schema


def test_enforcement_key_is_registered_and_defaults_off() -> None:
    assert _config_schema.VerifyConfig().enforce_plan_material_pins is False
    coercer = _config_schema._SECTIONS["verify"]["enforce_plan_material_pins"]
    assert coercer(True, "verify.enforce_plan_material_pins") is True
    assert coercer(False, "verify.enforce_plan_material_pins") is False
