"""rebar.llm — a framework for running tool-using LLM agents that emit structured
findings, exposed (like the rest of rebar) over library, CLI, and MCP.

Design in one paragraph: an **operation** (e.g. :func:`review_ticket`) assembles
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
    result = rebar.llm.review_ticket("abc123", "ticket-quality")   # -> review_result dict
    result["findings"]  # [{severity, dimension, detail, citations[...]}, ...]
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
    LLMConfigError,
    LLMError,
    LLMRunnerError,
    LLMUnavailableError,
    StructuredOutputError,
)
from rebar.llm.findings import build_result, normalize_finding, validate_result
from rebar.llm.operations import review_ticket, select_reviewers
from rebar.llm.plan_review import claim_gate_check, resign_plan_review, review_plan
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
    # operations
    "review_ticket",
    "review_code",
    "scan_epics_for_spec",
    "verify_completion",
    "enrich",
    "review_plan",
    "resign_plan_review",
    "claim_gate_check",
    "select_reviewers",
    "aggregate_findings",
    # config / diagnostics
    "LLMConfig",
    "available_backends",
    "agents_extra_installed",
    # findings contract helpers
    "build_result",
    "normalize_finding",
    "validate_result",
    # runner seam (custom ops / tests)
    "Runner",
    "RunRequest",
    "FakeRunner",
    "get_runner",
    # prompt / reviewer registry
    "Prompt",
    "Reviewer",
    "get_prompt",
    "load_catalog",
    # prompt-library authoring (write + structured enumerate; story B-DM)
    "enumerate_library",
    "enumerate_criteria",
    "create_prompt",
    "update_prompt",
    "LibraryWriteError",
    "InvalidPromptIdError",
    "PromptExistsError",
    # exceptions
    "LLMError",
    "LLMConfigError",
    "LLMUnavailableError",
    "LLMRunnerError",
    "StructuredOutputError",
]
