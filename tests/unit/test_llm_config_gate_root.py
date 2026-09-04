"""The LLM config table is discovered from the GATE's pinned code root, not from the ambient
cwd (bug 2876).

`gate_source.gate_read_root` promises that activating a snapshot means "every config rebuilt
deep in the gate reads each" root. `LLMConfig.from_env` honours that for `repo_path`, but the
`[tool.rebar.llm]` TABLE itself was read through `_config_sources.repo_root(None)` — i.e.
`REBAR_ROOT`, else the git toplevel of the process cwd. When ambient discovery missed the
project's config, `parse_class_slots({})` silently returned the BUILT-IN defaults, which are
bare Anthropic ids, so a Bedrock-configured project issued direct-Anthropic calls and 401'd.

These are end-to-end over the REAL discovery path — a real `rebar.toml` on disk, read through
`load_class_slots`/`resolve_model_string` and through the runner's chain lookup. The existing
`test_llm_fallback_chain.py` monkeypatches `_read_llm_file_table`, which is exactly why the
whole suite was blind to this: it stubs out the layer that was broken.
"""

from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.models

from rebar.llm.config import LLMConfig
from rebar.llm.gate_context import use_code_root
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit

#: The primary the project config names, and the arm it falls back to. Distinct providers on
#: purpose: the bug's whole signature is a Bedrock-configured class serving direct Anthropic.
_BEDROCK_PRIMARY = "bedrock:us.anthropic.claude-sonnet-4-6"
_ANTHROPIC_PRIMARY = "claude-sonnet-4-6"
_ANTHROPIC_FALLBACK = "claude-opus-4-8"

_PROJECT_TOML = """
[llm]
model = "{primary}"

[llm.model_classes]
standard = {{ model = "{primary}", fallback = [
  {{ model = "{fallback}", provider = "anthropic" }},
] }}
"""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Neither the operator's ambient roots nor their per-class overrides may reach these
    tests: both are higher-precedence layers that would mask what is under test."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    for name in ("REBAR_ROOT", "REBAR_CONFIG", "REBAR_LLM_CONFIG_FILE", "REBAR_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for name in ("FRONTIER", "STANDARD", "TRIVIAL"):
        for field in ("MODEL", "PROVIDER", "ENDPOINT"):
            monkeypatch.delenv(f"REBAR_LLM_{name}_{field}", raising=False)


def _project(tmp_path, *, primary: str, fallback: str = _ANTHROPIC_FALLBACK):
    """A real project tree carrying a real `rebar.toml`, with a `.git` marker so config
    discovery treats it as the repo boundary exactly as it would a checkout."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "rebar.toml").write_text(_PROJECT_TOML.format(primary=primary, fallback=fallback))
    return root


def _transport_http_module():
    from anthropic import AsyncAnthropic

    from rebar.llm.anthropic_model import _anthropic_http_client_module

    return _anthropic_http_client_module(AsyncAnthropic)


@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    """Run with the process cwd OUTSIDE any rebar project — the state a gate reaches whenever
    it is driven from an agent harness, an MCP server, or any directory that is not the
    checkout. Ambient discovery must find nothing, so the only root left is the gate's."""
    away = tmp_path / "away"
    away.mkdir()
    (away / ".git").mkdir()  # a repo boundary that is NOT a rebar project
    monkeypatch.chdir(away)
    return away


# ── the resolution half ───────────────────────────────────────────────────────────────────


def test_a_class_resolves_through_the_active_gate_code_root(tmp_path, elsewhere):
    """The reported defect, at its narrowest. Inside an active gate snapshot, a class resolved
    WITHOUT a threaded root must still read the snapshot's config. Before the fix this returned
    the built-in default `anthropic:claude-sonnet-4-6` — byte-for-byte the model id in the 401
    the ticket reports."""
    from rebar.llm.model_classes import resolve_model_string

    root = _project(tmp_path, primary=_BEDROCK_PRIMARY)

    with use_code_root(str(root)):
        assert resolve_model_string("standard") == _BEDROCK_PRIMARY


def test_the_gate_root_does_not_override_an_explicitly_threaded_root(tmp_path, elsewhere):
    """The cascade's order, not merely its effect: an explicit argument is a caller override and
    must still outrank the active snapshot, or threading a root would become meaningless."""
    from rebar.llm.model_classes import resolve_model_string

    snapshot = _project(tmp_path, primary=_BEDROCK_PRIMARY)
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").mkdir()
    (other / "rebar.toml").write_text(
        _PROJECT_TOML.format(
            primary=f"anthropic:{_ANTHROPIC_PRIMARY}", fallback=_ANTHROPIC_FALLBACK
        )
    )

    with use_code_root(str(snapshot)):
        assert resolve_model_string("standard", str(other)) == f"anthropic:{_ANTHROPIC_PRIMARY}"


def test_without_a_gate_the_ambient_root_is_still_the_answer(tmp_path, monkeypatch):
    """The no-regression half: outside a gate there is no snapshot, so discovery must keep
    walking up from the cwd exactly as it does today."""
    from rebar.llm.model_classes import resolve_model_string

    root = _project(tmp_path, primary=_BEDROCK_PRIMARY)
    monkeypatch.chdir(root)

    assert resolve_model_string("standard") == _BEDROCK_PRIMARY


def test_the_whole_llm_config_reads_the_gate_root_not_only_the_classes(tmp_path, elsewhere):
    """`cfg.model` is a SECOND resolution path the class table cannot reach (rebar.toml documents
    it as load-bearing). It degraded to the bare `DEFAULT_MODEL` under the same mechanism, so
    fixing only the class table would leave the scalar leaking to direct Anthropic."""
    root = _project(tmp_path, primary=_BEDROCK_PRIMARY)

    with use_code_root(str(root)):
        assert LLMConfig.from_env().model == _BEDROCK_PRIMARY


# ── the end-to-end half: the fallback must ANSWER, not merely be attempted ─────────────────


@pytest.fixture
def socket(monkeypatch):
    """Real construction, mocked socket. `status` decides how each candidate answers and `seen`
    records which ids were actually requested, so "the fallback answered" is observed on the
    wire rather than inferred from configuration."""
    status: dict[str, int] = {}
    seen: list[str] = []
    transport_http = _transport_http_module()

    def _handler(request) -> object:
        name = json.loads(request.content).get("model", "")
        seen.append(name)
        code = status.get(name, 200)
        if code != 200:
            return transport_http.Response(
                code, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}}
            )
        return transport_http.Response(
            200,
            json={
                "id": "msg_x",
                "type": "message",
                "role": "assistant",
                "model": name,
                "content": [{"type": "text", "text": f"answered-by-{name}"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    monkeypatch.setattr(
        transport_http,
        "AsyncHTTPTransport",
        lambda *a, **kw: transport_http.MockTransport(_handler),
    )
    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", True)
    return {"status": status, "seen": seen}


def _gate_cfg(root):
    """The config a gate builds for itself: nothing threaded, everything discovered. One
    attempt, so the transport's own tenacity retry cannot re-send to the primary several times
    before the chain is consulted."""
    from dataclasses import replace

    return replace(LLMConfig.from_env(), llm_retry_max_attempts=1)


def _answer(result) -> str:
    """The text the caller actually received."""
    return str(result["text"])


def _run(cfg):
    return PydanticAIRunner(cfg).run(
        RunRequest(
            system_prompt="sys",
            instructions="go",
            config=cfg,
            mode="text",
            execution_mode="single_turn",
        )
    )


def test_a_gate_rooted_chain_actually_serves_the_fallbacks_answer(tmp_path, elsewhere, socket):
    """The acceptance criterion the ticket asks for: not "a fallback was attempted" but "the
    fallback ANSWERED and its answer is what the caller got".

    A test that only asserted the attempt would have passed throughout the reported failure —
    the arm WAS attempted, it just 401'd. So this asserts the returned TEXT is the fallback's,
    and that the attestation names the fallback rather than the primary.

    Before the fix the chain was invisible: `fallback_targets_for` looked the class up through
    the ambient root, found no config, built an unwrapped primary, and the 529 propagated.
    """
    root = _project(tmp_path, primary=f"anthropic:{_ANTHROPIC_PRIMARY}")
    socket["status"][_ANTHROPIC_PRIMARY] = 529  # overloaded: an availability failure

    with use_code_root(str(root)):
        result = _run(_gate_cfg(root))

    assert socket["seen"] == [_ANTHROPIC_PRIMARY, _ANTHROPIC_FALLBACK], (
        "the chain never failed over"
    )
    assert _answer(result) == f"answered-by-{_ANTHROPIC_FALLBACK}", (
        "the caller did not receive the fallback's answer"
    )
    ran = result["provider_provenance"]["ran_model"]
    assert ran.endswith(_ANTHROPIC_FALLBACK) and not ran.endswith(_ANTHROPIC_PRIMARY)


def test_the_gate_rooted_primary_answers_when_it_is_healthy(tmp_path, elsewhere, socket):
    """The differential partner: same config, healthy primary. Without this, a fix that routed
    EVERYTHING to the fallback would pass the test above."""
    root = _project(tmp_path, primary=f"anthropic:{_ANTHROPIC_PRIMARY}")

    with use_code_root(str(root)):
        result = _run(_gate_cfg(root))

    assert socket["seen"] == [_ANTHROPIC_PRIMARY], "a healthy primary must not fail over"
    assert _answer(result) == f"answered-by-{_ANTHROPIC_PRIMARY}"


def test_os_getcwd_is_never_consulted_for_a_gate_rooted_class(tmp_path, elsewhere):
    """A regression guard with teeth: the cwd here IS a git repo (so discovery would happily
    stop there) but carries no rebar.toml. If resolution ever falls back to it again, the class
    silently becomes the bare-Anthropic default again."""
    from rebar.llm.model_classes import load_class_slots

    root = _project(tmp_path, primary=_BEDROCK_PRIMARY)
    assert not (elsewhere / "rebar.toml").exists()
    assert os.getcwd() == str(elsewhere)

    with use_code_root(str(root)):
        slots = load_class_slots()

    assert slots["standard"].model == _BEDROCK_PRIMARY
    assert [f.model for f in slots["standard"].fallback] == [_ANTHROPIC_FALLBACK]
