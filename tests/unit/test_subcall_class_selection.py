"""Hand-built LLM sub-calls must select their model from the model-CLASS vocabulary (bug afeb).

Four sites built a ``RunRequest`` with ``config=cfg`` and declared no model class, so they
inherited ``cfg.model`` and ignored ``[tool.rebar.llm.model_classes]`` entirely. MEASURED on the
ticket: with all three classes pointed at Bedrock and ``cfg.model`` left on direct Anthropic, 18 of
the 23 LLM calls in a plan review went to direct Anthropic.

Two kinds of test live here, and both are needed:

1. **Per-site runtime probes.** Each configures a class table whose values DIFFER from
   ``cfg.model`` — the only configuration in which "honoured the class" and "fell through to
   cfg.model" are distinguishable strings — and asserts the model that reaches the runner is the
   class value. The class table is the discriminator: with no table configured, ``frontier``
   resolves to the same default ``cfg.model`` carries and the observation would carry no
   information (the ticket's config-A/config-B analysis).
2. **A general provenance guard** (:func:`test_no_run_request_inherits_the_raw_config_model`) that
   fails for ANY ``RunRequest`` site in ``src/rebar`` whose config traces back to a bare
   ``LLMConfig.from_env()`` without passing through the class vocabulary. It enumerates nothing:
   a NEW hand-built sub-call with the same defect fails it on the day it is written.

The ``overlap-judge`` probe drives :func:`judge_one` DIRECTLY rather than through a gate. That is
deliberate: ``overlap/wire.py`` runs the judge only when BM25F retrieval returns candidates
(``if not candidates: return []``, with the query's own graph excluded), so a test that runs a gate
and hopes the judge fires silently passes while testing nothing — two probes during the
investigation hit exactly that and produced zero judge calls.
"""

from __future__ import annotations

import ast
import functools
import itertools
import pathlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any, NamedTuple

import pytest
from _tree_scan import parsed_python_files

from rebar.llm import config as llm_config
from rebar.llm.config import LLMConfig
from rebar.llm.runner import FakeRunner, Runner, RunRequest

pytestmark = pytest.mark.unit

# `cfg.model` and the three class slots are deliberately DISTINCT strings: a probe can then name
# which one arrived. `test` is NOT a provider name, so `split_provider_qualifier` reads these as
# unqualified — and since no inference prefix matches them either, `resolve_class` returns them
# unchanged rather than re-prefixed.
_CFG_MODEL = "anthropic:cfg-model-must-not-be-inherited"
_STANDARD = "test:standard-class-model"
_FRONTIER = "test:frontier-class-model"
_TRIVIAL = "test:trivial-class-model"

_DIGEST = {
    "problem_keywords": ["login", "session"],
    "component_or_area": "auth",
    "key_entities": ["SessionToken"],
    "propositions": ["users cannot authenticate", "session token is not persisted"],
}


@pytest.fixture(autouse=True)
def class_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure all three model classes away from ``cfg.model`` (the ticket's config B).

    Patched at ``_read_llm_file_table`` — the one function ``load_class_slots`` reads — rather than
    via the nine env vars, so the table is identical for every probe regardless of the ambient
    environment the conftest scrubs.
    """
    monkeypatch.setattr(
        llm_config,
        "_read_llm_file_table",
        lambda repo_root=None: {
            "model_classes": {
                "trivial": {"model": _TRIVIAL},
                "standard": {"model": _STANDARD},
                "frontier": {"model": _FRONTIER},
            }
        },
    )


class _Recorder(Runner):
    """Records the model on every request's config, then answers with a canned payload.

    Recording happens BEFORE the payload is produced, so a site that swallows downstream errors
    (the novelty and overlap sub-calls both do, by design) still yields the observation.
    """

    name = "recorder"

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.models: list[str] = []
        self._payload = payload

    def preflight(self) -> None:
        pass

    def run(self, req: RunRequest) -> dict:
        self.models.append(req.config.model)
        if req.mode != "structured":
            return FakeRunner(findings=[]).run(req)
        return {**(self._payload or {}), "runner": self.name, "model": None, "trace_id": None}


def _cfg(**kw: Any) -> LLMConfig:
    return replace(LLMConfig(model=_CFG_MODEL), **kw)


def _only_model(rec: _Recorder) -> str:
    assert rec.models, (
        "the sub-call never reached the runner — this probe would pass vacuously; construct the "
        "trigger deliberately rather than relying on a gate to fire it"
    )
    assert len(set(rec.models)) == 1, f"calls disagreed on their model: {rec.models}"
    return rec.models[0]


# ── per-site probes: the model reaching the runner is the class value, not cfg.model ──────────


def test_overlap_judge_selects_the_standard_class() -> None:
    from rebar.llm.overlap.judge import judge_one

    rec = _Recorder({"relation": "unrelated", "confidence": 0.0, "abstain": True})
    judge_one(dict(_DIGEST), dict(_DIGEST), _cfg(), rec)
    assert _only_model(rec) == _STANDARD


def test_overlap_judge_selects_the_standard_class_for_every_pair() -> None:
    """The volume site: one plan review made 18 of its 23 calls here, so the binding has to hold
    per call and not only on the first."""
    from rebar.llm.overlap.judge import _CANDIDATES_PER_CALL, judge

    # Enough candidates to fill more than one BATCH: the judge sends one call per batch per
    # ordering, so a corpus one over the bound is the smallest that still proves the binding is
    # re-applied on a LATER call rather than only on the first.
    ids = [f"C{i}" for i in range(_CANDIDATES_PER_CALL + 1)]
    rec = _Recorder({"relation": "unrelated", "confidence": 0.0, "abstain": True})
    judge(
        "Q",
        dict(_DIGEST),
        ids,
        {i: dict(_DIGEST) for i in ids},
        config=_cfg(),
        runner=rec,
    )
    assert len(rec.models) == 4  # two batches x both orderings
    assert _only_model(rec) == _STANDARD


def test_ticket_digest_selects_the_trivial_class() -> None:
    """`trivial`: the ticket-digest prompt is a single-turn, tool-less extractor of four
    structured fields ("Not a reviewer" in its own frontmatter) — narrow canonicalizing work, and
    the highest-volume site of the four since it runs on every ticket store write."""
    from rebar.llm.enrich import enrich

    rec = _Recorder(dict(_DIGEST))
    enrich(text="Login is broken.", config=_cfg(), runner=rec)
    assert _only_model(rec) == _TRIVIAL


def test_ticket_digest_holds_on_the_store_write_path() -> None:
    """``enrich_drain.maybe_drain`` runs on the ticket STORE WRITE path (``event_append`` /
    ``push``), so the binding must hold for a caller that builds its own config from the
    environment and never passes one in — not only for a gate that hands ``enrich`` a config."""
    from rebar.llm.enrich import enrich

    rec = _Recorder(dict(_DIGEST))
    enrich(text="Login is broken.", config=None, runner=rec)
    assert _only_model(rec) == _TRIVIAL


def test_review_ticket_uses_the_operators_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """BY DESIGN, and registered as such: `review_ticket` is a top-level op's single LLM call, so
    there are no passes to differentiate and the operator's configured model is the right knob. The
    class vocabulary exists to spend differently ACROSS a gate's passes. Binding a class here would
    remove `llm.model` as a steering knob for this command and give nothing back. Same reasoning as
    `spec_scan`. (Retirement of this op is ticket 316a; if it goes, so does this test.)"""
    from rebar.llm import operations

    monkeypatch.setattr(
        operations, "assemble_context", lambda tid, *, graph, repo_root: ("ctx", [tid])
    )
    rec = _Recorder()
    cfg = _cfg()
    # `source="local"` is REQUIRED, not incidental. The conftest pins the suite to
    # `REBAR_GATE_SOURCE=attested`, and attested mode materializes the pinned snapshot — two real
    # `git fetch` subprocesses against `origin`. This test is about which model reaches the runner
    # and has no business touching the network: the checkout the CI gate runs in has no `origin`
    # remote, so the fetch cannot succeed and blocks until the snapshot timeout. An explicit
    # `source` argument wins over the environment (see `gate_source.resolve_gate_handle`).
    operations._review_ticket_impl(
        "abc123", "ticket-quality", config=cfg, runner=rec, source="local"
    )
    assert _only_model(rec) == cfg.model


def test_code_novelty_selects_the_standard_class() -> None:
    from rebar.llm.code_review.workflow_ops import score_code_novelty

    rec = _Recorder({"novelties": []})
    score_code_novelty(
        [{"finding": "f", "criteria": ["correctness"], "location": "a.py:1"}],
        [{"id": "p1", "finding": "prior"}],
        diff_text="--- a\n+++ b\n+x\n",
        cfg=_cfg(),
        runner=rec,
    )
    assert _only_model(rec) == _STANDARD


# ── the fail-safe behaviour the class binding must not disturb ────────────────────────────────


class _BoomRunner(Runner):
    name = "boom"

    def preflight(self) -> None:
        pass

    def run(self, req: RunRequest) -> dict:
        raise RuntimeError("provider down")


def test_code_novelty_still_degrades_to_keeping_more_findings() -> None:
    """A broken novelty signal must yield ``{}`` (every finding scores 0.0 ⇒ kept), never a raise:
    the floor can then only keep MORE, never drop wrongly."""
    from rebar.llm.code_review.workflow_ops import score_code_novelty

    assert (
        score_code_novelty(
            [{"finding": "f"}],
            [{"id": "p1"}],
            diff_text="d",
            cfg=_cfg(),
            runner=_BoomRunner(),
        )
        == {}
    )


def test_overlap_judge_failure_is_an_abstain_not_a_raise() -> None:
    """The overlap step is advisory and must never block a review."""
    from rebar.llm.overlap.judge import judge, judge_one

    assert judge_one(dict(_DIGEST), dict(_DIGEST), _cfg(), _BoomRunner())["abstain"] is True
    assert (
        judge("Q", dict(_DIGEST), ["C"], {"C": dict(_DIGEST)}, config=_cfg(), runner=_BoomRunner())
        == []
    )


# ── the general guard: no RunRequest may inherit an unbound cfg.model ─────────────────────────

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "rebar"

# Text that marks a config as bound to the model-class vocabulary. A site (or an assignment
# feeding one) that mentions any of these has DECLARED its class; that is the whole obligation.
_CLASS_BINDERS = (
    "resolve_model_string",
    "resolve_model",
    "resolve_class",
    "STANDARD_CLASS",
    "TRIVIAL_CLASS",
    "FRONTIER_CLASS",
    "model_ladder",
    "_verifier_cfg",
    "_verifier_model_for_completion",
)

# An expression that MINTS a config straight from the environment/operator settings. A config that
# reaches a RunRequest from one of these without crossing a binder above is exactly bug afeb.
_RAW_ORIGIN = "LLMConfig.from_env"

# Helpers that return a COPY of the config with a NON-model field adjusted — the output-token
# budget. They are transparent to MODEL provenance, so the analysis follows through them to their
# argument instead of stopping at the call. Stopping would be the dangerous reading: it renders a
# site `unresolved`, and the only way to pass then is to register it as unfollowable, which would
# blind this guard at the very plan-review passes bug afeb is about.
_MODEL_TRANSPARENT = ("max_output_cfg", "_max_output_cfg")


def _unwrap_model_transparent(expr: str) -> str | None:
    """The inner config expression of a model-transparent wrapper call, else ``None``."""
    try:
        node = ast.parse(expr, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call) or len(node.args) != 1:
        return None
    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    return ast.unparse(node.args[0]) if name in _MODEL_TRANSPARENT else None


# Sites that inherit `cfg.model` ON PURPOSE. Registration is a DELIBERATE act, and it is the
# ONLY way a raw site passes: a new hand-built sub-call fails until someone either declares a
# class or writes down why the operator's bare model is the right one there.
_CFG_MODEL_BY_DESIGN: dict[str, str] = {}

# Sites whose config provenance this analysis cannot follow — it stops at attribute access
# (`self._config`) and at parameters whose callers are outside `src/rebar` — each with the reason
# it is not a bug-afeb site. Same ratchet: unregistered means failing.
_UNFOLLOWABLE: dict[str, str] = {
    "llm/workflow/completion_recovery.py::_run_one_successor": (
        "config is `self._config` (an attribute): a batched recovery successor re-runs the "
        "verifier on the runner's own config, deliberately keeping whatever model the primary "
        "attempt used"
    ),
    "llm/workflow/completion_recovery.py::_run_finalizer": (
        "config is `self._config` (an attribute): the finalizer assembles the full-coverage "
        "verdict from the bank on the runner's own config, deliberately keeping the primary "
        "attempt's model"
    ),
    "llm/plan_review/passes.py::pass1_chunk": (
        "Pass-1 finder: the batch runner copies `model_ladder[0]` (a CLASS name) onto cfg.model "
        "before calling, and escalation replaces it per attempt — measured on Bedrock in the "
        "ticket's config B"
    ),
    "llm/plan_review/prerequisites.py::run_focused_finder": (
        "per-call `call_cfg` from the size ladder; story b690 made it effective and the ticket "
        "measured `plan-review-prerequisite-verifier` on Bedrock"
    ),
    "llm/evals/eval_solver.py::_run_novelty_case": "eval harness: pins the model under eval",
    "llm/evals/eval_solver.py::_run_code_review_case": "eval harness: pins the model under eval",
    "llm/evals/eval_solver.py::_run_verifier_case": "eval harness: pins the model under eval",
    "llm/evals/plan_replay/tier1.py::build_candidate_runner": (
        "eval harness (ticket presolar-finable-binturong): cfg.model is "
        "`parity.resolve_pinned_model('pass2').model_id`, a fully-resolved Bedrock "
        "standard-class model string constructed just above this call, not the operator's "
        "configured model"
    ),
    "llm/evals/plan_replay/tier2.py::run_tier2_full": (
        "eval harness (ticket peaceable-choppy-sapsucker): cfg.model is "
        "`parity.resolve_pinned_model('pass1').model_id`, a fully-resolved Bedrock "
        "frontier-class model string constructed just above this call, not the operator's "
        "configured model"
    ),
    "llm/plan_review/fidelity_spot_eval.py::_relocation_requests": (
        "eval harness: compares two prompts on ONE fixed model, so cfg.model is the control"
    ),
    "llm/operations.py::_review_ticket_impl": (
        "`rebar.llm.review_ticket`'s PRIMARY op call, not a sub-call (story 316a split the "
        "public wrapper, which warns, from this implementation, which is what the deprecated "
        "op and its internal callers actually run). ec44 cut this over from bare "
        "`LLMConfig.from_env()` to `resolve_gate_config()` (the boundary-composed snapshot), so "
        "the analysis no longer traces to a bare raw origin — it stops at the opaque resolver "
        "call. A top-level op makes ONE call, so there are no passes to differentiate and the "
        "operator's configured model is the right knob; the class vocabulary exists to spend "
        "differently ACROSS a gate's passes. Same reasoning as spec_scan below. The op's CLI "
        "verb (`rebar review`) is already retired as a forwarding shim over `rebar review-plan`."
    ),
    "llm/spec_scan.py::_scan_epics_inner": (
        "`scan-spec`'s PRIMARY op call, not a sub-call — a top-level op runs the operator's "
        "configured model. ec44 moved the compose/bind onto the `scan_epics_for_spec` wrapper "
        "(`compose_and_bind_llm_config` + `gate_source.apply_handle`), so `cfg` now arrives here "
        "as a parameter bound through a `with ... as` binding this analysis does not follow. It "
        "was also the one site the ticket's measurement could not exercise (it needs a "
        "--spec-file), so afeb scoped it out rather than change it unmeasured."
    ),
}


@functools.cache
def _parsed(path: pathlib.Path) -> ast.Module:
    """``ast.parse`` of one source file, memoised for this process.

    The provenance scan reads the same files over and over: ``_verdict`` re-walks the
    WHOLE tree once per site whose config arrives as a parameter, and ``_functions``
    walks it again. Memoising by path collapses that to one read+parse per file
    (ticket fa90-3292-38d4-4fd2). Safe because ``src/rebar`` is READ-ONLY to this
    suite — no test in this module writes it — and the cache dies with the process,
    so it can never be served to a later pytest session.
    """
    return ast.parse(path.read_text())


_Function = ast.FunctionDef | ast.AsyncFunctionDef
_FunctionSite = tuple[pathlib.Path, _Function]


class _CallSite(NamedTuple):
    path: pathlib.Path
    tree: ast.Module
    node: ast.Call
    #: Discovery position across the whole corpus. `RunRequest(...)` and
    #: `RunRequest.for_structured(...)` land in DIFFERENT name buckets, so merging them back
    #: into one corpus needs the original walk order; sorting by line number would not do —
    #: `ast.walk` is breadth-first, so a call nested one level deeper is discovered later even
    #: when it appears earlier in the file.
    order: int


class _AstIndex(NamedTuple):
    """Immutable relationships derived once from the process's cached source trees."""

    parents_by_tree: Mapping[ast.AST, Mapping[ast.AST, ast.AST]]
    functions_by_name: Mapping[str, tuple[_FunctionSite, ...]]
    calls_by_name: Mapping[str, tuple[_CallSite, ...]]


@functools.cache
def _ast_index() -> _AstIndex:
    """Index each parsed source tree once for parent, function, and caller queries."""
    order = itertools.count()
    parents_by_tree: dict[ast.AST, Mapping[ast.AST, ast.AST]] = {}
    functions_by_name: dict[str, list[_FunctionSite]] = {}
    calls_by_name: dict[str, list[_CallSite]] = {}

    for module in parsed_python_files(_SRC):
        path = module.path
        tree = module.tree
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                functions_by_name.setdefault(node.name, []).append((path, node))
            if isinstance(node, ast.Call):
                called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if called is not None:
                    calls_by_name.setdefault(called, []).append(
                        _CallSite(path, tree, node, next(order))
                    )
        parents_by_tree[tree] = MappingProxyType(parents)

    return _AstIndex(
        parents_by_tree=MappingProxyType(parents_by_tree),
        functions_by_name=MappingProxyType(
            {name: tuple(sites) for name, sites in functions_by_name.items()}
        ),
        calls_by_name=MappingProxyType(
            {name: tuple(sites) for name, sites in calls_by_name.items()}
        ),
    )


def _functions() -> dict[str, list[_FunctionSite]]:
    """Every function/method in ``src/rebar``, indexed by its bare name (the granularity a call
    site gives us: ``completion_subcall.pass2_completion(...)`` and
    ``pass2_completion(...)`` both resolve by
    ``pass2_completion``)."""
    return {name: list(sites) for name, sites in _ast_index().functions_by_name.items()}


def _enclosing(tree: ast.AST, target: ast.AST) -> list[_Function]:
    """The chain of function definitions containing ``target``, innermost first."""
    parents = _ast_index().parents_by_tree[tree]
    chain: list[_Function] = []
    cur = target
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
            chain.append(cur)
    return chain


def _assignments(fns: list[Any], name: str) -> list[str]:
    """The unparsed right-hand sides assigned to ``name`` anywhere in ``fns``."""
    out = []
    for fn in fns:
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                out.append(ast.unparse(node.value))
    return out


def _params(fn: Any) -> list[str]:
    args = fn.args
    return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]


def _arg_for_param(call: ast.Call, fn: Any, param: str) -> str | None:
    """The caller's expression bound to ``fn``'s ``param`` at ``call``, or None if not passed."""
    for kw in call.keywords:
        if kw.arg == param:
            return ast.unparse(kw.value)
    positional = [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)]
    if param in positional:
        idx = positional.index(param)
        if idx < len(call.args):
            return ast.unparse(call.args[idx])
    return None


def _combine(verdicts: set[str]) -> str:
    """Fold sibling verdicts. ``bound`` WINS over ``raw``: the shape of the fix is a
    reassignment (``cfg = replace(cfg, model=resolve_model_string(...))``), so a function whose
    config is minted raw and then rebound must read as bound — the engine's own
    ``RunnerAgentStep`` does exactly that with ``resolve_model``. The cost is that a function
    which binds a class for one call and passes the raw config to a second RunRequest reads as
    bound; the per-site probes above cover the sites where that would matter."""
    if "bound" in verdicts:
        return "bound"
    return "raw" if "raw" in verdicts else "unresolved"


def _verdict(tree: ast.AST, site: ast.Call, expr: str, depth: int) -> str:
    """``"bound"`` | ``"raw"`` | ``"unresolved"`` for the config expression ``expr`` at ``site``.

    Backward provenance, one hop at a time: a class binder anywhere in the chain cleanses it, a
    bare ``LLMConfig.from_env`` at the end of it is the defect, and a parameter transfers the
    obligation to the function's callers.
    """
    if any(binder in expr for binder in _CLASS_BINDERS):
        return "bound"
    if _RAW_ORIGIN in expr:
        return "raw"
    if depth > 6:
        return "unresolved"
    inner = _unwrap_model_transparent(expr)
    if inner is not None:
        return _verdict(tree, site, inner, depth + 1)
    if not expr.isidentifier():
        return "unresolved"  # attribute/subscript/deep chain: out of this analysis's reach

    chain = _enclosing(tree, site)
    if not chain:
        return "unresolved"
    rhs = _assignments(chain, expr)
    if rhs:
        return _combine({_verdict(tree, site, r, depth + 1) for r in rhs})

    # A parameter: the obligation belongs to whoever supplies it.
    owner = next((fn for fn in chain if expr in _params(fn)), None)
    if owner is None:
        return "unresolved"
    seen: set[str] = set()
    for caller in _ast_index().calls_by_name.get(owner.name, ()):
        passed = _arg_for_param(caller.node, owner, expr)
        if passed is None:
            continue
        if _enclosing(caller.tree, caller.node):
            seen.add(_verdict(caller.tree, caller.node, passed, depth + 1))
        else:
            seen.add("unresolved")
    return _combine(seen) if seen else "unresolved"


def _is_run_request_construction(func: ast.expr) -> bool:
    """``RunRequest(...)`` or its ``RunRequest.for_structured(...)`` builder.

    Matching the bare name alone would make this scan FAIL OPEN the day a site adopts the
    builder: the single-turn structured sites would silently leave the corpus and every guard
    below would pass on a smaller one — the exact vacuous-pass failure
    :func:`test_the_provenance_analysis_can_see_the_sites_it_judges` exists to catch.
    """
    if isinstance(func, ast.Name):
        return func.id == "RunRequest"
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "for_structured"
        and isinstance(func.value, ast.Name)
        and func.value.id == "RunRequest"
    )


def _run_request_sites() -> list[tuple[str, str, str]]:
    """``(key, config_expr, verdict)`` for every ``RunRequest`` construction in src/rebar."""
    sites = []
    index = _ast_index()
    found = (
        *index.calls_by_name.get("RunRequest", ()),
        *index.calls_by_name.get("for_structured", ()),
    )
    for call_site in sorted(found, key=lambda site: site.order):
        node = call_site.node
        if not _is_run_request_construction(node.func):
            continue
        cfg_kw = next((k.value for k in node.keywords if k.arg == "config"), None)
        expr = ast.unparse(cfg_kw) if cfg_kw is not None else ""
        chain = _enclosing(call_site.tree, node)
        outer = chain[-1].name if chain else "<module>"
        key = f"{call_site.path.relative_to(_SRC).as_posix()}::{outer}"
        sites.append((key, expr, _verdict(call_site.tree, node, expr, 0)))
    return sites


_EXPECTED_RUN_REQUEST_SITES = [
    ("llm/code_review/workflow_ops.py::score_code_novelty", "verify.max_output_cfg(cfg)", "bound"),
    ("llm/enrich.py::enrich", "cfg", "bound"),
    ("llm/epic_bug_screen.py::_screen_one", "cfg", "bound"),
    ("llm/evals/eval_solver.py::_run_code_review_case", "cfg", "unresolved"),
    ("llm/evals/eval_solver.py::_run_novelty_case", "cfg", "unresolved"),
    ("llm/evals/eval_solver.py::_run_verifier_case", "cfg", "unresolved"),
    ("llm/evals/plan_replay/tier1.py::build_candidate_runner", "cfg", "unresolved"),
    ("llm/evals/plan_replay/tier2.py::run_tier2_full", "cfg", "unresolved"),
    ("llm/operations.py::_review_ticket_impl", "max_output_cfg(cfg)", "unresolved"),
    ("llm/overlap/judge.py::judge_one", "cfg", "bound"),
    ("llm/overlap/judge.py::judge_batch", "cfg", "bound"),
    (
        "llm/plan_review/completion_subcall.py::pass2_completion",
        "_max_output_cfg(cfg)",
        "bound",
    ),
    ("llm/plan_review/fidelity_spot_eval.py::_relocation_requests", "cfg", "unresolved"),
    ("llm/plan_review/fidelity_spot_eval.py::_relocation_requests", "cfg", "unresolved"),
    # `_score_floor_novelty` moved from `plan_review/__init__.py` to the new sibling
    # `plan_review/floors.py` (ticket 02b7, module-size headroom extraction).
    ("llm/plan_review/floors.py::_score_floor_novelty", "vcfg", "bound"),
    ("llm/plan_review/passes.py::pass1_chunk", "_max_output_cfg(cfg)", "unresolved"),
    ("llm/plan_review/passes.py::pass1_container", "_max_output_cfg(cfg)", "bound"),
    ("llm/plan_review/passes.py::pass1_isf", "_max_output_cfg(cfg)", "bound"),
    ("llm/plan_review/passes.py::summarize_for_isf", "_max_output_cfg(cfg)", "bound"),
    ("llm/plan_review/prerequisites.py::run_focused_finder", "call_cfg", "unresolved"),
    ("llm/plan_review/xcheck.py::_assess_contradictions", "vcfg", "bound"),
    ("llm/plan_review/xcheck.py::_assess_comment_trail", "vcfg", "bound"),
    ("llm/spec_scan.py::_scan_epics_inner", "cfg", "unresolved"),
    (
        "llm/workflow/completion_recovery.py::_run_one_successor",
        "self._config",
        "unresolved",
    ),
    ("llm/workflow/completion_recovery.py::_run_finalizer", "self._config", "unresolved"),
    ("llm/workflow/runs.py::build_agent_request", "cfg", "bound"),
]


@pytest.mark.repo_policy
def test_provenance_scan_preserves_verdicts_with_linear_whole_tree_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Freeze semantic coverage and the deterministic work budget, not elapsed time."""
    original_walk = ast.walk
    module_walks: Counter[int] = Counter()

    def counted_walk(node: ast.AST):
        if isinstance(node, ast.Module):
            module_walks[id(node)] += 1
        return original_walk(node)

    monkeypatch.setattr(ast, "walk", counted_walk)
    _ast_index.cache_clear()
    _parsed.cache_clear()

    sites = _run_request_sites()

    assert len(_EXPECTED_RUN_REQUEST_SITES) == 26
    assert all(expr for _, expr, _ in _EXPECTED_RUN_REQUEST_SITES)
    assert sites == _EXPECTED_RUN_REQUEST_SITES
    assert len(module_walks) >= 400, "the oracle did not exercise the real source corpus"
    repeated = [count for count in module_walks.values() if count > 3]
    assert not repeated, (
        "each parsed module may be walked for function discovery, site discovery, and one "
        f"shared relationship index; {len(repeated)} modules exceeded that budget "
        f"(maximum {max(repeated, default=0)})"
    )


@pytest.fixture(scope="session")
def run_request_sites() -> list[tuple[str, str, str]]:
    """The provenance analysis, derived ONCE per test process.

    The four guards below each used to call :func:`_run_request_sites`, re-deriving an
    identical, immutable answer four times over (ticket fa90-3292-38d4-4fd2). A session
    fixture is the whole cache: it is bounded by the pytest session that created it, so
    nothing can survive a source change into a later run.
    """
    return _run_request_sites()


@pytest.mark.repo_policy
def test_the_provenance_analysis_can_see_the_sites_it_judges(
    run_request_sites: list[tuple[str, str, str]],
) -> None:
    """Guards the guard: if `RunRequest` construction moves behind a factory this scan finds
    nothing and every assertion below passes vacuously."""
    sites = run_request_sites
    assert len(sites) >= 15, f"only {len(sites)} RunRequest sites found — the scan is not working"
    assert all(expr for _, expr, _ in sites), "a RunRequest site passes no config= at all"


@pytest.mark.repo_policy
def test_no_run_request_inherits_the_raw_config_model(
    run_request_sites: list[tuple[str, str, str]],
) -> None:
    """THE general defect: a config minted by ``LLMConfig.from_env()`` reaching a ``RunRequest``
    without crossing the model-class vocabulary. Bug afeb was four instances of it; this fails on
    the next one too, without naming any of them."""
    offenders = {
        key: expr
        for key, expr, verdict in run_request_sites
        if verdict == "raw" and key not in _CFG_MODEL_BY_DESIGN
    }
    assert not offenders, (
        "these RunRequest sites inherit cfg.model instead of selecting a model class "
        f"(bug afeb): {offenders}\n"
        "Bind a class at the site — e.g. "
        "`cfg = replace(cfg, model=resolve_model_string(STANDARD_CLASS))` — choosing the class "
        "from the PROMPT's shape: tool-less constrained extraction -> `trivial`, single-turn "
        "judging/verification -> `standard`, an agentic open-ended finder -> `frontier`. "
        "If cfg.model really is right there, register the site in _CFG_MODEL_BY_DESIGN."
    )


@pytest.mark.repo_policy
def test_every_unfollowable_site_is_registered_with_a_reason(
    run_request_sites: list[tuple[str, str, str]],
) -> None:
    unresolved = {key for key, _, verdict in run_request_sites if verdict == "unresolved"}
    assert unresolved <= set(_UNFOLLOWABLE), (
        "new RunRequest site(s) whose config provenance cannot be followed: "
        f"{sorted(unresolved - set(_UNFOLLOWABLE))}. Either declare a model class at the "
        "site, or register it above with the reason cfg.model is correct there."
    )


@pytest.mark.repo_policy
def test_neither_registry_has_stale_entries(
    run_request_sites: list[tuple[str, str, str]],
) -> None:
    """An entry that no longer matches a real site would silently license a future violation."""
    by_verdict: dict[str, set[str]] = {}
    for key, _, verdict in run_request_sites:
        by_verdict.setdefault(verdict, set()).add(key)
    assert set(_CFG_MODEL_BY_DESIGN) <= by_verdict.get("raw", set()), (
        "_CFG_MODEL_BY_DESIGN entries that are no longer raw-config sites: "
        f"{sorted(set(_CFG_MODEL_BY_DESIGN) - by_verdict.get('raw', set()))}"
    )
    assert set(_UNFOLLOWABLE) <= by_verdict.get("unresolved", set()), (
        "_UNFOLLOWABLE entries that are no longer unfollowable sites: "
        f"{sorted(set(_UNFOLLOWABLE) - by_verdict.get('unresolved', set()))}"
    )
