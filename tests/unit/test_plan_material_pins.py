"""Contract tests for signed plan-material pin manifest lines."""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import attest
from rebar.llm.plan_review import manifest as manifest_module


def _api():
    missing = [
        name
        for name in ("ManifestFormatError", "manifest_pins")
        if not hasattr(manifest_module, name)
    ]
    if missing:
        pytest.fail(f"plan-material pin manifest API is absent: {', '.join(missing)}")
    try:
        from rebar.llm.plan_review.relation_snapshot import PlanMaterialPin
    except ModuleNotFoundError:
        pytest.fail("plan relation snapshot API is absent")
    return (
        manifest_module.ManifestFormatError,
        manifest_module.build_manifest,
        manifest_module.manifest_pins,
        PlanMaterialPin,
    )


def _verdict() -> dict:
    return {
        "verdict": "PASS",
        "ticket_id": "aaaa-bbbb-cccc-dddd",
        "model": "model",
        "runner": "runner",
        "coverage": {"counts": {"blocking": 0, "advisory_surfaced": 1}},
    }


def test_build_manifest_emits_sorted_pins_before_dependencies(monkeypatch) -> None:
    _, build_manifest, manifest_pins, PlanMaterialPin = _api()
    monkeypatch.setattr("rebar.signing.gate_code_version", lambda: "test-version")
    pins = [
        PlanMaterialPin("prerequisite", "ffff-eeee-dddd-cccc", "0123456789abcdef"),
        PlanMaterialPin("child", "bbbb-cccc-dddd-eeee", "fedcba9876543210"),
        PlanMaterialPin("child", "aaaa-bbbb-cccc-dddd", "1111111111111111"),
    ]

    manifest = build_manifest(
        _verdict(),
        material="2222222222222222",
        deps={"z.py": "digest"},
        verified_at_sha="a" * 40,
        pins=pins,
    )

    pin_lines = [line for line in manifest if line.startswith("plan-material-pin:")]
    assert pin_lines == [
        "plan-material-pin: child aaaa-bbbb-cccc-dddd 1111111111111111",
        "plan-material-pin: child bbbb-cccc-dddd-eeee fedcba9876543210",
        "plan-material-pin: prerequisite ffff-eeee-dddd-cccc 0123456789abcdef",
    ]
    assert manifest.index(pin_lines[-1]) < manifest.index("dep digest z.py")
    assert manifest.index("verified-at-sha:" + "a" * 40) < manifest.index(pin_lines[0])
    assert manifest_pins(manifest) == sorted(pins, key=lambda pin: (pin.role, pin.canonical_id))


def test_no_pins_preserves_legacy_manifest_bytes(monkeypatch) -> None:
    _, build_manifest, manifest_pins, _ = _api()
    monkeypatch.setattr("rebar.signing.gate_code_version", lambda: "test-version")
    kwargs = dict(
        material="2222222222222222",
        deps={"a.py": "digest"},
        regver="registry",
        refreshed_from="old probe=PASS",
        verified_at_sha="b" * 40,
    )
    assert build_manifest(_verdict(), **kwargs) == build_manifest(_verdict(), pins=(), **kwargs)
    assert manifest_pins(build_manifest(_verdict(), **kwargs)) == []


@pytest.mark.parametrize(
    "line",
    [
        "plan-material-pin:",
        "plan-material-pin: child aaaa-bbbb-cccc-dddd",
        "plan-material-pin: child aaaa-bbbb-cccc-dddd 0123456789abcdef extra",
        "plan-material-pin: parent aaaa-bbbb-cccc-dddd 0123456789abcdef",
        "plan-material-pin: child not-a-canonical-id 0123456789abcdef",
        "plan-material-pin: child aaaa-bbbb-cccc-dddd ABCDEF0123456789",
        "plan-material-pin: child aaaa-bbbb-cccc-dddd 0123456789abcde",
    ],
)
def test_manifest_pins_rejects_every_malformed_shape(line: str) -> None:
    ManifestFormatError, _, manifest_pins, _ = _api()
    with pytest.raises(ManifestFormatError):
        manifest_pins([line])


def test_manifest_pins_rejects_duplicate_role_and_id() -> None:
    ManifestFormatError, _, manifest_pins, _ = _api()
    line = "plan-material-pin: child aaaa-bbbb-cccc-dddd 0123456789abcdef"
    with pytest.raises(ManifestFormatError):
        manifest_pins([line, line])


def test_attest_reexports_pin_parser_and_error() -> None:
    ManifestFormatError, _, manifest_pins, _ = _api()
    assert attest.manifest_pins is manifest_pins
    assert attest.ManifestFormatError is ManifestFormatError


@pytest.mark.parametrize("canonical_id", ["jira-reb-1160", "jira-dig-123"])
def test_manifest_pins_accepts_canonical_jira_local_ids(canonical_id: str) -> None:
    _, build_manifest, manifest_pins, PlanMaterialPin = _api()
    pin = PlanMaterialPin("prerequisite", canonical_id, "0123456789abcdef")
    manifest = build_manifest(_verdict(), material="1111111111111111", pins=[pin])
    assert manifest_pins(manifest) == [pin]
