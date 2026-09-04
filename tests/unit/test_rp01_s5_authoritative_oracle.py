"""RP-01 S5 — HELD-OUT oracle for the authoritative one-operation structured path
(ticket [rebar:ripening-liberal-tuatara], 10a3-7dd0-1b3d-47cc).

S1–S4 already made the bounded one-operation Pydantic path the SOLE structured-output
implementation reachable through ``PydanticAIRunner`` (S2 replaced the manual per-attempt
scheduler with ONE bounded ``Agent`` run). This oracle pins the CROSS-CUTTING invariants the
per-story oracles do not, so a later change that reintroduces a bespoke retry loop, bypasses
the runner facade, or breaks the ``structured_retry_limit=0`` fail-safe fails HERE:

  * AC-1 — the structured dispatch is single-sourced: no consumer reaches the one-operation
    implementation except through the ``get_runner(...).run(...)`` facade (a mode-RESOLVING
    AST census, not a literal grep, so multi-line and computed-``mode`` consumers are seen).
  * AC-3 — output repair is the IN-Agent bounded retry, never a fresh-Agent loop: a bounded
    retry adds a model request WITHOUT a second ``run_sync``; the only sanctioned second
    ``run_sync`` is the bug-895c native→prompted downgrade.
  * AC-4 — ``structured_retry_limit=0`` is a single-shot fail-safe (zero output retries),
    preserving the overlap/judge and contracts batch abstain.

Every behavioral test drives the REAL ``PydanticAIRunner`` over an offline ``FunctionModel``
(ALLOW_MODEL_REQUESTS off), reusing the S2 oracle's scripted-model harness.
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
# The one-operation implementation. A consumer that names any of these is reaching PAST the
# runner facade into the private structured stack — exactly the bypass AC-1 forbids.
_INTERNAL_IMPL = frozenset({"_pai_structured", "_run_native_output", "_run_prompted_output"})
# The ONLY modules allowed to name the internal implementation: the module that DEFINES it and
# the single runner dispatch that calls it.
_DISPATCH_MODULES = frozenset({"structured_run.py", "runner.py"})


# ─────────────────────────────── AC-1: single-sourced dispatch ──────────────────────────────


def _parsed_sources():
    roots = [_REPO_ROOT / "src" / "rebar", _REPO_ROOT / "scripts"]
    return [m for root in roots if root.exists() for m in parsed_python_files(root)]


def _code_names(tree: ast.AST) -> set[str]:
    """Identifiers used in CODE (``ast.Name`` ids + ``ast.Attribute`` attrs) — never names that
    appear only inside a string or comment. This is why the census is AST-based: a docstring
    mentioning ``_pai_structured`` (pai_retry.py, agent_call.py) is not a call site."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def test_structured_dispatch_is_single_sourced_no_consumer_bypasses_the_facade():
    """AC-1: the one-operation implementation is referenced in CODE only by the module that
    defines it and the single runner dispatch. Because every structured consumer obtains its
    runner through ``get_runner(...)`` and the runner is the sole caller of the implementation,
    this single-source property is what guarantees ALL consumers use the one-operation path —
    without editing each consumer. A new consumer that called the private stack directly (or a
    resurrected bespoke scheduler in a third module) would appear here as an offender."""
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
    """AC-1 (the RUNTIME single-dispatch contract, complementing the source census above): a
    ``mode="structured"`` request driven through the real ``PydanticAIRunner.run(...)`` facade
    invokes the one-operation implementation EXACTLY ONCE, while a ``mode="text"`` request does
    NOT touch it at all. This is behaviour-anchored — it observes the runtime routing through
    the public facade, not source text — so it survives a refactor of the implementation's body
    (a rename of the dispatch point is the one thing it pins, which is exactly the invariant:
    there is a single structured dispatch point and the facade routes to it)."""

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
    """Resolve whether an expression given for ``mode=`` evaluates to ``"structured"``.

    Handles the three shapes a literal grep for ``mode="structured"`` misses:
      * a bare constant ``"structured"``;
      * a conditional ``"structured" if <cond> else "findings"`` (fidelity_spot_eval.py);
      * a name bound to either of the above earlier in the same module.
    """
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
    """Nodes in ``scope`` not inside a deeper function (same-scope statements, including those
    nested in if/with/try blocks) — used to resolve names within a single function scope."""
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
    """True if ``scope`` (or a nested function scope) constructs a ``RunRequest`` whose
    effective ``mode`` resolves to ``"structured"``. Assignment bindings are collected PER
    FUNCTION SCOPE (plus inherited module/enclosing bindings), so a ``mode`` bound to
    ``"structured"`` in one function cannot leak into an unrelated ``RunRequest`` in another —
    matching Python's actual name binding rather than a flat module-wide guess."""
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
                # `RunRequest.for_structured(...)` FIXES mode="structured", so the keyword the
                # census reads is no longer written at the call site. Matching only the keyword
                # would silently drop every site that adopts the builder — the same fail-open
                # this census exists to rule out.
                return True
            if name == "for_structured":
                # The consolidated builder (story 44ff): `RunRequest.for_structured(...)` IS a
                # structured construction, with the mode fixed inside the builder rather than
                # passed at the call site. Before the consolidation these same consumers wrote
                # `RunRequest(mode=..., ...)` by hand, and a census that only knows the old
                # spelling reports them as gone the moment they are consolidated — measuring
                # the SPELLING rather than the property. There is no `mode` keyword to resolve
                # here: reaching the builder is itself the proof.
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
    """AC-1: the census MECHANISM must resolve computed and multi-line ``mode`` — the failure
    mode a literal ``RunRequest(mode="structured")`` grep hits (it matched ZERO sites because
    every construction spans lines, and it can never see a conditional ``mode``). The census
    must detect the computed-``mode`` consumer (``fidelity_spot_eval.py``), the out-of-tree
    script consumer (``jira_dc_capability_map.py``), and a broad set of multi-line sites."""
    files = _structured_consumer_files()
    assert "fidelity_spot_eval.py" in files, (
        "the census missed a COMPUTED-mode consumer a grep cannot see"
    )
    assert "jira_dc_capability_map.py" in files, (
        "the census missed the out-of-tree scripts/ consumer"
    )
    # The runner itself constructs the canonical RunRequest; the many plan/code-review and
    # workflow consumers push the count well past what a broken (zero-match) grep would report.
    assert len(files) >= 10, f"census under-counted structured consumers: {sorted(files)}"


# ───────────────────────── AC-3: in-Agent bounded retry, never a loop ───────────────────────


@pytest.fixture
def _run_sync_counter(monkeypatch):
    """Count outer ``Agent.run_sync`` invocations across a run, delegating to the real method.

    ``run_sync`` is the OUTER model run. A bounded output retry must NOT add one (it is an
    in-Agent ``ModelRetry``); a resurrected fresh-Agent scheduler WOULD add one per attempt.
    So this counter is the decisive no-bespoke-loop probe."""
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
    """AC-3 (the decisive no-loop proof): a transient-error first turn recovers on the good
    second turn via the IN-Agent bounded retry — TWO model requests but still EXACTLY ONE
    ``run_sync``. A bespoke fresh-Agent scheduler would instead issue a SECOND ``run_sync`` for
    the retry, so this pair (calls==2, run_sync==1) is what a resurrected loop cannot satisfy."""
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
    """AC-3: the bug-895c native→prompted downgrade is the ONE sanctioned second outer run. The
    native attempt is made once (here the provider 400s compiling the grammar), then the
    prompted attempt runs once — exactly TWO outer attempts, never a third. Counting the native
    attempt (patched to raise, as the S2 oracle does) plus the prompted ``run_sync`` proves the
    downgrade does not spin a loop."""
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
    """AC-4: ``structured_retry_limit=0`` clamps the output-retry allowance to zero — the knob
    survives the cutover as the single-shot abstain fail-safe overlap/judge.py and the
    contracts.py batch depend on."""
    cfg = LLMConfig(repo_path=".")
    req0 = dataclasses.replace(_req(cfg), structured_retry_limit=0)
    assert output_retry_allowance(req0) == 0


def test_structured_retry_limit_zero_is_single_shot_no_bounded_retry():
    """AC-4 (end-to-end teeth): a transient-error turn that WOULD normally drive one bounded
    retry (2 model requests, per the S2 oracle) instead aborts after EXACTLY ONE model request
    when ``structured_retry_limit=0`` — proving the fail-safe suppresses the output retry rather
    than merely lowering a number. A regression that ignored the knob would show calls==2."""
    cfg = LLMConfig(repo_path=".")
    model, state = _scripted_model([{"text": "boom", "finish_reason": "error"}, {"text": _VALID}])
    with pytest.raises(LLMRunnerError):
        PydanticAIRunner(cfg, model_override=model).run(
            dataclasses.replace(_req(cfg), structured_retry_limit=0)
        )
    assert state["calls"] == 1, "limit=0 is single-shot: the transient error is not retried"
