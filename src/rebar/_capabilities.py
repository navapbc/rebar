"""The descriptive semantic capability registry (ADR 0100 §7 / RP-05 S4).

A top-level command cannot truthfully declare one required packaging extra:
optionality varies by nested mode, analyzer, renderer, or selected provider. This
module maps each *semantic capability* — a unit of functionality gated behind an
optional extra — to (a) its packaging ``extra`` and (b) a typed *missing posture*
describing how the domain reacts when the extra is absent (``error`` hard-fails,
``unavailable`` reports a metric as absent, ``abstain`` withholds a grounding
verdict, ``fallback`` degrades to a plainer rendering).

This registry is **error-shaping infrastructure only**. It is stdlib-only (it must
import in the leanest no-extras install), it NEVER imports an optional/probe
package — availability is detected with :func:`importlib.util.find_spec`, which
resolves a module without executing it — and it NEVER manufactures a domain
result: the ``unavailable``/``abstain``/``fallback`` payloads are owned by the
domain components, not here. The only cross-module dependency is reusing
:class:`rebar._optional.OptionalDependencyError` (itself stdlib-only), so no import
cycle is introduced.
"""

from __future__ import annotations

import enum
import importlib.util
from dataclasses import dataclass

from rebar._optional import OptionalDependencyError


class Posture(str, enum.Enum):
    """How the domain reacts to a capability whose extra is not installed."""

    ERROR = "error"
    UNAVAILABLE = "unavailable"
    ABSTAIN = "abstain"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class Capability:
    """One semantic capability: a unit of functionality gated behind an extra."""

    key: str
    extra: str
    posture: Posture
    probe: str
    install_hint: str
    summary: str = ""


@dataclass(frozen=True)
class Finding:
    """One structural validation problem discovered by :func:`validate`."""

    code: str
    key: str
    detail: str


# The runtime packaging extras — mirrors pyproject.toml's
# [project.optional-dependencies] MINUS ``dev`` (development metadata, never a
# runtime capability). Held as an explicit literal so import stays stdlib-only and
# never reads pyproject.toml.
DECLARED_EXTRAS: frozenset[str] = frozenset(
    {
        "agents",
        "ui",
        "bedrock",
        "jira-datacenter",
        "s3",
        "metrics",
        "pricing",
        "grounding",
        "grounding-t2",
        "grounding-terraform",
        "adf",
        "wiki",
        "tracing",
        "mcp",
        "reviewbot",
        "evals",
    }
)


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        key="agent_runtime",
        extra="agents",
        posture=Posture.ERROR,
        probe="pydantic_ai",
        install_hint="pip install 'nava-rebar[agents]'",
        summary="the provider-agnostic LLM agent runtime",
    ),
    Capability(
        key="audit_ui",
        extra="ui",
        posture=Posture.ERROR,
        probe="jinja2",
        install_hint="pip install 'nava-rebar[ui]'",
        summary="the HTML audit-trail renderer",
    ),
    Capability(
        key="bedrock_provider",
        extra="bedrock",
        posture=Posture.ERROR,
        probe="boto3",
        install_hint="pip install 'pydantic-ai-slim[bedrock]'",
        summary="the AWS Bedrock model provider",
    ),
    Capability(
        key="jira_datacenter",
        extra="jira-datacenter",
        posture=Posture.ERROR,
        probe="jira",
        install_hint="pip install 'nava-rebar[jira-datacenter]'",
        summary="the Jira Data Center REST client",
    ),
    Capability(
        key="s3_remote",
        extra="s3",
        posture=Posture.ERROR,
        probe="git_remote_s3",
        install_hint="pip install 'nava-rebar[s3]'",
        summary="the s3:// ticket-store remote helper",
    ),
    Capability(
        key="metrics_lizard",
        extra="metrics",
        posture=Posture.UNAVAILABLE,
        probe="lizard",
        install_hint="pip install 'nava-rebar[metrics]'",
        summary="the lizard code-health analyzer",
    ),
    Capability(
        key="pricing",
        extra="pricing",
        posture=Posture.UNAVAILABLE,
        probe="genai_prices",
        install_hint="pip install 'nava-rebar[pricing]'",
        summary="the model-pricing/token-cost estimator",
    ),
    Capability(
        key="grounding_structural",
        extra="grounding",
        posture=Posture.ABSTAIN,
        probe="tree_sitter_language_pack",
        install_hint="pip install 'nava-rebar[grounding]'",
        summary="the tree-sitter structural grounding oracle",
    ),
    Capability(
        key="grounding_t2",
        extra="grounding-t2",
        posture=Posture.ABSTAIN,
        probe="pyright",
        install_hint="pip install 'nava-rebar[grounding-t2]'",
        summary="the tier-2 type-aware grounding oracle",
    ),
    Capability(
        key="grounding_terraform",
        extra="grounding-terraform",
        posture=Posture.ABSTAIN,
        probe="hcl2",
        install_hint="pip install 'nava-rebar[grounding-terraform]'",
        summary="the optional Terraform structural grounding tools (python-hcl2)",
    ),
    Capability(
        key="cloud_adf",
        extra="adf",
        posture=Posture.FALLBACK,
        probe="marklas",
        install_hint="pip install 'nava-rebar[adf]'",
        summary="the Jira Cloud ADF rich-text renderer",
    ),
    Capability(
        key="datacenter_wiki",
        extra="wiki",
        posture=Posture.FALLBACK,
        probe="pypandoc",
        install_hint="pip install 'nava-rebar[wiki]'",
        summary="the Jira Data Center wiki-markup renderer",
    ),
    Capability(
        key="trace_export",
        extra="tracing",
        posture=Posture.FALLBACK,
        probe="opentelemetry",
        install_hint="pip install 'nava-rebar[tracing]'",
        summary="the OTLP trace sink (write-only)",
    ),
    Capability(
        key="mcp_server",
        extra="mcp",
        posture=Posture.ERROR,
        probe="mcp",
        install_hint="pip install 'nava-rebar[mcp]'",
        summary="the rebar-mcp server surface",
    ),
    Capability(
        key="review_bot",
        extra="reviewbot",
        posture=Posture.ERROR,
        probe="fastapi",
        install_hint="pip install 'nava-rebar[reviewbot]'",
        summary="the Gerrit review-bot webhook service",
    ),
)


_BY_KEY: dict[str, Capability] = {cap.key: cap for cap in CAPABILITIES}

CAPABILITY_KEYS: frozenset[str] = frozenset(_BY_KEY)


def get(key: str) -> Capability:
    """Return the capability record for ``key``; raise ``KeyError`` if unknown."""

    return _BY_KEY[key]


def is_capability(name: str) -> bool:
    """Return whether ``name`` is a registered capability key."""

    return name in CAPABILITY_KEYS


def posture_for(key: str) -> Posture:
    """Return the missing-posture of the capability ``key``."""

    return get(key).posture


def extra_for(key: str) -> str:
    """Return the packaging extra of the capability ``key``."""

    return get(key).extra


def install_hint(key: str) -> str:
    """Return the ``pip install`` hint of the capability ``key``."""

    return get(key).install_hint


def is_available(key: str) -> bool:
    """Return whether the capability's probe module is importable.

    Detection uses :func:`importlib.util.find_spec` only, which resolves a module
    spec without executing the module — the probe package is never imported. An
    unknown key raises ``KeyError`` (consistent with :func:`get`).
    """

    probe = get(key).probe
    try:
        return importlib.util.find_spec(probe) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def missing_error(key: str) -> OptionalDependencyError:
    """Return (do not raise) an ``OptionalDependencyError`` for an error capability.

    The message names the extra and embeds the capability's ``install_hint``
    verbatim. Only valid for an ``error``-posture capability; ``unavailable`` /
    ``abstain`` / ``fallback`` postures are domain-owned and raise ``ValueError``.
    """

    cap = get(key)
    if cap.posture is not Posture.ERROR:
        raise ValueError(
            f"capability {key!r} has {cap.posture.value!r} posture; the domain owns "
            "non-error postures, so no OptionalDependencyError is produced here"
        )
    return OptionalDependencyError(
        f"the {cap.extra!r} capability {key!r} is required but the {cap.extra!r} extra "
        f"is not installed; install it with: {cap.install_hint}"
    )


def require_capability(key: str) -> None:
    """Guard an ``error``-posture capability: raise if its extra is missing.

    Raises ``ValueError`` for a non-``error`` posture (domain-owned), raises the
    :func:`missing_error` for an ``error`` capability whose probe is absent, and
    returns ``None`` when the capability is available.
    """

    cap = get(key)
    if cap.posture is not Posture.ERROR:
        raise ValueError(
            f"capability {key!r} has {cap.posture.value!r} posture; require_capability "
            "guards only error-posture capabilities"
        )
    if not is_available(key):
        raise missing_error(key)
    return None


def validate(
    caps: tuple[Capability, ...] = CAPABILITIES,
    *,
    declared_extras: frozenset[str] = DECLARED_EXTRAS,
) -> tuple[Finding, ...]:
    """Structurally validate ``caps`` (pure — no ``find_spec``, no imports).

    Returns deterministically ordered findings for undeclared extras, invalid
    postures, duplicate keys, and empty probes. The shipped ``CAPABILITIES`` with
    the default ``DECLARED_EXTRAS`` validates to an empty tuple.
    """

    findings: list[Finding] = []
    seen: set[str] = set()
    for cap in caps:
        if cap.extra not in declared_extras:
            findings.append(
                Finding("undeclared_extra", cap.key, f"extra {cap.extra!r} not in declared extras")
            )
        if not isinstance(cap.posture, Posture):
            findings.append(
                Finding("invalid_posture", cap.key, f"posture {cap.posture!r} is not a Posture")
            )
        if cap.key in seen:
            findings.append(
                Finding("duplicate_key", cap.key, f"key {cap.key!r} appears more than once")
            )
        seen.add(cap.key)
        if not cap.probe:
            findings.append(Finding("empty_probe", cap.key, "probe module name is empty"))
    return tuple(findings)
