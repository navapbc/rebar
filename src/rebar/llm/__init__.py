"""rebar.llm — a framework for running tool-using LLM agents that emit structured
findings, exposed (like the rest of rebar) over library, CLI, and MCP.

The primary review surface is the **plan-review gate** (:func:`review_plan` /
``rebar review-plan``): a deterministic floor plus a multi-pass (find → verify →
decide → coach) review of a ticket's whole plan, which signs a claim-gating
attestation. :func:`review_ticket` — a single-pass review of a ticket or
ticket-graph — is now **deprecated** (story 316a; its CLI verb ``rebar review`` is
a forwarding shim over ``review-plan``); it still works but signals a registered
deprecation on every call.

Design in one paragraph: an **operation** (e.g. :func:`review_plan`) assembles
deterministic context from rebar's own reads, resolves a **prompt** git-canonically
(a packaged prompt or a ``.rebar/prompts/<id>.md`` override — Langfuse is never
consulted for prompt text), and dispatches to a
pluggable **Runner**. The default runner runs an in-process, provider-agnostic
Pydantic AI agent — the provider chosen by the model string — with read-only,
line-numbered repository file tools plus MCP servers, and returns findings
constrained to the canonical ``review_result`` JSON Schema. Other runners slot in
behind the same protocol. Langfuse provides tracing (and is an optional read-replica
of prompts, never the source of truth).

**Optionality is a hard rule:** importing this package pulls **no** heavy
dependency — the agent runtime (pydantic-ai) / langfuse / anthropic are imported
lazily by the runner only when an operation runs. ``import rebar`` and ``import
rebar.llm`` stay stdlib-only; running needs the ``nava-rebar[agents]`` extra +
``ANTHROPIC_API_KEY``.

    import rebar.llm
    result = rebar.llm.review_plan("abc123")   # -> plan_review_verdict dict
    result["blocking"]  # [{criteria[...], finding, ...}, ...]
"""

from __future__ import annotations

from rebar.llm.aggregate import aggregate_findings
from rebar.llm.code_review import review_code
from rebar.llm.completion import verify_completion
from rebar.llm.config import (
    LLMConfig,
    agents_extra_installed,
    available_backends,
)
from rebar.llm.enrich import enrich
from rebar.llm.errors import (
    CompletionRecoveryError,
    LLMConfigError,
    LLMError,
    LLMRunnerError,
    LLMUnavailableError,
    StructuredOutputError,
)
from rebar.llm.findings import build_result, normalize_finding, validate_result
from rebar.llm.operations import review_ticket, select_reviewers
from rebar.llm.plan_review import (
    claim_gate_check,
    plan_review_status,
    resign_plan_review,
    review_plan,
)
from rebar.llm.prompting.prompt_library import (
    InvalidPromptIdError,
    LibraryWriteError,
    PromptExistsError,
    create_prompt,
    enumerate_criteria,
    enumerate_library,
    update_prompt,
)
from rebar.llm.prompting.prompts import Prompt, Reviewer, get_prompt, load_catalog
from rebar.llm.runner import (
    FakeRunner,
    Runner,
    RunRequest,
    get_runner,
)
from rebar.llm.spec_scan import scan_epics_for_spec

__all__ = [
    "CompletionRecoveryError",
    "FakeRunner",
    "InvalidPromptIdError",
    # config / diagnostics
    "LLMConfig",
    "LLMConfigError",
    # exceptions
    "LLMError",
    "LLMRunnerError",
    "LLMUnavailableError",
    "LibraryWriteError",
    # prompt / reviewer registry
    "Prompt",
    "PromptExistsError",
    "Reviewer",
    "RunRequest",
    # runner seam (custom ops / tests)
    "Runner",
    "StructuredOutputError",
    "agents_extra_installed",
    "aggregate_findings",
    "available_backends",
    # findings contract helpers
    "build_result",
    "claim_gate_check",
    "create_prompt",
    "enrich",
    "enumerate_criteria",
    # prompt-library authoring (write + structured enumerate; story B-DM)
    "enumerate_library",
    "get_prompt",
    "get_runner",
    "load_catalog",
    "normalize_finding",
    "plan_review_status",
    "resign_plan_review",
    "review_code",
    "review_plan",
    # operations
    "review_ticket",
    "scan_epics_for_spec",
    "select_reviewers",
    "update_prompt",
    "validate_result",
    "verify_completion",
]
