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
    )
    rec = _provenance(provider="bedrock", model="bedrock:x", base_url=None, caps=odd)
    assert rec["capabilities"] == {
        "native_structured_output": False,
        "prompt_cache_style": "bedrock",
        "supports_thinking": True,
        "supports_temperature": False,
    }


def test_record_is_json_serializable() -> None:
    """HELD OUT. It is persisted into a signed sidecar payload, so a dataclass or any other
    non-serializable value in `capabilities` would break the write at runtime rather than here."""
    rec = _provenance(provider="anthropic", model="anthropic:m", base_url=None, caps=CAPS)
    json.loads(json.dumps(rec))  # raises if any value is not serializable
