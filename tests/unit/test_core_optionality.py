"""WS-J3: CI-enforced optionality — the lean runtime stays lean.

Two guards, the in-suite half of the optionality CI (the wheel/per-extra jobs live
in .github/workflows/optionality.yml):

  * RUNTIME — importing the workflow engine's LEAN runtime (DSL parse/lint/migrate,
    executor, scripted steps, render, run orchestration) must not pull the heavy
    [agents]/[eval]/[tracing] stack into sys.modules, proving a scripted workflow
    runs with no optional dependency. Run in a clean subprocess.
  * STATIC — no module under src/rebar imports the heavy stack at MODULE scope;
    every such import must be lazy (inside a function), so `import rebar` and the
    lean runtime can never silently grow heavy.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import rebar

# The heavy stack gated behind extras. None may be imported by the lean runtime or
# at module scope anywhere in the core tree.
_HEAVY = (
    "langchain",
    "langgraph",
    "langchain_anthropic",
    "langchain_openai",
    "langchain_mcp_adapters",
    "langfuse",
    "anthropic",
    "deepagents",
    "inspect_ai",
    "opentelemetry",
    # [agents] extra — the provider-agnostic in-process runtime (story d6d1 cutover
    # dropped LangChain/LangGraph for pydantic-ai). `httpx` arrives transitively via
    # pydantic-ai; neither is a core dep, so both must stay lazy (call-boundary).
    "pydantic_ai",
    "httpx",
    # [grounding] extra — the in-process structural-parsing binding. The grounding
    # contract + harness are stdlib-only; tree-sitter must stay lazy (worker boundary).
    "tree_sitter",
    "tree_sitter_language_pack",
)

_SRC = Path(rebar.__file__).resolve().parent


def test_lean_workflow_runtime_pulls_no_heavy_stack() -> None:
    code = (
        "import sys;"
        "import rebar;"
        "import rebar.llm.workflow.executor;"
        "import rebar.llm.workflow.steps;"
        "import rebar.llm.workflow.runs;"
        "import rebar.llm.workflow.render;"
        "import rebar.llm.workflow.lint;"
        "import rebar.grounding;"  # grounding contract + harness must be import-clean
        f"heavy={_HEAVY!r};"
        "leaked=[m for m in heavy if m in sys.modules];"
        "print('LEAK:' + ','.join(leaked) if leaked else 'CLEAN')"
    )
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "CLEAN", (
        f"the lean workflow runtime leaked the heavy stack: {cp.stdout.strip()}"
    )


def _module_scope_imports(tree: ast.Module):
    """Yield Import/ImportFrom nodes at MODULE scope (recurse into module-level
    if/try/with, but NOT into function/class bodies — those are lazy by design)."""

    def walk(body):
        for node in body:
            if isinstance(node, ast.Import | ast.ImportFrom):
                yield node
            elif isinstance(node, ast.If | ast.Try | ast.With | ast.For | ast.While):
                yield from walk(node.body)
                yield from walk(getattr(node, "orelse", []))
                yield from walk(getattr(node, "finalbody", []))
                for h in getattr(node, "handlers", []):
                    yield from walk(h.body)
            elif isinstance(node, ast.Match):
                for case in node.cases:
                    yield from walk(case.body)

    yield from walk(tree.body)


def _is_heavy(name: str | None) -> bool:
    return bool(name) and any(name == h or name.startswith(h + ".") for h in _HEAVY)


def test_no_core_module_imports_heavy_stack_at_module_scope() -> None:
    offenders: list[str] = []
    for py in _SRC.rglob("*.py"):
        # _engine ships as reconciler subprocess data (stdlib-only); skip caches.
        if "_engine" in py.parts or "__pycache__" in py.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in _module_scope_imports(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            for n in names:
                if _is_heavy(n):
                    offenders.append(f"{py.relative_to(_SRC.parent)}: import {n}")
    assert not offenders, (
        "heavy [agents]/[eval]/[tracing] imports must be LAZY (inside a function), "
        "not at module scope:\n" + "\n".join(offenders)
    )
