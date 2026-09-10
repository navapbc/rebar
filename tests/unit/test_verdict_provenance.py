"""Verify signed verdicts record provider provenance beside the model identifier.

The additive record contains provider, endpoint host, tier, and effective capabilities.
Assertions target serialized payload values so internal refactoring does not weaken the contract.
"""

from __future__ import annotations

import json

import pytest

from rebar.llm.capabilities import ModelCapabilities

CAPS = ModelCapabilities(
    native_structured_output=True,
    prompt_cache_style="anthropic",
    supports_thinking=False,
    supports_temperature=True,
)


def _provenance(**kw):
    """Import at call time so a missing symbol fails the TEST, not collection of the module."""
    from rebar.llm.capabilities import provenance_for

    return provenance_for(**kw)


# ── happy path (the ONLY test handed to the implementer) ──────────────────────────────────
def test_provenance_record_carries_provider_model_tier_and_capabilities() -> None:
    """Happy path: a first-class provider with no custom endpoint yields the five documented
    fields. ``tier`` is ``first_class`` and ``endpoint_host`` is None when no base_url is set."""
    rec = _provenance(
        provider="anthropic", model="anthropic:claude-opus-4-8", base_url=None, caps=CAPS
    )
    assert rec["provider"] == "anthropic"
    assert rec["model"] == "anthropic:claude-opus-4-8"
    assert rec["tier"] == "first_class"
    assert rec["endpoint_host"] is None
    # the EFFECTIVE record, not a recomputation — every capability field is carried through
    assert rec["capabilities"]["native_structured_output"] is True
    assert rec["capabilities"]["prompt_cache_style"] == "anthropic"
    assert rec["capabilities"]["supports_thinking"] is False
    assert rec["capabilities"]["supports_temperature"] is True


# ── HELD OUT from the implementer ─────────────────────────────────────────────────────────
def test_custom_endpoint_is_best_effort_and_records_host_only() -> None:
    """Verify a configured endpoint records ``best_effort`` tier and only its hostname."""
    rec = _provenance(
        provider="openai",
        model="openai:local-model",
        base_url="http://localhost:1234/v1",
        caps=CAPS,
    )
    assert rec["tier"] == "best_effort"
    assert rec["endpoint_host"] == "localhost"
    assert "1234" not in str(rec["endpoint_host"])
    assert "/v1" not in str(rec["endpoint_host"])


def test_credentials_in_base_url_never_reach_the_record() -> None:
    """Verify serialized provenance excludes credentials embedded in an endpoint URL."""
    rec = _provenance(
        provider="openai",
        model="openai:gpt-4o",
        base_url="https://alice:hunter2@gateway.internal:8443/v1",
        caps=CAPS,
    )
    assert rec["endpoint_host"] == "gateway.internal"
    blob = json.dumps(rec)
    assert "hunter2" not in blob, "password leaked into the provenance record"
    assert "alice" not in blob, "username leaked into the provenance record"
    assert "@" not in str(rec["endpoint_host"])


def test_api_key_is_never_carried_in_the_record() -> None:
    """HELD OUT. ``cfg.api_key`` flows near this path (providers.py places it on the OpenAI
    provider), so the record must not pick it up even incidentally."""
    rec = _provenance(
        provider="openai", model="openai:gpt-4o", base_url="https://host/v1", caps=CAPS
    )
    blob = json.dumps(rec)
    assert "sk-" not in blob
    assert "api_key" not in blob


@pytest.mark.parametrize(
    ("provider", "model", "expected_tier"),
    [
        ("anthropic", "anthropic:claude-opus-4-8", "first_class"),
        ("bedrock", "bedrock:us.anthropic.claude-sonnet-4-6", "first_class"),
    ],
)
def test_first_class_providers_are_tiered_first_class(provider, model, expected_tier) -> None:
    """Verify Bedrock uses the ``first_class`` tier expected by verdict consumers."""
    rec = _provenance(provider=provider, model=model, base_url=None, caps=CAPS)
    assert rec["tier"] == expected_tier
    assert rec["provider"] == provider


def test_capabilities_are_the_passed_record_not_a_recomputation() -> None:
    """Verify provenance carries the resolved capability record without recomputing it."""
    odd = ModelCapabilities(
        native_structured_output=False,
        prompt_cache_style="bedrock",
        supports_thinking=True,
        supports_temperature=False,
        native_web_search=True,
    )
    rec = _provenance(provider="bedrock", model="bedrock:x", base_url=None, caps=odd)
    assert rec["capabilities"] == {
        "native_structured_output": False,
        "prompt_cache_style": "bedrock",
        "supports_thinking": True,
        "supports_temperature": False,
        # `_provenance` received no web attachment, so the record must report `off`
        # even when the model profile supports native web search.
        "web_access": "off",
    }


def test_record_is_json_serializable() -> None:
    """HELD OUT. It is persisted into a signed sidecar payload, so a dataclass or any other
    non-serializable value in `capabilities` would break the write at runtime rather than here."""
    rec = _provenance(provider="anthropic", model="anthropic:m", base_url=None, caps=CAPS)
    json.loads(json.dumps(rec))  # raises if any value is not serializable


# ── the PERSISTED PAYLOAD, not just the assembled record ──────────────────────────────────
def _credentialed_provenance():
    from rebar.llm.capabilities import provenance_for

    return provenance_for(
        provider="openai",
        model="openai:gpt-4o",
        base_url="https://alice:hunter2@gateway.internal:8443/v1",
        caps=CAPS,
    )


def test_no_credential_material_in_the_persisted_completion_payload() -> None:
    """Verify serialized completion payloads exclude endpoint and API-key credentials."""
    from rebar.llm import completion_sidecar

    verdict = {
        "verdict": "PASS",
        "runner": "pydantic_ai",
        "model": "openai:gpt-4o",
        "provider_provenance": _credentialed_provenance(),
        "criteria": [],
        "findings": [],
        "summary": "ok",
    }
    payload = completion_sidecar.build_payload(verdict)
    blob = json.dumps(payload)
    assert "hunter2" not in blob, "password reached the PERSISTED payload"
    assert "alice" not in blob, "username reached the PERSISTED payload"
    assert "sk-" not in blob, "an api-key-shaped secret reached the PERSISTED payload"
    # the provenance itself still made it through, so this is not passing by omission
    assert payload["provider_provenance"]["endpoint_host"] == "gateway.internal"


def test_legacy_payload_without_provenance_still_builds_and_signs() -> None:
    """Verify payloads without provenance still build and produce verifiable manifests."""
    from rebar.llm import completion_sidecar

    legacy = {
        "verdict": "PASS",
        "runner": "pydantic_ai",
        "model": "anthropic:claude-opus-4-8",
        "criteria": [],
        "findings": [],
        "summary": "ok",
    }
    assert "provider_provenance" not in legacy
    payload = completion_sidecar.build_payload(legacy)
    # loads cleanly, round-trips, and the pre-existing model field is untouched
    assert json.loads(json.dumps(payload))["model"] == "anthropic:claude-opus-4-8"
    # absence is representable, never an error
    assert payload.get("provider_provenance") is None


# Gateway tiering uses membership in the admitted provider registry.
# Unrecognized gateway-shaped names receive no gateway semantics.


def _gateway_provider_names() -> list[str]:
    """The `gateway/*` qualifiers rebar actually admits, read from the config allowlist rather
    than hardcoded — a sixth gateway added there must be covered here automatically."""
    from rebar.llm.config import KNOWN_PROVIDER_NAMES

    return sorted(n for n in KNOWN_PROVIDER_NAMES if n.startswith("gateway/"))


def test_the_gateway_provider_family_is_non_empty() -> None:
    """Guard for the parametrized tests below: if the allowlist ever loses its `gateway/*`
    entries, the tier tests must fail loudly rather than silently parametrize over nothing."""
    assert len(_gateway_provider_names()) >= 5


def test_the_enumerated_gateway_set_matches_the_config_registry_exactly() -> None:
    """Verify the enumerated gateway set matches the admitted provider registry.

    This keeps membership-based tiering synchronized without prefix logic in
    ``capabilities.py``.
    """
    from rebar.llm.capabilities import _GATEWAY_PROVIDER_NAMES

    assert set(_GATEWAY_PROVIDER_NAMES) == set(_gateway_provider_names())


def test_an_unadmitted_gateway_lookalike_is_not_granted_gateway_semantics() -> None:
    """Verify an unadmitted ``gateway/`` lookalike receives no gateway semantics."""
    from rebar.llm.capabilities import _GATEWAY_PROVIDER_NAMES
    from rebar.llm.config import KNOWN_PROVIDER_NAMES

    typo = "gateway/nonsense"
    assert typo not in KNOWN_PROVIDER_NAMES, "config is the gate that makes this unreachable"
    assert typo not in _GATEWAY_PROVIDER_NAMES


@pytest.mark.parametrize("provider", _gateway_provider_names())
def test_gateway_provider_without_base_url_is_best_effort(provider: str) -> None:
    """THE defect. A `gateway/anthropic:claude-opus-4-8` run carries no `base_url`, so the
    base_url-only rule stamped it `first_class`. Every gateway qualifier must tier
    `best_effort`, because rebar cannot vouch for what the intermediary sent upstream."""
    rec = _provenance(
        provider=provider, model=f"{provider}:claude-opus-4-8", base_url=None, caps=CAPS
    )
    assert rec["tier"] == "best_effort", (
        f"{provider} traverses an opaque intermediary — it cannot sign as first_class"
    )
    assert rec["provider"] == provider


def test_gateway_tier_is_not_decided_by_the_provider_names_shape() -> None:
    """Verify provider name shape does not change tier without gateway membership."""
    for direct in ("mygateway", "openai-gateway", "gateway", "gateway-anthropic"):
        rec = _provenance(provider=direct, model=f"{direct}:m", base_url=None, caps=CAPS)
        assert rec["tier"] == "first_class", f"{direct} is not an enumerated gateway qualifier"


@pytest.mark.parametrize(
    "provider", ["anthropic", "bedrock", "openai", "google-cloud", "groq", "vertexai"]
)
def test_direct_providers_with_no_base_url_stay_first_class(provider: str) -> None:
    """The no-collateral-damage half. Every non-gateway qualifier with no custom endpoint
    keeps signing `first_class`; the fix must narrow the rule, not invert it."""
    rec = _provenance(provider=provider, model=f"{provider}:m", base_url=None, caps=CAPS)
    assert rec["tier"] == "first_class"


@pytest.mark.parametrize("provider", _gateway_provider_names())
def test_gateway_endpoint_host_stays_none_unless_a_base_url_was_actually_configured(
    provider: str,
) -> None:
    """Verify gateway provenance omits ``endpoint_host`` when no ``base_url`` was observed.

    Provider and tier identify the intermediary without placing an inferred hostname in the
    signed record.
    """
    rec = _provenance(provider=provider, model=f"{provider}:m", base_url=None, caps=CAPS)
    assert rec["endpoint_host"] is None
    assert rec["provider"].startswith("gateway/"), "the record still names the intermediary"

    with_url = _provenance(
        provider=provider,
        model=f"{provider}:m",
        base_url="https://gw.example.test:8443/proxy",
        caps=CAPS,
    )
    assert with_url["endpoint_host"] == "gw.example.test"
    assert with_url["tier"] == "best_effort"


def test_gateway_credentials_in_a_configured_base_url_never_reach_the_record() -> None:
    """The gateway arm inherits the security oracle: adding a provider-name branch must not
    route around `urlparse(...).hostname`, which strips userinfo."""
    rec = _provenance(
        provider="gateway/anthropic",
        model="gateway/anthropic:claude-opus-4-8",
        base_url="https://alice:hunter2@gw.internal:8443/proxy",
        caps=CAPS,
    )
    assert "hunter2" not in json.dumps(rec)
    assert "alice" not in json.dumps(rec)
    assert rec["endpoint_host"] == "gw.internal"


# Bedrock provenance records the resolved region and the source that supplied it.


def _strip_region_env(monkeypatch) -> None:
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(var, raising=False)


def test_bedrock_record_carries_the_region_and_the_rebar_knob_as_source(monkeypatch) -> None:
    """Happy path: the configured knob resolves, and BOTH additive keys land verbatim."""
    _strip_region_env(monkeypatch)
    rec = _provenance(
        provider="bedrock",
        model="bedrock:us.anthropic.claude-sonnet-4-6",
        base_url=None,
        caps=CAPS,
        bedrock_region_name="us-east-1",
    )
    assert rec["region"] == "us-east-1"
    assert rec["region_source"] == "REBAR_LLM_BEDROCK_REGION"


@pytest.mark.parametrize("source", ["repo-config", "cli"])
def test_bedrock_record_carries_the_configured_regions_true_origin(monkeypatch, source) -> None:
    """Verify a configured Bedrock region retains its resolved configuration source."""
    _strip_region_env(monkeypatch)
    rec = _provenance(
        provider="bedrock",
        model="bedrock:us.anthropic.claude-sonnet-4-6",
        base_url=None,
        caps=CAPS,
        bedrock_region_name="us-east-1",
        bedrock_region_source=source,
    )
    assert rec["region"] == "us-east-1"
    assert rec["region_source"] == source


def test_bedrock_region_source_alone_never_conjures_region_keys(monkeypatch) -> None:
    """A threaded source label without a resolved VALUE records nothing — the keys stay
    gated on the region itself, so a stale label cannot smuggle a guess into a signed record."""
    _strip_region_env(monkeypatch)
    rec = _provenance(
        provider="bedrock",
        model="bedrock:us.anthropic.claude-sonnet-4-6",
        base_url=None,
        caps=CAPS,
        bedrock_region_name=None,
        bedrock_region_source="repo-config",
    )
    assert "region" not in rec
    assert "region_source" not in rec


@pytest.mark.parametrize(
    ("var", "value"),
    [("AWS_DEFAULT_REGION", "eu-west-1"), ("AWS_REGION", "us-west-2")],
)
def test_bedrock_record_names_the_env_var_that_supplied_the_region(
    monkeypatch, var: str, value: str
) -> None:
    """Each env source is recorded under ITS OWN name — the label is the audit trail."""
    _strip_region_env(monkeypatch)
    monkeypatch.setenv(var, value)
    rec = _provenance(
        provider="bedrock",
        model="bedrock:us.anthropic.claude-sonnet-4-6",
        base_url=None,
        caps=CAPS,
        bedrock_region_name=None,
    )
    assert rec["region"] == value
    assert rec["region_source"] == var


def test_bedrock_record_omits_region_keys_when_only_a_profile_could_resolve(
    monkeypatch,
) -> None:
    """Verify region fields stay absent when rebar does not resolve a region.

    Profile-derived values are outside this path and must not be inferred.
    """
    _strip_region_env(monkeypatch)
    rec = _provenance(
        provider="bedrock",
        model="bedrock:us.anthropic.claude-sonnet-4-6",
        base_url=None,
        caps=CAPS,
        bedrock_region_name=None,
    )
    assert "region" not in rec
    assert "region_source" not in rec


def test_non_bedrock_records_never_carry_region_keys(monkeypatch) -> None:
    """The runner threads `bedrock_region_name` unconditionally, so the PROVIDER check must
    gate the record: an anthropic run with region env set records nothing region-shaped."""
    _strip_region_env(monkeypatch)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    rec = _provenance(
        provider="anthropic",
        model="anthropic:claude-opus-4-8",
        base_url=None,
        caps=CAPS,
        bedrock_region_name="us-east-1",
    )
    assert "region" not in rec
    assert "region_source" not in rec


def test_region_bearing_record_stays_json_serializable(monkeypatch) -> None:
    """The record is embedded in signed sidecar payloads; the additive keys must not break
    serialization."""
    _strip_region_env(monkeypatch)
    rec = _provenance(
        provider="bedrock",
        model="bedrock:us.anthropic.claude-sonnet-4-6",
        base_url=None,
        caps=CAPS,
        bedrock_region_name="us-east-1",
    )
    assert json.loads(json.dumps(rec))["region_source"] == "REBAR_LLM_BEDROCK_REGION"
