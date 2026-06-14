"""rebar.llm — a framework for running tool-using LLM agents that emit structured
findings, exposed (like the rest of rebar) over library, CLI, and MCP.

Design in one paragraph: an **operation** (e.g. :func:`review_ticket`) assembles
deterministic context from rebar's own reads, resolves a **reviewer** prompt from
Langfuse prompt management (with a packaged fallback), and dispatches to a
pluggable **Runner**. The default runner runs an in-process LangChain/LangGraph
agent — the one runtime native to both Langflow and Langfuse — with read-only,
line-numbered repository file tools plus MCP servers, and returns findings
constrained to the canonical ``review_result`` JSON Schema. A Langflow runner
(REST) and other harnesses slot in behind the same protocol. Langfuse provides
tracing + the prompt library.

**Optionality is a hard rule:** importing this package pulls **no** heavy
dependency — langchain/langgraph/langfuse/anthropic are imported lazily by the
runner only when an operation runs. ``import rebar`` and ``import rebar.llm`` stay
stdlib-only; running needs the ``nava-rebar[agents]`` extra + ``ANTHROPIC_API_KEY``.

    import rebar.llm
    result = rebar.llm.review_ticket("abc123", "ticket-quality")   # -> review_result dict
    result["findings"]  # [{severity, dimension, detail, citations[...]}, ...]
"""

from __future__ import annotations

from rebar.llm.config import (
    LLMConfig,
    agents_extra_installed,
    available_backends,
)
from rebar.llm.errors import (
    LLMConfigError,
    LLMError,
    LLMRunnerError,
    StructuredOutputError,
)
from rebar.llm.aggregate import aggregate_findings
from rebar.llm.code_review import review_code, select_code_reviewers
from rebar.llm.findings import build_result, normalize_finding, validate_result
from rebar.llm.operations import review_ticket, select_reviewers
from rebar.llm.prompts import Reviewer, get_reviewer, load_catalog
from rebar.llm.spec_scan import scan_epics_for_spec
from rebar.llm.runner import (
    DeepAgentsRunner,
    FakeRunner,
    RunRequest,
    Runner,
    get_runner,
)

__all__ = [
    # operations
    "review_ticket",
    "review_code",
    "scan_epics_for_spec",
    "select_reviewers",
    "select_code_reviewers",
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
    "DeepAgentsRunner",
    "get_runner",
    # reviewer registry
    "Reviewer",
    "get_reviewer",
    "load_catalog",
    # exceptions
    "LLMError",
    "LLMConfigError",
    "LLMRunnerError",
    "StructuredOutputError",
]
