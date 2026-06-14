"""Tests for the rebar.llm agent-operations framework + the review_ticket op.

All offline: the agent run is exercised through a FakeRunner (the dependency-
injection seam), so no model, network, or `agents` extra is needed. The live
langgraph/langflow paths are tested only for their graceful-degradation errors.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar import schemas


# ── import-cleanliness (the hard optionality rule) ────────────────────────────
def test_import_rebar_llm_pulls_no_heavy_deps() -> None:
    """`import rebar.llm` must not import langchain/langfuse/anthropic/pydantic —
    they are lazy. Run in a clean subprocess so import order can't mask it."""
    code = (
        "import sys, rebar.llm;"
        "heavy=[m for m in "
        "('langchain','langgraph','langchain_anthropic','langchain_mcp_adapters',"
        "'langfuse','anthropic','pydantic') if m in sys.modules];"
        "print('HEAVY' if heavy else 'CLEAN', heavy)"
    )
    cp = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
        env={"PYTHONPATH": "src", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.startswith("CLEAN"), cp.stdout


# ── reviewer registry + prompt resolution (offline fallback) ──────────────────
def test_catalog_and_fallback_prompt_render() -> None:
    import rebar.llm as llm
    from rebar.llm import prompts

    catalog = llm.load_catalog()
    assert "ticket-quality" in catalog
    assert catalog["ticket-quality"].default is True
    rv = llm.get_reviewer("ticket-quality")
    text, obj = prompts.resolve_prompt(
        rv, {"ticket_id": "T1", "ticket_context": "CTX", "repo_path": "/x"}, None
    )
    assert "T1" in text and "CTX" in text
    assert obj is None  # no Langfuse → packaged fallback, no prompt object


def test_unknown_reviewer_raises() -> None:
    import rebar.llm as llm
    from rebar.llm.prompts import ReviewerError

    with pytest.raises(ReviewerError):
        llm.get_reviewer("does-not-exist")


# ── deterministic reviewer selection (the rules layer) ────────────────────────
@pytest.mark.parametrize(
    "changed, expected",
    [
        ([], {"ticket-quality"}),
        (["src/rebar/auth_helpers.py"], {"ticket-quality", "security"}),
        (["src/rebar/signing.py"], {"ticket-quality", "security"}),
        (["tests/test_x.py"], {"ticket-quality", "tests"}),
        (["src/rebar/auth.py", "tests/test_auth.py"], {"ticket-quality", "security", "tests"}),
        (["README.md"], {"ticket-quality"}),
    ],
)
def test_select_reviewers_rules(changed, expected) -> None:
    import rebar.llm as llm

    assert set(llm.select_reviewers(changed)) == expected


# ── findings normalization / citation resolution / validation ─────────────────
def test_normalize_coerces_shape() -> None:
    from rebar.llm.findings import normalize_finding

    f = normalize_finding({"severity": "BOGUS", "category": "x", "description": "d"})
    assert f["severity"] == "info"  # unknown clamps to info
    assert f["dimension"] == "x" and f["detail"] == "d"
    assert f["citations"] == []


def test_resolve_citations_downgrades_unresolved(tmp_path: Path) -> None:
    from rebar.llm.findings import build_result, resolve_citations

    (tmp_path / "real.py").write_text("a\nb\nc\n", encoding="utf-8")
    result = build_result(
        [{
            "severity": "high", "dimension": "d", "detail": "x",
            "citations": [
                {"kind": "file", "path": "real.py", "line_start": 1, "line_end": 2},
                {"kind": "file", "path": "real.py", "line_start": 99},   # out of range
                {"kind": "file", "path": "missing.py", "line_start": 1},  # no such file
                "freeform",
            ],
        }],
        runner="fake",
    )
    resolve_citations(result, str(tmp_path))
    kinds = [c["kind"] for c in result["findings"][0]["citations"]]
    # valid file kept; out-of-range + missing downgraded to source; freeform = source
    assert kinds == ["file", "source", "source", "source"]


def test_resolve_citations_rejects_denied_state_paths(tmp_path: Path) -> None:
    """A citation into .git/.tickets-tracker/.bridge_state must be downgraded — the
    file-tool sandbox guarantee has to hold in the OUTPUT too (PR #6 review)."""
    from rebar.llm.findings import build_result, resolve_citations

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / ".bridge_state").mkdir()
    (tmp_path / ".bridge_state" / "map.json").write_text("{}\n", encoding="utf-8")
    result = build_result(
        [{"severity": "high", "dimension": "d", "detail": "x", "citations": [
            {"kind": "file", "path": ".git/config", "line_start": 1},
            {"kind": "file", "path": ".bridge_state/map.json", "line_start": 1},
        ]}],
        runner="fake",
    )
    resolve_citations(result, str(tmp_path))
    kinds = [c["kind"] for c in result["findings"][0]["citations"]]
    assert kinds == ["source", "source"]  # both denied -> downgraded


def test_read_file_tool_caps_without_slurping(tmp_path: Path) -> None:
    """read_file streams and caps at _READ_MAX_LINES, and tells the agent how to
    page (PR #6 review + windowing research)."""
    pytest.importorskip("langchain_core")
    from rebar.llm.runner import _READ_MAX_LINES, _filesystem_tools

    big = tmp_path / "big.txt"
    big.write_text("".join(f"line {i}\n" for i in range(1, _READ_MAX_LINES + 501)),
                   encoding="utf-8")
    read_file = {t.name: t for t in _filesystem_tools(str(tmp_path))}["read_file"]
    out = read_file.invoke({"path": "big.txt"})
    assert "truncated" in out
    assert f"line_start={_READ_MAX_LINES + 1}" in out  # paging guidance for next window
    assert out.count("\n") <= _READ_MAX_LINES + 1  # capped, not the full file
    # a narrow range returns exactly that window, no truncation note
    narrow = read_file.invoke({"path": "big.txt", "line_start": 5, "line_end": 7})
    assert "truncated" not in narrow and narrow.startswith("5: line 5")
    assert narrow.strip().endswith("7: line 7")


def test_read_file_truncates_overlong_lines(tmp_path: Path) -> None:
    pytest.importorskip("langchain_core")
    from rebar.llm.runner import _READ_MAX_LINE_CHARS, _filesystem_tools

    (tmp_path / "min.js").write_text("x" * (_READ_MAX_LINE_CHARS + 4000) + "\n",
                                     encoding="utf-8")
    read_file = {t.name: t for t in _filesystem_tools(str(tmp_path))}["read_file"]
    out = read_file.invoke({"path": "min.js"})
    assert "chars truncated" in out
    assert len(out) < _READ_MAX_LINE_CHARS + 500  # the 4000-char tail was clipped


def test_discovery_hides_noise_and_gitignored(rebar_repo: Path) -> None:
    """list_directory/search_files hide vendored/generated + .gitignore'd files, but
    read_file can still access an explicitly named one (large-project handling)."""
    pytest.importorskip("langchain_core")
    from rebar.llm.runner import _filesystem_tools

    (rebar_repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    (rebar_repo / "secret.txt").write_text("TOKEN=abc\n", encoding="utf-8")
    (rebar_repo / "visible.py").write_text("TOKEN_marker = 1\n", encoding="utf-8")
    (rebar_repo / "node_modules").mkdir()
    (rebar_repo / "node_modules" / "dep.js").write_text("TOKEN_marker\n", encoding="utf-8")

    tools = {t.name: t for t in _filesystem_tools(str(rebar_repo))}
    listing = tools["list_directory"].invoke({"path": "."})
    assert "visible.py" in listing
    assert "secret.txt" not in listing and "node_modules" not in listing
    # search skips the gitignored file and the vendored dir, finds the tracked one
    found = tools["search_files"].invoke({"pattern": "TOKEN_marker"})
    assert "visible.py" in found
    assert "secret.txt" not in found and "node_modules" not in found
    # but an explicitly named ignored file is still readable (not a security deny)
    assert "TOKEN=abc" in tools["read_file"].invoke({"path": "secret.txt"})


def test_validate_rejects_bad_result() -> None:
    pytest.importorskip("jsonschema")
    from rebar.llm.findings import FindingsError, validate_result

    with pytest.raises(FindingsError):
        validate_result({"findings": [{"severity": "nope", "dimension": "d", "detail": "x"}]})


def test_pydantic_mirror_field_sets_match_schema() -> None:
    """Pin the Pydantic structured-output model to the JSON Schema $defs so the two
    can't drift (the schema is the source of truth)."""
    pytest.importorskip("pydantic")
    model = findings_response_model = __import__(
        "rebar.llm.findings", fromlist=["findings_response_model"]
    ).findings_response_model
    Review = model()
    Finding = Review.model_fields["findings"].annotation.__args__[0]
    Citation = Finding.model_fields["citations"].annotation.__args__[0]

    common = schemas.load("common")["$defs"]
    assert set(Finding.model_fields) == set(common["finding"]["properties"]), (
        "Pydantic Finding fields drifted from common.schema.json finding $def"
    )
    assert set(Citation.model_fields) == set(common["citation"]["properties"]), (
        "Pydantic Citation fields drifted from common.schema.json citation $def"
    )


def test_normalize_clamps_soft_fields() -> None:
    from rebar.llm.findings import normalize_finding

    f = normalize_finding({
        "severity": "high", "dimension": "d", "detail": "x", "confidence": 2.5,
        "citations": [{"kind": "file", "path": "a.py", "line_start": -3}],
    })
    assert f["confidence"] == 1.0  # clamped into [0,1]
    assert "line_start" not in f["citations"][0]  # negative line dropped


def test_framework_errors_are_llmerror() -> None:
    """H1: the expected failure modes are catchable as one LLMError vocabulary."""
    import rebar.llm as llm
    from rebar.llm.findings import FindingsError
    from rebar.llm.prompts import ReviewerError

    assert issubclass(FindingsError, llm.LLMError)
    assert issubclass(ReviewerError, llm.LLMError)


# ── config + runner selection ─────────────────────────────────────────────────
def test_infer_provider() -> None:
    from rebar.llm.config import infer_provider

    assert infer_provider("claude-opus-4-8") == "anthropic"
    assert infer_provider("gpt-4o") == "openai"
    assert infer_provider("chatgpt-4o-latest") == "openai"
    assert infer_provider("gemini-2.5-pro") == "google_genai"
    assert infer_provider("openai:gpt-4o") == "openai"  # provider:model form
    assert infer_provider("local-model", explicit="openai") == "openai"
    assert infer_provider("mystery-model") is None


def test_build_model_wiring_is_provider_agnostic() -> None:
    """_build_model must pass model/provider/base_url/api_key straight through to
    init_chat_model and never inject temperature (claude-opus-4.x reject it)."""
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import _build_model

    captured: dict = {}

    def fake_init(model, model_provider=None, **kw):
        captured.update(model=model, provider=model_provider, kw=kw)
        return object()

    _build_model(
        LLMConfig(model="gpt-4o", model_provider="openai", base_url="http://h/v1",
                  api_key="k", max_tokens=123, timeout_s=7),
        fake_init,
    )
    assert captured["model"] == "gpt-4o" and captured["provider"] == "openai"
    assert captured["kw"]["base_url"] == "http://h/v1" and captured["kw"]["api_key"] == "k"
    assert captured["kw"]["max_tokens"] == 123 and captured["kw"]["timeout"] == 7
    assert "temperature" not in captured["kw"]


def test_build_model_constructs_claude_and_chatgpt() -> None:
    """Validate the real multi-provider path: Claude -> ChatAnthropic, ChatGPT ->
    ChatOpenAI (construction only; no API call). Skips when the libs are absent."""
    pytest.importorskip("langchain")
    pytest.importorskip("langchain_anthropic")
    pytest.importorskip("langchain_openai")
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import _build_model, _import_langgraph

    _, _, init_chat_model = _import_langgraph()
    claude = _build_model(LLMConfig(model="claude-opus-4-8", api_key="test"), init_chat_model)
    assert type(claude).__name__ == "ChatAnthropic"
    assert getattr(claude, "temperature", None) is None  # never sent
    gpt = _build_model(
        LLMConfig(model="gpt-4o", model_provider="openai", api_key="test"), init_chat_model
    )
    assert type(gpt).__name__ == "ChatOpenAI"
    # OpenAI-compatible local server (LMStudio/Ollama/vLLM) via base_url.
    local = _build_model(
        LLMConfig(model="m", model_provider="openai", api_key="x", base_url="http://h/v1"),
        init_chat_model,
    )
    assert type(local).__name__ == "ChatOpenAI"


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar.llm.config import LLMConfig

    monkeypatch.setenv("REBAR_LLM_RUNNER", "fake")
    monkeypatch.setenv("REBAR_LLM_MODEL", "gpt-4o")
    monkeypatch.setenv("REBAR_LLM_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("REBAR_LLM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("REBAR_LLM_MAX_ITERS", "7")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    cfg = LLMConfig.from_env(repo_root=".")
    assert cfg.runner == "fake" and cfg.model == "gpt-4o" and cfg.max_iterations == 7
    assert cfg.model_provider == "openai" and cfg.base_url == "http://localhost:1234/v1"
    assert cfg.langfuse.enabled is True


def test_runner_selection_and_stubs() -> None:
    from rebar.llm.config import LLMConfig
    from rebar.llm.errors import LLMConfigError
    from rebar.llm.runner import (
        DeepAgentsRunner, FakeRunner, LangflowRunner, LangGraphRunner, RunRequest,
        get_runner,
    )

    assert isinstance(get_runner(LLMConfig(runner="fake")), FakeRunner)
    assert isinstance(get_runner(LLMConfig(runner="langflow")), LangflowRunner)
    assert isinstance(get_runner(LLMConfig(runner="langgraph")), LangGraphRunner)
    assert isinstance(get_runner(LLMConfig(runner="deepagents")), DeepAgentsRunner)
    # The DEFAULT (review) runner is langgraph, NOT deepagents.
    assert isinstance(get_runner(LLMConfig()), LangGraphRunner)
    fake = FakeRunner(findings=[{"severity": "low", "dimension": "d", "detail": "x"}])
    assert isinstance(get_runner(LLMConfig(runner="langgraph"), override=fake), FakeRunner)

    req = RunRequest(system_prompt="s", instructions="i", config=LLMConfig(repo_path="."))
    # Langflow runner is a documented stub.
    with pytest.raises(NotImplementedError):
        LangflowRunner(LLMConfig()).run(req)
    # LangGraph runner without the 'agents' extra (langchain) gives a clear install
    # error. Guard on langchain's actual absence — when it IS installed, running
    # needs real credentials, which this offline test does not exercise.
    from rebar.llm.config import _module_available

    if not _module_available("langchain"):
        with pytest.raises(LLMConfigError):
            LangGraphRunner(LLMConfig(repo_path=".")).run(req)
    # deepagents runner without the extra installed gives the same clear error.
    if not _module_available("deepagents"):
        with pytest.raises(LLMConfigError):
            DeepAgentsRunner(LLMConfig(repo_path=".")).run(req)


def test_deepagents_runner_assembles(tmp_path: Path) -> None:
    """The opt-in deepagents runner wires a read-only, repo-rooted deep agent with
    our findings schema (construction only; no model call). Skips without the lib."""
    pytest.importorskip("deepagents")
    pytest.importorskip("langchain_anthropic")
    from rebar.llm import findings as F
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import _build_model, _import_deepagents, _import_langgraph

    _, ToolStrategy, init_chat_model = _import_langgraph()
    create_deep_agent, FilesystemBackend, FilesystemPermission = _import_deepagents()
    model = _build_model(
        LLMConfig(model="claude-opus-4-8", api_key="test", repo_path=str(tmp_path)),
        init_chat_model,
    )
    agent = create_deep_agent(
        model=model,
        tools=[],
        system_prompt="review",
        backend=FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True),
        permissions=[FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")],
        response_format=ToolStrategy(F.findings_response_model(), handle_errors=True),
    )
    assert agent is not None


# ── review_ticket end-to-end (FakeRunner against a real store) ────────────────
def _seed(repo: Path) -> str:
    r = str(repo)
    epic = rebar.create_ticket("epic", "Login epic", repo_root=r)
    rebar.create_ticket(
        "task", "Add auth",
        description="Body.\n\n## Acceptance Criteria\n- [ ] login works",
        parent=epic, repo_root=r,
    )
    return epic


def test_review_ticket_end_to_end(rebar_repo: Path) -> None:
    import rebar.llm as llm

    epic = _seed(rebar_repo)
    (rebar_repo / "app.py").write_text("import os\nKEY='x'\n", encoding="utf-8")
    runner = llm.FakeRunner(
        findings=[{
            "severity": "high", "dimension": "security",
            "detail": "hardcoded secret",
            "citations": [{"kind": "file", "path": "app.py", "line_start": 2, "line_end": 2}],
        }],
        summary="one issue",
    )
    result = llm.review_ticket(epic, "ticket-quality", repo_root=str(rebar_repo), runner=runner)
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["runner"] == "fake"
    assert result["reviewers"] == ["ticket-quality"]
    assert result["target"]["kind"] == "ticket"
    assert result["findings"][0]["citations"][0]["kind"] == "file"  # real file kept


def test_review_ticket_graph_includes_children(rebar_repo: Path) -> None:
    import rebar.llm as llm

    epic = _seed(rebar_repo)
    runner = llm.FakeRunner(findings=[])
    result = llm.review_ticket(epic, repo_root=str(rebar_repo), graph=True, runner=runner)
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["target"]["kind"] == "ticket_graph"
    assert len(result["target"]["ticket_ids"]) >= 2  # epic + its task


def test_review_ticket_unknown_reviewer_is_llmerror(rebar_repo: Path) -> None:
    import rebar.llm as llm

    epic = _seed(rebar_repo)
    with pytest.raises(llm.LLMError):
        llm.review_ticket(epic, "no-such-reviewer", repo_root=str(rebar_repo),
                          runner=llm.FakeRunner())


# ── CLI surface ───────────────────────────────────────────────────────────────
def test_cli_review_check(capsys: pytest.CaptureFixture) -> None:
    from rebar._cli import main

    rc = main(["review", "--check"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert "langchain" in data and "anthropic_api_key" in data


def test_cli_review_with_fake_runner(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture) -> None:
    epic = _seed(rebar_repo)
    monkeypatch.setenv("REBAR_LLM_RUNNER", "fake")  # offline runner, valid empty review
    from rebar._cli import main

    rc = main(["review", epic, "--output", "json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    result = json.loads(out)
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["runner"] == "fake" and result["findings"] == []


def test_cli_review_bad_reviewer_is_graceful(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture) -> None:
    epic = _seed(rebar_repo)
    monkeypatch.setenv("REBAR_LLM_RUNNER", "fake")
    from rebar._cli import main

    rc = main(["review", epic, "no-such-reviewer"])
    err = capsys.readouterr().err
    assert rc == 1 and "Error:" in err  # clean error, not a traceback


# ── MCP surface ───────────────────────────────────────────────────────────────
def test_mcp_review_tool_registered_and_gated(rebar_repo: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from adapters import _unwrap  # tests/interfaces on sys.path
    from rebar.mcp_server import build_server

    srv = build_server()
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    assert "review_ticket" in tools
    # plain-dict return → no advertised outputSchema (NO_SCHEMA_EXEMPT contract)
    assert not tools["review_ticket"].outputSchema

    epic = _seed(rebar_repo)
    # Disabled by default (no REBAR_MCP_ALLOW_LLM) → tool error.
    monkeypatch.delenv("REBAR_MCP_ALLOW_LLM", raising=False)
    with pytest.raises(Exception):
        _unwrap(asyncio.run(srv.call_tool("review_ticket", {"ticket_id": epic})))
