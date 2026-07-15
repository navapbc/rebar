"""The agentic `resolve_symbol` reviewer tool (bug 406f).

The finder's repo-scoped file tools cannot see a third-party dependency in
site-packages, so a library symbol reads as "not found" and gets wrongly flagged
hallucinated. `pai_tools.grounding_tools` exposes `resolve_symbol` so the agent can
CONFIRM a symbol in the installed environment before asserting non-existence. These
tests need no LLM / API key — they exercise the plain tool function directly, and use
PyYAML (``yaml``), a CORE runtime dependency present on every job.
"""

from __future__ import annotations

from rebar.llm import pai_tools

_TP_PKG = "yaml"  # PyYAML: a core dependency, installed in every CI job.


def _resolve_symbol():
    (tool,) = pai_tools.grounding_tools(".")
    assert tool.__name__ == "resolve_symbol"
    return tool


def test_grounding_tools_offers_only_resolve_symbol() -> None:
    tools = pai_tools.grounding_tools(".")
    assert [t.__name__ for t in tools] == ["resolve_symbol"]


def test_resolve_symbol_confirms_third_party_module() -> None:
    out = _resolve_symbol()(_TP_PKG)
    assert out.startswith("EXISTS")
    assert "third-party/stdlib" in out


def test_resolve_symbol_confirms_member_via_module_arg() -> None:
    out = _resolve_symbol()("safe_load", module=_TP_PKG)
    assert out.startswith("EXISTS")
    assert "yaml.safe_load" in out


def test_resolve_symbol_unresolved_is_not_a_nonexistence_claim() -> None:
    out = _resolve_symbol()("zzz_no_such_symbol_406f")
    assert out.startswith("UNRESOLVED")
    assert "NOT proof" in out  # must actively discourage a hallucination finding


def test_resolve_symbol_never_raises_on_garbage() -> None:
    # A non-identifier is rejected safely (never handed to importlib as a path).
    out = _resolve_symbol()("os; rm -rf /")
    assert out.startswith("UNRESOLVED")


def test_resolve_symbol_venv_inside_repo_is_third_party(tmp_path, monkeypatch) -> None:
    """A module whose origin lives under the repo's OWN venv (repo_root/.venv/...)
    is third-party, not repo-local, even though the path sits inside the repo root
    (bug c810: the documented worktree setup puts .venv inside the checkout)."""
    import sys

    from rebar.grounding import resolve as _resolve

    venv = tmp_path / ".venv"
    origin = venv / "lib" / "python3.12" / "site-packages" / "yaml" / "__init__.py"
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(
        _resolve,
        "resolve_in_environment",
        lambda *a, **k: {"module": "yaml", "origin": str(origin)},
    )
    (tool,) = pai_tools.grounding_tools(str(tmp_path))
    out = tool("yaml")
    assert out.startswith("EXISTS")
    assert "third-party/stdlib" in out


def test_resolve_symbol_repo_module_still_repo_local(tmp_path, monkeypatch) -> None:
    """The venv exclusion must not overreach: a module whose origin lives in the
    repo's own source tree still classifies repo-local."""
    from rebar.grounding import resolve as _resolve

    origin = tmp_path / "src" / "mypkg" / "__init__.py"
    monkeypatch.setattr(
        _resolve,
        "resolve_in_environment",
        lambda *a, **k: {"module": "mypkg", "origin": str(origin)},
    )
    (tool,) = pai_tools.grounding_tools(str(tmp_path))
    out = tool("mypkg")
    assert out.startswith("EXISTS")
    assert "repo-local" in out
