"""Construct the AWS Bedrock provider from an inference-profile model id.

Bedrock rejects plain on-demand Claude ids, so callers must use a ``us.`` or ``global.``
inference profile. Authentication always uses the ambient AWS credential chain and never a
rebar-managed API key. Heavy dependencies are imported inside their consumers, and this leaf
does not import ``runner`` at runtime.
"""

from __future__ import annotations

import logging
import os

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError

logger = logging.getLogger(__name__)

# The documented default Bedrock model id — an INFERENCE-PROFILE id (the `us.` prefix), never
# a bare on-demand id, per the module docstring's MEASURED ValidationException. MEASURED to
# cache (ticket 2932).
DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-6"


def resolve_bedrock_region(
    bedrock_region_name: str | None, *, configured_source: str | None = None
) -> tuple[str | None, str | None]:
    """Resolve Bedrock region and provenance source without importing boto3.

    Precedence is ``bedrock_region_name``, ``AWS_DEFAULT_REGION``, then ``AWS_REGION``. The
    configured arm retains its resolved ``configured_source``. Empty values are unset, and no
    match returns ``(None, None)`` for boto3 profile resolution. Rebar handles ``AWS_REGION``
    explicitly because botocore does not, while still refusing to invent a default region."""
    if bedrock_region_name:
        return bedrock_region_name, configured_source or "REBAR_LLM_BEDROCK_REGION"
    for var in ("AWS_DEFAULT_REGION", "AWS_REGION"):
        value = os.environ.get(var)
        if value:
            return value, var
    return None, None


def build_bedrock_provider(cfg: LLMConfig, *, session=None):
    """Build the bare ``BedrockProvider`` required by ``ProviderSession``.

    This function constructs the boto3 client so configured transport bounds reach botocore.
    ``llm_retry_max_attempts`` counts total attempts, clamps to one, and uses adaptive mode.
    ``timeout_s`` applies to both connect and read phases. ``llm_retry_max_wait_s`` is not mapped
    because botocore owns its backoff.

    The resolved region is passed explicitly, with no invented default. Authentication remains
    on the ambient AWS chain because no credential arguments are accepted. Boto3 clients require
    no run-teardown registration.
    """
    try:
        from pydantic_ai.providers.bedrock import BedrockProvider
    except ImportError as exc:
        # Selected-provider boundary (RP-05 S4): the ``bedrock`` model/provider was chosen, so
        # enforce the ``bedrock_provider`` semantic capability here. Its install guidance is
        # single-sourced from the capability registry (the pydantic-ai-slim form, not
        # nava-rebar[bedrock]) rather than hard-coded.
        from rebar._capabilities import install_hint

        raise LLMConfigError(
            "a bedrock model/provider is configured but the optional bedrock provider "
            f"package is not installed: {install_hint('bedrock_provider')}"
        ) from exc
    import boto3
    from botocore.config import Config as BotoConfig

    region, _region_source = resolve_bedrock_region(
        cfg.bedrock_region_name, configured_source=cfg.bedrock_region_source
    )
    # RP-04 S4: an injected caller-owned boto3 Session is used INSTEAD of constructing an
    # ambient one — `boto3.session.Session(...)` is never called on this path. The caller owns
    # the injected session's region resolution, so rebar's own region pre-check (which guards
    # only the ambient construction) is skipped; rebar still applies its documented
    # retry/timeout knobs to the client and never invents a default region.
    if session is None:
        session = boto3.session.Session(region_name=region)
        if not session.region_name:
            # Convert boto3's missing-region failure into a typed configuration error without
            # inventing a default region.
            raise LLMConfigError(
                "a bedrock model/provider is configured but no AWS region could be resolved. "
                "rebar resolves the region as REBAR_LLM_BEDROCK_REGION (rebar's own knob; the "
                "value and its source are recorded in the verdict's provider provenance) > "
                "AWS_DEFAULT_REGION > AWS_REGION > boto3's own resolution (the active profile's "
                "config), and none of those supplied one. Set REBAR_LLM_BEDROCK_REGION, or "
                "export AWS_DEFAULT_REGION or AWS_REGION. NOTE: instance-metadata (IMDS) "
                "reachability does not supply a region — credential discovery and region "
                "discovery are independent, so a working instance role does not remove the "
                "need to set one."
            )
    attempts = max(1, int(cfg.llm_retry_max_attempts))
    boto_config = BotoConfig(
        retries={"max_attempts": attempts, "mode": "adaptive"},
        read_timeout=float(cfg.timeout_s),
        connect_timeout=float(cfg.timeout_s),
    )
    client = session.client("bedrock-runtime", config=boto_config)
    return BedrockProvider(bedrock_client=client)
