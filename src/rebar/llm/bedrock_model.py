"""First-class AWS Bedrock provider construction (story S3, ticket 2932).

Leaf module (the ``anthropic_model.py`` convention): heavy libraries (boto3, via
``pydantic_ai.providers.bedrock``/``pydantic_ai.models.bedrock``) are imported **inside** the
functions that need them, never at module top, so ``import rebar.llm`` stays stdlib-only. This
module imports nothing from ``runner`` — ``providers.py`` (the ``ProviderSession`` registry)
holds the ONE call site, kept to a one-line delegation because that file sits at 289/300 LOC
against an ATTESTED cap from a closed story.

Authentication rides the AMBIENT AWS credential chain ONLY (instance role in prod,
``AWS_PROFILE``/env locally, or boto3's own default chain) — rebar manages no Bedrock API key,
mirroring how the direct-Anthropic path reads a rebar-managed key but a local-server OpenAI
endpoint does not: Bedrock is authenticated infrastructure, not a bearer-token API.

MEASURED facts (recorded on ticket 2932; treat as facts, not assumptions):

- Plain on-demand model ids (e.g. ``anthropic.claude-sonnet-4-6``) are NOT invokable at all —
  AWS returns ``ValidationException: Invocation of model ID ... with on-demand throughput
  isn't supported. Retry your request with the ID or ARN of an inference profile.`` So an
  inference-profile id (the ``us.``/``global.`` prefix form) is the only working form.
- ``us.anthropic.claude-sonnet-4-6`` is the documented default: MEASURED to cache
  (cache_write then cache_read on a repeated prefix) — see :mod:`rebar.llm.structured_run`'s
  ``warn_if_cache_ineffective`` for the models that do NOT.
"""

from __future__ import annotations

import logging

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError

logger = logging.getLogger(__name__)

# The documented default Bedrock model id — an INFERENCE-PROFILE id (the `us.` prefix), never
# a bare on-demand id, per the module docstring's MEASURED ValidationException. MEASURED to
# cache (ticket 2932).
DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-6"


def build_bedrock_provider(cfg: LLMConfig):
    """Build the ``BedrockProvider`` for ``ProviderSession``'s ``"bedrock"`` builder slot.

    Returns a bare ``Provider`` (not a Model) — exactly the contract
    ``ProviderSession.provider_factory`` needs, mirroring ``_build_openai`` in
    ``providers.py``. No custom client/transport is opened here (unlike the retrying
    Anthropic path), so nothing is registered on ``session._closeables``.

    ``cfg.bedrock_region_name`` (``REBAR_LLM_BEDROCK_REGION``) is passed through when set;
    otherwise ``BedrockProvider`` falls back to boto3's own region resolution
    (``AWS_REGION``/``AWS_DEFAULT_REGION``/the active profile's config) — never a
    rebar-invented default region, since a wrong region is a silent-until-call
    misconfiguration, not a value rebar can safely guess.

    Authentication is deliberately NOT parameterized here beyond region: no
    ``aws_access_key_id``/``aws_secret_access_key`` argument is ever threaded through, so
    the ambient credential chain (instance role / ``AWS_PROFILE`` / boto3 default chain) is
    always what authenticates — never a rebar-managed key (see module docstring).
    """
    try:
        from pydantic_ai.providers.bedrock import BedrockProvider
    except ImportError as exc:
        raise LLMConfigError(
            "a bedrock model/provider is configured but the optional bedrock provider "
            "package is not installed: pip install 'pydantic-ai-slim[bedrock]'"
        ) from exc
    region = cfg.bedrock_region_name or None
    if region is None:
        # MEASURED in compose-review-bot-1 (ticket a574): the container has no AWS_REGION, no
        # AWS_DEFAULT_REGION and no profile, so boto3 resolves NOTHING and BedrockProvider raised
        # a bare `NoRegionError` from deep inside client construction — a stack trace that names
        # no rebar setting. Every earlier Bedrock probe in this epic passed region_name explicitly,
        # which is exactly why ambient resolution was never exercised. Pre-check boto3's OWN
        # resolution (rather than inventing a default region, which would be a
        # silent-until-call misconfiguration) and fail with a typed, actionable error instead.
        import boto3

        if not boto3.session.Session().region_name:
            raise LLMConfigError(
                "a bedrock model/provider is configured but no AWS region could be resolved. "
                "Set REBAR_LLM_BEDROCK_REGION (rebar's own knob, so the value is visible to "
                "rebar's config layer and to the verdict's provider provenance), or a standard "
                "AWS region source (AWS_REGION / AWS_DEFAULT_REGION / the active profile's "
                "config). NOTE: instance-metadata (IMDS) reachability does NOT supply a region "
                "— credential discovery and region discovery are independent, so a working "
                "instance role does not remove the need to set one."
            )
    return BedrockProvider(region_name=region)
