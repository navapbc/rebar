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
    ``providers.py``. The boto3 client is constructed HERE (bug 61d8) so rebar's documented
    retry/timeout knobs reach the botocore transport instead of leaving it on stock
    defaults; nothing is registered on ``session._closeables`` (boto3 clients need no
    explicit close).

    **Retry/timeout semantics (bug 61d8 — how the documented keys map onto botocore):**

    - ``cfg.llm_retry_max_attempts`` (``REBAR_LLM_RETRY_MAX_ATTEMPTS``) →
      ``retries={"max_attempts": N, "mode": "adaptive"}``. botocore's ``max_attempts``
      counts **total attempts including the first** — the same counting as the Anthropic
      path's tenacity ``stop_after_attempt(N)`` — so the one configured integer means the
      same thing on both transports. ``N <= 1`` clamps to 1 (fail-fast, zero retries),
      mirroring the Anthropic envelope's ``max(1, ...)``. Mode ``"adaptive"`` adds
      client-side rate limiting on throttling, botocore's recommended posture for
      throttle-prone services like Bedrock.
    - ``cfg.timeout_s`` (``REBAR_LLM_TIMEOUT``) → BOTH ``read_timeout`` and
      ``connect_timeout``. The Anthropic path applies one ``httpx.Timeout(timeout_s)`` to
      every phase (connect/read/write/pool), so "parity" here means the same single bound
      on each socket phase, not a split budget.
    - ``cfg.llm_retry_max_wait_s`` has **no** botocore equivalent (botocore owns its own
      backoff) and is deliberately not mapped — honoring only the keys that translate is
      what keeps this wiring honest rather than inventing semantics.

    ``cfg.bedrock_region_name`` (``REBAR_LLM_BEDROCK_REGION``) is passed to the boto3
    session when set; otherwise the session falls back to boto3's own region resolution
    (``AWS_DEFAULT_REGION`` or the active profile's config) — never a rebar-invented
    default region, since a wrong region is a silent-until-call misconfiguration, not a
    value rebar can safely guess.

    ``AWS_REGION`` is NOT one of those sources: MEASURED on botocore/boto3 1.43.62, with
    ``AWS_DEFAULT_REGION`` genuinely unset and ``AWS_REGION=us-east-1``,
    ``boto3.session.Session().region_name`` is ``None``, so setting ``AWS_REGION`` alone
    leaves this path failing. ``AWS_DEFAULT_REGION`` is botocore's canonical region
    variable and does resolve — the same asymmetry the review-bot service in
    ``infra/compose/docker-compose.yml`` records, which is why it sets both
    ``REBAR_LLM_BEDROCK_REGION`` and ``AWS_DEFAULT_REGION``. Re-measure before trusting
    this on a materially newer botocore.

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
    import boto3
    from botocore.config import Config as BotoConfig

    region = cfg.bedrock_region_name or None
    session = boto3.session.Session(region_name=region)
    if not session.region_name:
        # MEASURED in compose-review-bot-1 (ticket a574): the container has no AWS_REGION, no
        # AWS_DEFAULT_REGION and no profile, so boto3 resolves NOTHING and client construction
        # raised a bare `NoRegionError` from deep inside botocore — a stack trace that names
        # no rebar setting. Every earlier Bedrock probe in this epic passed region_name
        # explicitly, which is exactly why ambient resolution was never exercised. Pre-check
        # boto3's OWN resolution (rather than inventing a default region, which would be a
        # silent-until-call misconfiguration) and fail with a typed, actionable error instead.
        raise LLMConfigError(
            "a bedrock model/provider is configured but no AWS region could be resolved. "
            "Set REBAR_LLM_BEDROCK_REGION (rebar's own knob, so the value is visible to "
            "rebar's config layer and to the verdict's provider provenance), or "
            "AWS_DEFAULT_REGION / the active profile's config. NOTE: AWS_REGION alone does "
            "NOT resolve a region — MEASURED on botocore/boto3 1.43.62, with "
            "AWS_DEFAULT_REGION unset and AWS_REGION set, boto3 resolves no region at all, "
            "so if you have only set AWS_REGION that is why you are seeing this. "
            "AWS_DEFAULT_REGION is botocore's canonical region variable. NOTE ALSO: "
            "instance-metadata (IMDS) reachability does NOT supply a region "
            "— credential discovery and region discovery are independent, so a working "
            "instance role does not remove the need to set one."
        )
    attempts = max(1, int(cfg.llm_retry_max_attempts))
    boto_config = BotoConfig(
        retries={"max_attempts": attempts, "mode": "adaptive"},
        read_timeout=float(cfg.timeout_s),
        connect_timeout=float(cfg.timeout_s),
    )
    client = session.client("bedrock-runtime", config=boto_config)
    return BedrockProvider(bedrock_client=client)
