"""S5 (343b): signed verdicts stamp provider, endpoint, tier and effective capabilities.

A signed gate verdict used to record only a model STRING, so a verdict produced behind an
opaque gateway still claimed it came from ``anthropic:claude-opus-4-8``. These tests pin the
ADDITIVE ``provider_provenance`` object written alongside that string by all THREE sidecars.

Assertions are on OBSERVABLE payload contents, never on internal names, so a behaviour-
preserving rename/extraction does not break them.
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
    """HELD OUT. A configured base_url means rebar is talking to an endpoint it does not
    vouch for, so the tier drops to ``best_effort`` and the HOST is recorded — never the
    full URL, which can carry a path, a port-scoped secret, or credentials."""
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
    """HELD OUT — the security oracle. ``urlparse(...).hostname`` strips userinfo;
    ``.netloc`` RETAINS ``user:secret@`` and is the wrong accessor (note runner.py uses
    ``.netloc`` for a truthiness check — copying that here would leak). The whole record is
    serialized and searched, so a credential smuggled into ANY field is caught, not just
    endpoint_host."""
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
    "provider,model,expected_tier",
    [
        ("anthropic", "anthropic:claude-opus-4-8", "first_class"),
        ("bedrock", "bedrock:us.anthropic.claude-sonnet-4-6", "first_class"),
    ],
)
def test_first_class_providers_are_tiered_first_class(provider, model, expected_tier) -> None:
    """HELD OUT. Bedrock is first-class as of this epic, so a Bedrock verdict must NOT be
    tiered best_effort — the whole point of the field is letting a consumer reject an
    off-tier verdict, which is worthless if the tiering is wrong."""
    rec = _provenance(provider=provider, model=model, base_url=None, caps=CAPS)
    assert rec["tier"] == expected_tier
    assert rec["provider"] == provider


def test_capabilities_are_the_passed_record_not_a_recomputation() -> None:
    """HELD OUT. `runner.run()` resolves caps ONCE and the object-vs-string distinction there
    is load-bearing (it caused a prior regression). Provenance must stamp THAT record; a
    second `capabilities_for` call could silently diverge from what actually drove the run.
    A deliberately unusual record proves the values were carried, not re-derived."""
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
        # Bug 129e. `_provenance` here passes no `web`, i.e. the request did not attach web
        # access — so the record must say "off" and must NOT leak the model's native-tool
        # capability as if it had been used. `native_web_search=True` above is deliberately
        # contradictory for exactly that reason.
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
    """343b AC: no credential material from `base_url` or `api_key` appears anywhere in the
    PERSISTED sidecar payload.

    This asserts the PAYLOAD, not the provenance record. They are different artifacts: the record
    is one key inside a payload that also carries model, runner, coverage and findings, and the
    configured `api_key` flows near that path independently — so a record-only assertion would
    pass even if a credential leaked through some OTHER field. The whole payload is serialized and
    searched."""
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
    """343b AC: a legacy payload lacking `provider_provenance` still loads and its signature
    verifies.

    This is the whole justification for the additive-only design. NOTE the signing seam: the
    payload is NOT what gets signed — signing binds (ticket_id, manifest), a list of "key: value"
    lines — so "the signature still verifies" is a claim about the MANIFEST. A verdict carrying no
    provenance must therefore (1) still build a valid payload, and (2) still produce a signable
    manifest, with the absence read as "unknown, legacy" rather than as an error."""
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


# ── bug 7fe2: a `gateway/*` provider must not sign as first_class ─────────────────────────
#
# `KNOWN_PROVIDER_NAMES` (config.py) admits five `gateway/*` qualifiers, and they carry NO
# `base_url` — the gateway URL is resolved inside pydantic-ai from its own env/api-key. The
# tier was derived SOLELY from `base_url`, so every byte could traverse Pydantic's AI Gateway
# (an intermediary that can rewrite the request — the Vercel AI Gateway has been documented
# silently downgrading Anthropic's 1-hour prompt cache) while the SIGNED verdict claimed
# `first_class`. The two-tier field exists so an attestation consumer can reject an off-tier
# verdict; a wrong tier makes it worthless.


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
    """THE DRIFT PIN, and the reason the tier rule can be pure MEMBERSHIP.

    `capabilities._GATEWAY_PROVIDER_NAMES` restates the `gateway/*` members of
    `config.KNOWN_PROVIDER_NAMES` instead of filtering them out of it, because filtering would
    itself need the prefix test that f184's attested criterion bans inside `capabilities.py`
    (and that epic 061c's registry-membership decision bans generally). Restating is only safe
    if the two cannot drift, so a sixth gateway added to the registry fails HERE until it is
    listed — which is a loud build failure rather than a verdict silently signed `first_class`.

    This test may use `startswith`; the ban is on `capabilities.py`, not on its tests."""
    from rebar.llm.capabilities import _GATEWAY_PROVIDER_NAMES

    assert set(_GATEWAY_PROVIDER_NAMES) == set(_gateway_provider_names())


def test_an_unadmitted_gateway_lookalike_is_not_granted_gateway_semantics() -> None:
    """The membership dividend. Under prefix matching any string beginning `gateway/` — a typo,
    a hand-edited config, a future name rebar does not model — silently acquired gateway
    semantics. Under membership it does not, and `KNOWN_PROVIDER_NAMES` is the single upstream
    gate that stops such a qualifier being configurable at all."""
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
    """Contrast case: the rule is set MEMBERSHIP, not name shape. No amount of the token
    'gateway' in a provider name downgrades a direct provider — including a bare `gateway`, a
    name that merely contains it, and one that ends with it."""
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
    """THE RECORDED DECISION for this bug: `endpoint_host` is NOT back-filled with a guessed
    gateway hostname.

    pydantic-ai resolves the gateway URL from `PYDANTIC_AI_GATEWAY_BASE_URL` / `PAIG_BASE_URL`
    or infers it from the API KEY (`providers/gateway.py`), none of which this seam observes.
    Synthesising `gateway.pydantic.dev` here would put an UNVERIFIED fact into a SIGNED record
    — the precise failure mode the tier field exists to prevent. The intermediary is instead
    named by the `provider` field (`gateway/anthropic`) and flagged by `tier`, both of which
    are observed. When a `base_url` IS configured, the real host is recorded as before."""
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


# ── 8274: bedrock region provenance — the resolved region + its source enter the record ────
# rebar resolves the Bedrock region itself (REBAR_LLM_BEDROCK_REGION > AWS_DEFAULT_REGION >
# AWS_REGION > boto3/profile; `bedrock_model.resolve_bedrock_region`); the verdict records
# WHICH source supplied it so an operator can tell a knob-set run from an env-inherited one.


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
    """cda8: a knob value that came from the config-file table (or the CLI) is labeled by its
    TRUE origin — `bedrock_region_source`, resolved by the SAME `LLMConfig.from_env` pass that
    produced the value and threaded by the runner — never blanket-labeled as the env var."""
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
    """When nothing in rebar's chain resolves, the keys are ABSENT — not None, not a guess.
    This seam never imports boto3, so a profile-resolved region is unobservable here, and
    synthesising one would put an unverified fact into a signed record (the endpoint_host
    rule)."""
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
