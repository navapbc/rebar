"""Configuration surface for optional plan-material-pin enforcement."""

import logging
from types import SimpleNamespace

import pytest

from rebar import _config_schema, config
from rebar.llm.plan_review import attest, registry
from rebar.llm.plan_review.relation_snapshot import PlanMaterialPin


def test_enforcement_key_is_registered_and_defaults_off() -> None:
    assert _config_schema.VerifyConfig().enforce_plan_material_pins is False
    coercer = _config_schema._SECTIONS["verify"]["enforce_plan_material_pins"]
    assert coercer(True, "verify.enforce_plan_material_pins") is True
    assert coercer(False, "verify.enforce_plan_material_pins") is False


def test_unreadable_optional_config_warns_structurally_and_returns_false(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        config,
        "load_config",
        lambda *a, **k: (_ for _ in ()).throw(config.ConfigError("bad bool")),
    )
    with caplog.at_level(logging.WARNING):
        assert attest._read_enforce_plan_material_pins("/repo") is False
    record = next(r for r in caplog.records if getattr(r, "event", None))
    assert record.event == "plan_material_pin_config_unreadable"


def test_signing_emits_identical_complete_pins_across_enforcement_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = PlanMaterialPin("child", "aaaa-bbbb-cccc-dddd", "1111111111111111")
    snapshot = SimpleNamespace(related_material=(pin,))
    manifests: list[list[str]] = []
    monkeypatch.setattr(attest, "dependency_hashes", lambda *a, **k: {})
    monkeypatch.setattr(attest, "registry_version", lambda *a, **k: "registry")
    monkeypatch.setattr(registry, "disabled_builtins", lambda *a, **k: [])
    monkeypatch.setattr("rebar.llm.config.current_code_sha", lambda: "a" * 40)
    monkeypatch.setattr(
        "rebar.signing.sign_manifest",
        lambda ticket_id, manifest, **kwargs: manifests.append(manifest) or {"signed": True},
    )

    verdict = {"verdict": "PASS", "ticket_id": "1111-2222-3333-4444", "coverage": {}}
    for enforced in (False, True):
        monkeypatch.setattr(
            config,
            "load_config",
            lambda *a, value=enforced, **k: SimpleNamespace(
                verify=SimpleNamespace(enforce_plan_material_pins=value)
            ),
        )
        assert attest._read_enforce_plan_material_pins("/repo") is enforced
        attest.sign_plan_review(
            verdict.copy(),
            material="2222222222222222",
            repo_root="/repo",
            relation_snapshot=snapshot,
        )

    assert manifests[0] == manifests[1]
    assert attest.manifest_pins(manifests[0]) == [pin]
