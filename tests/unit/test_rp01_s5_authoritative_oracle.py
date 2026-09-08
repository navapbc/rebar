"""Hold out the RP-01 S5 invariants for the authoritative structured-output path.

An AST guard permits private structured-operation names only in their owner and runner.
Runtime probes keep retries within one ``Agent.run_sync`` except for native-to-prompted fallback.
A zero structured retry limit remains a single-shot fail-safe. Tests use an offline
``FunctionModel``.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from rebar.llm import structured_run as structured_run_mod

pytest.importorskip("pydantic_ai")

from _tree_scan import parsed_python_files

# Reuse the S2 oracle harness (imported by basename; tests/unit is on sys.path in this suite).
from test_rp01_s2_bounded_op_oracle import (
    _VALID,
    _native_cfg,
    _offline,  # noqa: F401 — autouse fixture, re-registered by import
    _req,
    _scripted_model,
)

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMRunnerError
from rebar.llm.runner import PydanticAIRunner
from rebar.llm.structured_run import output_retry_allowance

pytestmark = pytest.mark.unit

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
# These private operations may be reached only through the runner facade.
_INTERNAL_IMPL = frozenset({"_pai_structured", "_run_native_output", "_run_prompted_output"})
# Only the owner and runner dispatch may name the private operations.
_DISPATCH_MODULES = frozenset({"structured_run.py", "runner.py"})


# ─────────────────────────────── AC-1: single-sourced dispatch ──────────────────────────────


def _parsed_sources():
    roots = [_REPO_ROOT / "src" / "rebar", _REPO_ROOT / "scripts"]
    return [m for root in roots if root.exists() for m in parsed_python_files(root)]


def _code_names(tree: ast.AST) -> set[str]:
    """Return executable identifiers while excluding names in strings and comments."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def test_structured_dispatch_is_single_sourced_no_consumer_bypasses_the_facade():
    """Only the defining module and runner may reference the private implementation."""
    offenders: dict[str, set[str]] = {}
    for module in _parsed_sources():
        if module.path.name in _DISPATCH_MODULES:
            continue
        hit = _INTERNAL_IMPL & _code_names(module.tree)
        if hit:
            offenders[str(module.path.relative_to(_REPO_ROOT))] = hit
    assert not offenders, (
        "structured-output dispatch must stay single-sourced through the runner facade; "
        f"these modules reach into the private one-operation stack: {offenders}"
    )


def test_runner_funnels_structured_through_one_operation_but_text_bypasses_it(monkeypatch):
    """The runner enters one structured operation and bypasses it for text mode."""

    real = structured_run_mod._pai_structured
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(structured_run_mod, "_pai_structured", counting)
    cfg = LLMConfig(repo_path=".")

    smodel, _ = _scripted_model([{"text": _VALID}])
    sresult = PydanticAIRunner(cfg, model_override=smodel).run(_req(cfg))
    assert sresult["verdict"] == "PASS"
    assert calls["n"] == 1, "a structured run funnels through the ONE operation exactly once"

    tmodel, _ = _scripted_model([{"text": "plain answer"}])
    treq = dataclasses.replace(_req(cfg), mode="text")
    tresult = PydanticAIRunner(cfg, model_override=tmodel).run(treq)
    assert "text" in tresult
    assert calls["n"] == 1, "a text run does NOT enter the structured one-operation path"


# ───────────────────────── AC-1: the census is mode-RESOLVING (not grep) ────────────────────


def _mode_is_structured(node: ast.AST, assignments: dict[str, ast.AST]) -> bool:
    """Resolve literal, conditional, and bound expressions that select structured mode."""
    if isinstance(node, ast.Constant):
        return node.value == "structured"
    if isinstance(node, ast.IfExp):
        return _mode_is_structured(node.body, assignments) or _mode_is_structured(
            node.orelse, assignments
        )
    if isinstance(node, ast.Name) and node.id in assignments:
        return _mode_is_structured(assignments[node.id], assignments)
    return False


def _own_nodes(scope: ast.AST):
    """Yield nodes in one scope while excluding nested function bodies."""
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield child
        yield from _own_nodes(child)


def _is_for_structured(func: ast.expr) -> bool:
    """True for the ``RunRequest.for_structured(...)`` builder, whose mode is structural."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "for_structured"
        and isinstance(func.value, ast.Name)
        and func.value.id == "RunRequest"
    )


def _scope_constructs_structured(scope: ast.AST, inherited: dict[str, ast.AST]) -> bool:
    """Detect structured requests recursively with bindings isolated by function scope."""
    assignments = dict(inherited)
    for node in _own_nodes(scope):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name):
                assignments[tgt.id] = node.value
    for node in _own_nodes(scope):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if _is_for_structured(func):
                # The canonical builder fixes structured mode without a mode keyword.
                return True
            if name == "for_structured":
                # Reaching an equivalent builder proves structured mode without a keyword.
                return True
            if name == "RunRequest":
                for kw in node.keywords:
                    if kw.arg == "mode" and _mode_is_structured(kw.value, assignments):
                        return True
    return any(
        _scope_constructs_structured(node, assignments)
        for node in ast.iter_child_nodes(scope)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _structured_consumer_files() -> set[str]:
    """AST census: every file constructing a ``RunRequest(...)`` whose EFFECTIVE ``mode``
    resolves to ``"structured"`` — multi-line constructions and computed modes included."""
    found: set[str] = set()
    for module in _parsed_sources():
        if _scope_constructs_structured(module.tree, {}):
            found.add(module.path.name)
    return found


def test_mode_resolving_census_sees_computed_and_multiline_consumers():
    """The census finds computed, multiline, and out-of-tree structured consumers."""
    files = _structured_consumer_files()
    assert "fidelity_spot_eval.py" in files, (
        "the census missed a COMPUTED-mode consumer a grep cannot see"
    )
    assert "jira_dc_capability_map.py" in files, (
        "the census missed the out-of-tree scripts/ consumer"
    )
    # A broad count guards against a narrowed or zero-match census.
    assert len(files) >= 10, f"census under-counted structured consumers: {sorted(files)}"


# ───────────────────────── AC-3: in-Agent bounded retry, never a loop ───────────────────────


@pytest.fixture
def _run_sync_counter(monkeypatch):
    """Count outer runs so in-run retries cannot masquerade as fresh operations."""
    from pydantic_ai import Agent

    real = Agent.run_sync
    box = {"n": 0}

    def counting(self, *a, **k):
        box["n"] += 1
        return real(self, *a, **k)

    monkeypatch.setattr(Agent, "run_sync", counting)
    return box


def test_wellformed_first_response_is_exactly_one_outer_run(_run_sync_counter):
    """AC-3: a well-formed first response performs EXACTLY ONE outer ``run_sync`` and ONE model
    request — no speculative extra attempt."""
    model, state = _scripted_model([{"text": _VALID}])
    result = PydanticAIRunner(LLMConfig(repo_path="."), model_override=model).run(
        _req(LLMConfig(repo_path="."))
    )
    assert result["verdict"] == "PASS"
    assert _run_sync_counter["n"] == 1, "the happy path is a single outer run"
    assert state["calls"] == 1, "and a single model request"


def test_bounded_output_retry_adds_a_request_but_not_a_second_run_sync(_run_sync_counter):
    """A retry adds a model request without adding a second outer run."""
    model, state = _scripted_model([{"text": "boom", "finish_reason": "error"}, {"text": _VALID}])
    result = PydanticAIRunner(LLMConfig(repo_path="."), model_override=model).run(
        _req(LLMConfig(repo_path="."))
    )
    assert result["verdict"] == "PASS"
    assert state["calls"] == 2, "the bounded retry issued a second model request"
    assert _run_sync_counter["n"] == 1, (
        "but the retry stayed IN the one Agent run — no fresh-Agent loop"
    )


def test_895c_downgrade_is_the_only_sanctioned_second_outer_run(monkeypatch, _run_sync_counter):
    """Native grammar fallback makes one native attempt and one prompted outer run."""
    from botocore.exceptions import ClientError

    from rebar.llm import structured_run

    native_attempts = {"n": 0}

    def _reject_native(*_a, **_k):
        native_attempts["n"] += 1
        raise ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Grammar compilation timed out."}},
            "Converse",
        )

    monkeypatch.setattr(structured_run, "_run_native_output", _reject_native)
    prompted, state = _scripted_model([{"text": _VALID}])
    result = PydanticAIRunner(_native_cfg(), model_override=prompted).run(_req(_native_cfg()))

    assert result["verdict"] == "PASS", "the prompted fallback produced the verdict"
    assert native_attempts["n"] == 1, "the native attempt was made exactly once"
    assert _run_sync_counter["n"] == 1, "the prompted attempt is one run — no loop, never a third"
    assert state["calls"] == 1, "exactly the prompted turn billed a model request"


# ───────────────────────── AC-4: structured_retry_limit=0 fail-safe ─────────────────────────


def test_structured_retry_limit_zero_yields_zero_output_retries():
    """A zero structured retry limit produces no output-retry allowance."""
    cfg = LLMConfig(repo_path=".")
    req0 = dataclasses.replace(_req(cfg), structured_retry_limit=0)
    assert output_retry_allowance(req0) == 0


def test_structured_retry_limit_zero_is_single_shot_no_bounded_retry():
    """A zero structured retry limit aborts a transient error after one request."""
    cfg = LLMConfig(repo_path=".")
    model, state = _scripted_model([{"text": "boom", "finish_reason": "error"}, {"text": _VALID}])
    with pytest.raises(LLMRunnerError):
        PydanticAIRunner(cfg, model_override=model).run(
            dataclasses.replace(_req(cfg), structured_retry_limit=0)
        )
    assert state["calls"] == 1, "limit=0 is single-shot: the transient error is not retried"
