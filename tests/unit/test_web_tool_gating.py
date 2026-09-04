"""Web access for web-flagged AGENT criteria (bug ff64-ca12-7132-40e7, bug 129e-2d88-cce2-492c).

ff64 gave the T1 prior-art criterion a web-search tool, attached only when the resolved model
string started with ``anthropic``. 129e: the production review bot moved to Bedrock, so
``resolved`` became ``bedrock:us.anthropic.claude-opus-4-8`` — and T1, a BLOCKING criterion whose
routing declares ``"web": true``, silently ran with no grounding tool at all. The same MODEL
through a different PROVIDER lost a capability because of how its name was spelled.

The fix, pinned here: web access is attached on EVERY provider (the provider's native tool where
its profile supports it, an in-process fallback where it does not), the decision consults no
provider-name string, and the signed verdict's provenance records the outcome AND the route so a
future silent withdrawal cannot hide. No test makes a live web/model call.
"""

from __future__ import annotations

import inspect
import json
from importlib import resources
from types import SimpleNamespace
from typing import ClassVar

import pytest

from rebar.llm import structured_run as structured_run_mod
from rebar.llm.capabilities import (
    ModelCapabilities,
    capabilities_for,
    provenance_for,
    web_access_provenance,
    web_search_capabilities,
)
from rebar.llm.config import LLMConfig
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytest.importorskip("pydantic_ai")


# A Claude model reached through Bedrock — the production review bot's model since the eb6e
# cutover, and the exact string the old prefix gate withheld the tool from.
_BEDROCK_CLAUDE = "bedrock:us.anthropic.claude-opus-4-8"


def _bedrock_claude_profile():
    """Bedrock-hosted Claude's real ``ModelProfile``, straight from pydantic-ai's provider —
    no boto3 client, no credentials, no call (``model_profile`` is a pure staticmethod)."""
    from pydantic_ai.providers.bedrock import BedrockProvider

    return BedrockProvider.model_profile("us.anthropic.claude-opus-4-8")


# ── the capability builder: universal, and provider-name-blind ─────────────────


def test_web_capability_is_attached_and_carries_both_routes():
    """One ``WebSearch`` serves every provider: pydantic-ai keeps the native tool where the
    model supports it and falls back to the local tool where it does not. Both arms must be
    present — a capability with only one arm is what made this provider-dependent."""
    caps = web_search_capabilities(web=True)
    from pydantic_ai.capabilities import WebSearch
    from pydantic_ai.native_tools import WebSearchTool

    assert caps is not None and len(caps) == 1
    tool = caps[0]
    assert isinstance(tool, WebSearch)
    # The NATIVE (provider-side) arm, preferred wherever the model supports it.
    assert [type(t).__name__ for t in tool.get_native_tools()] == [WebSearchTool.__name__]
    # The LOCAL arm SURVIVES. This is the trap the fix had to route around: setting
    # `max_uses`/`blocked_domains`/`allowed_domains` at the CAPABILITY level flips
    # pydantic-ai's `_requires_native()` True, which suppresses the local fallback and would
    # hard-fail on Bedrock — re-creating this very bug in the name of hardening it.
    assert tool.get_toolset() is not None, (
        "the local fallback was suppressed — web access would be provider-dependent again"
    )


def test_web_capability_bounds_both_routes():
    """The security posture is code, not prose: a BLOCKING reviewer must not be able to loop
    the web tool or pull unbounded third-party text into its context."""
    tool = web_search_capabilities(web=True)[0]
    (native,) = tool.get_native_tools()
    assert native.max_uses is not None and native.max_uses > 0, (
        "provider-side searches must be bounded per run"
    )
    # The local arm's own bound rides the DuckDuckGo tool (max_uses is native-only), so assert
    # the local tool was configured rather than default-constructed.
    local_impl = tool.local.function.__self__  # DuckDuckGoSearchTool behind the Tool
    assert local_impl.max_results is not None and local_impl.max_results > 0, (
        "local search results must be bounded per call"
    )


def test_the_decision_takes_no_model_or_provider_input():
    """The property epic 061c's zero-exceptions rule cares about, asserted structurally: the
    decision cannot consult a provider-name string because it is handed none. RED if a
    ``resolved``/model parameter is reintroduced — which is how the prefix gate got in."""
    params = inspect.signature(web_search_capabilities).parameters
    assert set(params) == {"web"}, (
        f"web access must not depend on any model/provider input, got {sorted(params)}"
    )
    assert params["web"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_prefix_gated_helper_is_gone_and_the_call_site_passes_no_model():
    """Belt to the signature test's braces, at the CALL SITE. The gate could return by threading
    the resolved model string back in, so assert on real AST: ``anthropic_model`` no longer
    defines the prefix-gated helper, and every ``web_search_capabilities`` call in the runner
    passes ONLY ``web=`` — no positional argument, no model/provider keyword.

    (``capabilities.py`` itself is covered by f184's attested no-prefix-matching guard in
    ``test_bedrock_provider.py``; this test deliberately does not duplicate or weaken it.)"""
    import ast
    import pathlib

    import rebar.llm.anthropic_model as anthropic_mod
    import rebar.llm.runner as runner_mod

    assert not hasattr(anthropic_mod, "_anthropic_web_search_capabilities"), (
        "the anthropic-prefix-gated web helper is back"
    )

    tree = ast.parse(pathlib.Path(runner_mod.__file__).read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "web_search_capabilities"
    ]
    assert calls, "the runner no longer calls web_search_capabilities at all"
    for call in calls:
        assert not call.args, f"model/provider passed positionally at line {call.lineno}"
        assert [kw.arg for kw in call.keywords] == ["web"], (
            f"web access decided from more than the flag at line {call.lineno}: "
            f"{[kw.arg for kw in call.keywords]}"
        )


def test_run_request_web_defaults_false():
    req = RunRequest(system_prompt="s", instructions="i", config=LLMConfig(model="m"))
    assert req.web is False


# ── provenance: the outcome and the route are on the signed record ─────────────


def test_provenance_records_the_web_access_route_per_provider():
    """The observability half of the fix. 129e survived the whole Bedrock cutover because a
    verdict recorded four capability facts and NOTHING about web search, so no reader could see
    a blocking criterion's grounding tool had been withdrawn. RED if the field is removed."""
    anthropic_caps = capabilities_for("anthropic:claude-opus-4-8")
    bedrock_caps = capabilities_for(
        SimpleNamespace(
            profile=_bedrock_claude_profile(), model_name="us.anthropic.claude-opus-4-8"
        )
    )
    # MEASURED (ticket 129e): Bedrock cannot carry the server-side tool; direct Anthropic can.
    assert anthropic_caps.native_web_search is True
    assert bedrock_caps.native_web_search is False

    native_rec = provenance_for(
        provider="anthropic",
        model="anthropic:claude-opus-4-8",
        base_url=None,
        caps=anthropic_caps,
        web=True,
    )
    local_rec = provenance_for(
        provider="bedrock", model=_BEDROCK_CLAUDE, base_url=None, caps=bedrock_caps, web=True
    )
    off_rec = provenance_for(
        provider="bedrock", model=_BEDROCK_CLAUDE, base_url=None, caps=bedrock_caps, web=False
    )
    assert native_rec["capabilities"]["web_access"] == "native"
    assert local_rec["capabilities"]["web_access"] == "local"
    assert off_rec["capabilities"]["web_access"] == "off"
    # Persisted into a signed sidecar payload — a non-serializable value would break the write.
    for rec in (native_rec, local_rec, off_rec):
        json.loads(json.dumps(rec))


def test_web_access_provenance_never_claims_a_route_that_was_not_attached():
    """``off`` wins over the route whenever the run did not attach the capability, so the
    record cannot attest grounding a reviewer never had."""
    for native in (True, False):
        caps = ModelCapabilities(
            native_structured_output=False,
            prompt_cache_style="none",
            supports_thinking=False,
            native_web_search=native,
        )
        assert web_access_provenance(caps, web=False) == "off"
        assert web_access_provenance(caps, web=True) == ("native" if native else "local")


def test_conservative_record_claims_no_provider_side_tool():
    """An unresolvable model must not be credited with a native tool it cannot evidence — that
    would put an unverified fact into a SIGNED record."""
    assert capabilities_for(object()).native_web_search is False


# ── runner threading: every provider gets it; unflagged stays byte-identical ───


class _CaptureAgent:
    """Stands in for ``pydantic_ai.Agent`` via the ``_import_pydantic_ai`` seam:
    records the construction kwargs and returns a canned text result — no model,
    no network."""

    captured: ClassVar[list[dict]] = []

    def __init__(self, model, **kwargs):
        _CaptureAgent.captured.append({"model": model, "kwargs": kwargs})

    def run_sync(self, prompt, usage_limits=None):
        return SimpleNamespace(output="ok", usage=SimpleNamespace(), response=None)


def _agent_kwargs(monkeypatch, tmp_path, *, model: str, web: bool) -> dict:
    """Run a single-turn text request through PydanticAIRunner with the Agent class
    swapped for the capture double; return the Agent construction kwargs."""

    monkeypatch.setattr(structured_run_mod, "_import_pydantic_ai", lambda: _CaptureAgent)
    # The real anthropic-path model construction needs a key present (never called).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-used")
    # The bedrock path builds a real BedrockProvider (only the Agent is doubled), and that
    # construction resolves BOTH a region and credentials — so without pinning each, this
    # case reads the host's ambient AWS config and passes on a developer box while failing
    # in CI, which has neither. MEASURED both arms:
    #   * no region  -> LLMConfigError from build_bedrock_provider's boto3 pre-check;
    #   * no creds   -> botocore reaches for IMDS and trips the network-forbidden fixture.
    # Region goes on the field, not via REBAR_LLM_BEDROCK_REGION: that env var is read only
    # by LLMConfig's env/table factory, not by this direct construction. Credentials go via
    # botocore's env provider, which short-circuits the IMDS lookup. No client call is ever
    # made, so neither value is used.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key-never-used")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-never-used")
    cfg = LLMConfig(model=model, repo_path=str(tmp_path), bedrock_region_name="us-east-1")
    req = RunRequest(
        system_prompt="s",
        instructions="i",
        config=cfg,
        reviewers=["plan-reviewer"],
        mode="text",
        execution_mode="single_turn",
        web=web,
    )
    before = len(_CaptureAgent.captured)
    PydanticAIRunner(cfg).run(req)
    assert len(_CaptureAgent.captured) == before + 1
    return _CaptureAgent.captured[-1]["kwargs"]


# Every provider rebar's own prefix map can emit, plus the Bedrock form the cutover produced.
# `bedrock:` is the one that matters: it is what the old gate withheld the tool from, and it is
# what the production review bot runs on today.
_ALL_PROVIDERS = (
    "anthropic:claude-opus-4-8",
    _BEDROCK_CLAUDE,
    "openai:gpt-4o",
    "google-gla:gemini-2.5-flash",
)


@pytest.mark.parametrize("model", _ALL_PROVIDERS)
def test_runner_attaches_web_capability_on_every_provider(monkeypatch, tmp_path, model):
    """THE regression test. RED on the `bedrock:` case (and on openai/google) the moment a
    provider-name gate is reinstated on the web path."""
    kwargs = _agent_kwargs(monkeypatch, tmp_path, model=model, web=True)
    from pydantic_ai.capabilities import WebSearch

    caps = kwargs.get("capabilities")
    assert caps is not None, f"web-flagged request on {model} got NO web capability"
    assert len(caps) == 1 and isinstance(caps[0], WebSearch)
    assert caps[0].get_native_tools() and caps[0].get_toolset() is not None


@pytest.mark.parametrize("model", _ALL_PROVIDERS)
def test_runner_unflagged_request_is_byte_identical(monkeypatch, tmp_path, model):
    """The no-request path must not move: an unflagged criterion sends exactly what it sent
    before web access existed — the ``capabilities`` key is OMITTED, never present-but-empty."""
    flagged = _agent_kwargs(monkeypatch, tmp_path, model=model, web=True)
    unflagged = _agent_kwargs(monkeypatch, tmp_path, model=model, web=False)
    assert "capabilities" not in unflagged
    assert web_search_capabilities(web=False) is None
    # The ONLY delta the flag introduces is the capabilities entry.
    assert unflagged == {k: v for k, v in flagged.items() if k != "capabilities"}


# ── routing: the packaged T1 entry declares web; pass1 threads it per criterion ─


def _packaged_routing() -> dict:
    text = resources.files("rebar.llm").joinpath("plan_review/criteria_routing.json").read_text()
    return json.loads(text)


def test_packaged_routing_declares_web_on_t1_only():
    routing = _packaged_routing()
    assert routing["T1"].get("web") is True
    assert [cid for cid, e in routing.items() if e.get("web")] == ["T1"]


class _CaptureRunner:
    """Runner double that records each RunRequest and returns an empty findings set."""

    name = "capture"

    def __init__(self):
        self.reqs: list[RunRequest] = []

    def preflight(self) -> None:  # pragma: no cover — protocol completeness
        pass

    def run(self, req: RunRequest) -> dict:
        self.reqs.append(req)
        return {"findings": [], "_usage": {}}


def test_pass1_chunk_threads_web_from_criterion_routing():
    from rebar.llm.plan_review import passes, registry

    crits = {c["id"]: c for c in registry.load_criteria(repo_root=None)}
    assert crits["T1"].get("web") is True  # descriptor carries the routing key
    cfg = LLMConfig(model="claude-opus-4-8", repo_path=".")
    r = _CaptureRunner()
    passes.pass1_chunk(r, cfg, plan="a plan", chunk=[crits["T1"]], agentic=True)
    assert r.reqs[-1].web is True
    # Another AGENT-tier criterion without the routing key never carries the flag.
    other = crits["G1G2"]
    assert not other.get("web")
    passes.pass1_chunk(r, cfg, plan="a plan", chunk=[other], agentic=True)
    assert r.reqs[-1].web is False
    # A single-turn chunk containing T1 (defensive: web rides only the agent tier).
    passes.pass1_chunk(r, cfg, plan="a plan", chunk=[crits["T1"]], agentic=False)
    assert r.reqs[-1].web is False


# ── rubric: T1 asserts web access, and still forbids fabricated citations ──────


def _t1_rubric() -> str:
    return resources.files("rebar.llm").joinpath("reviewers/plan_review_T1.md").read_text().lower()


def test_t1_rubric_asserts_web_access_rather_than_hedging_it():
    """Under the operator decision web access is a GUARANTEE for T1, so the rubric must not
    describe absence as an expected state — a reviewer told the tool "may" be offered will
    rationalise not using it."""
    text = _t1_rubric()
    assert "web search" in text or "web-search" in text
    for hedge in ("may be offered", "provider-dependent", "when it is not available", "fall back"):
        assert hedge not in text, f"T1 still frames web access as optional: {hedge!r}"


def test_t1_rubric_still_forbids_fabricated_citations_and_frames_results_as_untrusted():
    """The clause that guarded against invented sources must survive the reframing — and since
    the local route pulls third-party text into OUR process, the rubric must also say that text
    is data, not instruction."""
    text = _t1_rubric()
    assert "fabricate" in text
    assert "untrusted" in text
