"""WS-J3: CI-enforced optionality — the lean runtime stays lean.

Two guards, the in-suite half of the optionality CI (the wheel/per-extra jobs live
in .github/workflows/optionality.yml):

  * RUNTIME — importing the workflow engine's LEAN runtime (DSL parse/lint/migrate,
    executor, scripted steps, render, run orchestration) must not pull the heavy
    [agents]/[tracing] stack into sys.modules, proving a scripted workflow
    runs with no optional dependency. Run in a clean subprocess.
  * STATIC — no module under src/rebar imports the heavy stack at MODULE scope;
    every such import must be lazy (inside a function), so `import rebar` and the
    lean runtime can never silently grow heavy.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

from _tree_scan import parsed_python_files

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
    # [grounding-terraform] extra (REB-640) — python-hcl2 (and its lark parser) power
    # the optional Terraform structural grounding tools; both must stay lazy so
    # `import rebar` and non-Terraform reviews never pull the HCL parser.
    "hcl2",
    "lark",
)

_SRC = Path(rebar.__file__).resolve().parent


def test_lean_workflow_runtime_pulls_no_heavy_stack() -> None:
    code = textwrap.dedent(
        f"""
        import builtins
        import sys

        original_import = builtins.__import__

        def block_optional_pydantic(name, *args, **kwargs):
            if name == "pydantic" or name.startswith("pydantic."):
                raise ModuleNotFoundError("pydantic blocked by clean-runtime oracle")
            return original_import(name, *args, **kwargs)

        builtins.__import__ = block_optional_pydantic

        import rebar
        import rebar.llm.workflow.executor
        import rebar.llm.workflow.steps
        import rebar.llm.workflow.runs
        import rebar.llm.workflow.render
        import rebar.llm.workflow.lint
        import rebar.grounding
        from rebar._engine import engine_dir

        sys.path.insert(0, str(engine_dir()))
        import rebar_reconciler.runtime as reconciler_runtime

        first_auth = reconciler_runtime.StaticAuth("first-secret")
        second_auth = reconciler_runtime.StaticAuth("second-secret")
        assert "first-secret" not in repr(first_auth)
        assert first_auth == second_auth
        assert hash(first_auth) == hash(second_auth)
        assert first_auth.reveal() == "first-secret"

        heavy = {_HEAVY!r}
        leaked = [module for module in heavy if module in sys.modules]
        print("LEAK:" + ",".join(leaked) if leaked else "CLEAN")
        """
    )
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
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
    for module in parsed_python_files(_SRC):
        py = module.path
        # _engine ships as reconciler subprocess data (stdlib-only); skip caches.
        if "_engine" in py.parts or "__pycache__" in py.parts:
            continue
        tree = module.tree
        for node in _module_scope_imports(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            for n in names:
                if _is_heavy(n):
                    offenders.append(f"{py.relative_to(_SRC.parent)}: import {n}")
    assert not offenders, (
        "heavy [agents]/[tracing] imports must be LAZY (inside a function), "
        "not at module scope:\n" + "\n".join(offenders)
    )


def test_terraform_grounding_extra_is_optional_and_advertises_nothing_when_absent() -> None:
    """REB-640: with `hcl2` unimportable, `import rebar` and the grounding package
    still import, the Terraform tools report themselves UNAVAILABLE (advertise
    nothing), and a session's queries return a closed `no_tool/missing_extra`
    abstention — never a raise. Run in a clean subprocess with hcl2 blocked."""
    code = textwrap.dedent(
        """
        # Simulate the extra being ABSENT the way rebar actually detects it:
        # capability availability is resolved with importlib.util.find_spec
        # (never by importing the probe), so hide hcl2/lark from find_spec. Also
        # block real import as belt-and-braces, proving nothing eagerly imports it.
        import builtins, importlib.util
        _blocked = ("hcl2", "lark")
        _real_find_spec = importlib.util.find_spec
        def _hidden_find_spec(name, *args, **kwargs):
            if name in _blocked or name.split(".")[0] in _blocked:
                return None
            return _real_find_spec(name, *args, **kwargs)
        importlib.util.find_spec = _hidden_find_spec
        _real_import = builtins.__import__
        def _block_import(name, *args, **kwargs):
            if name in _blocked or name.split(".")[0] in _blocked:
                raise ModuleNotFoundError("blocked by optionality oracle: " + name)
            return _real_import(name, *args, **kwargs)
        builtins.__import__ = _block_import

        import rebar
        import rebar.grounding
        from rebar.grounding import terraform_tools as tft

        assert tft.available() is False, "must advertise NO terraform tools without the extra"
        session = tft.open_session(repo_root=".", selected=["infra/main.tf"])
        res = session.lookup_declaration("variable.x", module_path="infra")
        session.finalize()
        assert res.evidence["outcome"] == "abstain"
        assert res.evidence["reason"] == "no_tool"
        assert res.receipt["reason_detail"] == "missing_extra"
        print("OK")
        """
    )
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip().endswith("OK"), cp.stdout + cp.stderr
