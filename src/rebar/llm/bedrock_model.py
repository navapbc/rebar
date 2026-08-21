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
    """rebar's OWN Bedrock region chain: ``(region, source)``, or ``(None, None)``.

    Order, with the EXACT ``source`` label returned for each arm (bug 8274; the label is
    recorded verbatim as ``region_source`` in the verdict's provider provenance):

    1. ``bedrock_region_name`` — the configured knob (``rebar -c llm.bedrock_region_name``,
       ``REBAR_LLM_BEDROCK_REGION`` env, or ``[tool.rebar.llm].bedrock_region_name``) →
       ``configured_source``, the knob's TRUE origin (``"cli"`` /
       ``"REBAR_LLM_BEDROCK_REGION"`` / ``"repo-config"``) as resolved by the SAME
       ``LLMConfig.from_env`` pass that produced the value (``bedrock_region_source``,
       cda8) — never re-derived here, so record and resolution cannot diverge. A caller
       with no origin to thread (a hand-built config) falls back to the historical
       ``"REBAR_LLM_BEDROCK_REGION"`` label. ``configured_source`` labels ONLY this arm;
       it never reorders the chain or relabels the env arms below.
    2. ``AWS_DEFAULT_REGION`` env → ``"AWS_DEFAULT_REGION"``.
    3. ``AWS_REGION`` env → ``"AWS_REGION"``.
    4. Nothing set → ``(None, None)``: boto3's own resolution (the active profile) applies.

    ``AWS_REGION`` is in REBAR's chain because botocore's is measured NOT to consult it
    (botocore/boto3 1.43.62: ``AWS_DEFAULT_REGION`` unset + ``AWS_REGION`` set →
    ``boto3.session.Session().region_name`` is ``None``; the oracle test
    ``test_botocore_still_ignores_aws_region_but_rebar_now_closes_the_trap`` re-measures it
    every run) — yet ``AWS_REGION`` is the standard variable modern AWS tooling documents and
    operators actually export. Resolving it here and passing the value EXPLICITLY as
    ``region_name=`` makes botocore's quirk irrelevant. Empty-string values are treated as
    unset (botocore parity), and rebar still never invents a default region.

    Pure and stdlib-only (config value + ``os.environ``; no boto3), so the provenance seam
    (``capabilities.provenance_for``) can call it without pulling boto3 into a signed-record
    path, and both call sites deterministically agree."""
    if bedrock_region_name:
        return bedrock_region_name, configured_source or "REBAR_LLM_BEDROCK_REGION"
    for var in ("AWS_DEFAULT_REGION", "AWS_REGION"):
        value = os.environ.get(var)
        if value:
            return value, var
    return None, None


def build_bedrock_provider(cfg: LLMConfig, *, session=None):
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

    The region is resolved by rebar's OWN chain — :func:`resolve_bedrock_region`:
    ``cfg.bedrock_region_name`` (``REBAR_LLM_BEDROCK_REGION``) → ``AWS_DEFAULT_REGION`` →
    ``AWS_REGION`` → boto3's own resolution (the active profile's config) — and passed to the
    boto3 session EXPLICITLY as ``region_name=``, so botocore's measured refusal to consult
    ``AWS_REGION`` (see the resolver's docstring, bug 8274) cannot strand an operator whose
    shell exports only that standard variable. Never a rebar-invented default region, since a
    wrong region is a silent-until-call misconfiguration, not a value rebar can safely guess.
    The resolved source label is what ``capabilities.provenance_for`` records as
    ``region_source`` in the verdict's provider provenance.

    Authentication is deliberately NOT parameterized here beyond region: no
    ``aws_access_key_id``/``aws_secret_access_key`` argument is ever threaded through, so
    the ambient credential chain (instance role / ``AWS_PROFILE`` / boto3 default chain) is
    always what authenticates — never a rebar-managed key (see module docstring).
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
            # MEASURED in compose-review-bot-1 (ticket a574): the container has no region env
            # vars and no profile, so boto3 resolves NOTHING and client construction raised a
            # bare `NoRegionError` from deep inside botocore — a stack trace that names no rebar
            # setting. Pre-check the combined resolution (rebar's chain above, then boto3's own —
            # rather than inventing a default region, which would be a silent-until-call
            # misconfiguration) and fail with a typed, actionable error instead.
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
