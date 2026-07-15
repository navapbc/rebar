"""Guards the settled op-cert ADR artifact (story 6a89).

Asserts `docs/adr/0049-opcert-asymmetric.md` exists, is non-empty, cross-references
ADR 0046, and observably documents the settled operation-certificate model
(environment-attribution namespace, out-of-band trust file, the storage-anchor era
rule, uniform producer signing, and HMAC removal). Content is asserted, not merely
presence, so the ADR cannot silently regress to a stub.
"""

from __future__ import annotations

from pathlib import Path

ADR_PATH = Path(__file__).resolve().parents[2] / "docs" / "adr" / "0049-opcert-asymmetric.md"

# Strings the epic AC and story 6a89 require verbatim.
REQUIRED_STRINGS = (
    "rebar.opcert.v1",
    "trusted_environments.yaml",
    "ADR 0046",
)

# Key section keywords proving the settled model is actually documented, not just
# named. Matched case-insensitively against the body.
REQUIRED_TOPICS = (
    "environment-attribution",  # env-attribution model heading
    "storage anchor",  # authoritative era anchor (storage-position key validity)
    "era",  # era-validity / rotation vs kill-switch
    "uniform producer signing",  # round-6 producer-signing resolution
    "hmac removal",  # expand -> contract HMAC removal
    "docs/migrations/hmac-opcert-removal.md",  # the migration artifact reference
)


def test_opcert_adr_exists_and_is_non_empty() -> None:
    assert ADR_PATH.exists(), f"missing op-cert ADR at {ADR_PATH}"
    text = ADR_PATH.read_text(encoding="utf-8")
    # A real ADR, not a stub: comfortably longer than a placeholder.
    assert len(text) > 2000, f"op-cert ADR is suspiciously short ({len(text)} chars)"


def test_opcert_adr_contains_required_strings() -> None:
    text = ADR_PATH.read_text(encoding="utf-8")
    for needle in REQUIRED_STRINGS:
        assert needle in text, f"op-cert ADR missing required string: {needle!r}"


def test_opcert_adr_documents_settled_model() -> None:
    text = ADR_PATH.read_text(encoding="utf-8").lower()
    for topic in REQUIRED_TOPICS:
        assert topic.lower() in text, f"op-cert ADR does not cover topic: {topic!r}"
