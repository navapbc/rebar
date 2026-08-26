"""ee8a (intimate-flirty-barb): the LLMConfig `headers` surface.

Operator-configured request headers for gate LLM calls, resolved across the three
existing layers (CLI `-c llm.headers=` > `REBAR_LLM_HEADERS` > `[tool.rebar.llm]`),
with a closed `${env:...}` / `${run:...}` value grammar and the validation that keeps
the surface from becoming an authentication or protocol-injection channel.

Unlike `mcp_servers` (which degrades malformed JSON to `{}` — see
`test_config_llm.py::test_mcp_servers_env_json_overrides_file`), every failure here is
loud: silently discarding headers would leave an operator with an unattributed gateway
and no signal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rebar import config as cfg
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REBAR_CONFIG",
        "XDG_CONFIG_HOME",
        "REBAR_ROOT",
        "REBAR_LLM_BASE_URL",
        "REBAR_LLM_API_KEY",
        "REBAR_LLM_HEADERS",
        "REBAR_LLM_REPO_PATH",
        "GATEWAY_TAG",
        "GATEWAY_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg.set_cli_overrides(None)


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


def _cfg(root: Path) -> LLMConfig:
    cfg.reset_config_cache()
    return LLMConfig.from_env(repo_root=root)


# ══════════════════════════ HAPPY PATH ══════════════════════════


def test_headers_default_empty(tmp_path: Path) -> None:
    """AC1: unconfigured deployments are byte-unchanged."""
    assert _cfg(_proj(tmp_path)).headers == {}


def test_literal_and_env_value_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: a literal header and an ${env:VAR} header both resolve."""
    p = _proj(tmp_path)
    monkeypatch.setenv("GATEWAY_TAG", "team-platform")
    monkeypatch.setenv(
        "REBAR_LLM_HEADERS",
        '{"x-corp-tag": "rebar", "x-corp-team": "${env:GATEWAY_TAG}"}',
    )
    assert _cfg(p).headers == {"x-corp-tag": "rebar", "x-corp-team": "team-platform"}


def test_three_layer_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: CLI > env > file."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text('[llm.headers]\n"x-src" = "from-file"\n', encoding="utf-8")
    assert _cfg(p).headers == {"x-src": "from-file"}
    monkeypatch.setenv("REBAR_LLM_HEADERS", '{"x-src": "from-env"}')
    assert _cfg(p).headers == {"x-src": "from-env"}
    # Nested shape (what `parse_cli_overrides` builds and every existing test uses), and
    # installed AFTER the cache reset: `reset_config_cache()` documents that it clears the
    # process-wide CLI overrides, so `_cfg()` would drop an override installed before it.
    cfg.reset_config_cache()
    cfg.set_cli_overrides({"llm": {"headers": '{"x-src": "from-cli"}'}})
    assert LLMConfig.from_env(repo_root=p).headers == {"x-src": "from-cli"}


# ══════════════════════════ HELD-OUT ORACLE ══════════════════════════


def test_unset_env_var_raises_naming_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4: unset ${env:VAR} is a hard error naming the variable."""
    p = _proj(tmp_path)
    monkeypatch.setenv("REBAR_LLM_HEADERS", '{"x-t": "${env:GATEWAY_TAG}"}')
    with pytest.raises(LLMConfigError, match="GATEWAY_TAG"):
        _cfg(p)


@pytest.mark.parametrize(
    "value",
    ["${foo:bar}", "${envVAR}", "${en:VAR}", "${env:VAR", "${run:bogus}"],
)
def test_grammar_dispositions_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """AC5: unrecognized namespace / missing colon / unclosed / unknown run key all raise."""
    p = _proj(tmp_path)
    monkeypatch.setenv("REBAR_LLM_HEADERS", f'{{"x-t": "{value}"}}')
    with pytest.raises(LLMConfigError):
        _cfg(p)


def test_run_placeholder_survives_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6: a well-formed ${run:...} is left for the delivery seam to resolve."""
    p = _proj(tmp_path)
    monkeypatch.setenv("REBAR_LLM_HEADERS", '{"x-trace": "${run:trace_id}"}')
    assert _cfg(p).headers["x-trace"] == "${run:trace_id}"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("$${VAR}", "${VAR}"), ("a$$b", "a$b")],
)
def test_dollar_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str, expected: str
) -> None:
    """AC7: `$$` is a literal `$` anywhere, per the OTel Collector rule."""
    p = _proj(tmp_path)
    monkeypatch.setenv("REBAR_LLM_HEADERS", f'{{"x-t": "{configured}"}}')
    assert _cfg(p).headers["x-t"] == expected


@pytest.mark.parametrize("ctrl", ["\r", "\n"])
def test_env_indirection_control_chars_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ctrl: str
) -> None:
    """AC8: validation runs on the RESOLVED value, so an env var carrying CR/LF/NUL
    cannot smuggle an injected header past the check that exists to stop it."""
    p = _proj(tmp_path)
    monkeypatch.setenv("GATEWAY_TAG", f"foo{ctrl}Injected: bar")
    monkeypatch.setenv("REBAR_LLM_HEADERS", '{"x-t": "${env:GATEWAY_TAG}"}')
    with pytest.raises(LLMConfigError):
        _cfg(p)


@pytest.mark.parametrize("ctrl", ["\\r", "\\n", "\\u0000"])
def test_literal_control_chars_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ctrl: str
) -> None:
    """AC8 (literal arm): CR/LF/NUL are rejected in a directly-configured value.

    NUL cannot reach the resolver through ${env:VAR} — the OS refuses an environment
    variable containing a null byte (`ValueError: embedded null byte`) — so the
    env-indirection arm above covers CR/LF only and this arm covers all three via the
    one path that can actually carry a NUL.
    """
    p = _proj(tmp_path)
    monkeypatch.setenv("REBAR_LLM_HEADERS", f'{{"x-t": "foo{ctrl}Injected: bar"}}')
    with pytest.raises(LLMConfigError):
        _cfg(p)


@pytest.mark.parametrize(
    "name",
    ["authorization", "x-api-key", "cookie", "host", "proxy-authorization"],
)
@pytest.mark.parametrize("case", [str.lower, str.upper, str.title])
def test_denied_names_case_insensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, case
) -> None:
    """AC9: the denied set is rejected regardless of casing."""
    p = _proj(tmp_path)
    monkeypatch.setenv("REBAR_LLM_HEADERS", f'{{"{case(name)}": "v"}}')
    with pytest.raises(LLMConfigError):
        _cfg(p)


def test_rejection_message_names_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC10: the denylist redirects to the first-class channel rather than only forbidding."""
    p = _proj(tmp_path)
    monkeypatch.setenv("REBAR_LLM_HEADERS", '{"authorization": "Bearer x"}')
    with pytest.raises(LLMConfigError, match=re.escape("llm.api_key")):
        _cfg(p)


def test_malformed_json_env_layer_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC11 (env): malformed JSON raises rather than degrading to {} as mcp_servers does."""
    p = _proj(tmp_path)
    monkeypatch.setenv("REBAR_LLM_HEADERS", '{"x-t": ')
    with pytest.raises(LLMConfigError):
        _cfg(p)


def test_malformed_json_cli_layer_raises(tmp_path: Path) -> None:
    """AC11 (CLI)."""
    p = _proj(tmp_path)
    cfg.reset_config_cache()
    cfg.set_cli_overrides({"llm": {"headers": '{"x-t": '}})
    with pytest.raises(LLMConfigError):
        LLMConfig.from_env(repo_root=p)


def test_malformed_json_file_layer_raises(tmp_path: Path) -> None:
    """AC11 (file): the file table accepts a JSON string; a malformed one raises."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text('[llm]\nheaders = "{\\"x-t\\": "\n', encoding="utf-8")
    with pytest.raises(LLMConfigError):
        _cfg(p)


@pytest.mark.parametrize("body", ["[llm]\nheaders = 7\n", '[llm]\nheaders = "[1,2]"\n'])
def test_file_table_bad_shape_raises(tmp_path: Path, body: str) -> None:
    """AC12: neither a table nor a string, or JSON parsing to a non-object."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(body, encoding="utf-8")
    with pytest.raises(LLMConfigError):
        _cfg(p)


def test_no_vendor_header_literal_in_shipped_code() -> None:
    """AC13: the portability guarantee is enforced, not merely asserted.

    Scans only HEADER-SHAPED literals. Bare `langfuse_` is deliberately excluded: it
    occurs ~42 times across ~10 files as rebar's own legitimate vocabulary
    (`langfuse_prompt`, `LANGFUSE_PUBLIC_KEY`), so scanning it would fail on day one.
    """
    root = Path(__file__).resolve().parents[2] / "src" / "rebar" / "llm"
    me = Path(__file__).resolve()
    offenders = []
    for path in root.rglob("*.py"):
        if path.resolve() == me:
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for lit in ("x-litellm-", "helicone-", "x-langfuse-"):
            if lit in text:
                offenders.append(f"{path}: {lit}")
    assert offenders == [], f"vendor header literals in shipped code: {offenders}"


def test_x_authorization_context_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC14 negative control: the denylist matches exact names, not substrings."""
    p = _proj(tmp_path)
    monkeypatch.setenv("REBAR_LLM_HEADERS", '{"x-authorization-context": "tenant-7"}')
    assert _cfg(p).headers == {"x-authorization-context": "tenant-7"}
