"""The agentic `resolve_symbol` reviewer tool (bug 406f).

The finder's repo-scoped file tools cannot see a third-party dependency in
site-packages, so a library symbol reads as "not found" and gets wrongly flagged
hallucinated. `pai_tools.grounding_tools` exposes `resolve_symbol` so the agent can
CONFIRM a symbol in the installed environment before asserting non-existence. These
tests need no LLM / API key — they exercise the plain tool function directly, and use
PyYAML (``yaml``), a CORE runtime dependency present on every job.
"""

from __future__ import annotations

import os

from rebar.llm import fs_tools, pai_tools

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


def test_is_dependency_path_flags_install_and_venv_roots() -> None:
    sep = os.sep
    for part in ("site-packages", "dist-packages", ".venv", "venv"):
        p = sep.join(("", "repo", part, "pkg", "__init__.py"))
        assert fs_tools._is_dependency_path(p), p
    # First-party source has none of those components.
    assert not fs_tools._is_dependency_path(sep.join(("", "repo", "src", "rebar", "x.py")))


def test_resolve_symbol_repo_nested_venv_is_third_party(monkeypatch) -> None:
    """Regression: a dependency installed in a repo-LOCAL `.venv` lives under the
    repo root, but must still classify as third-party — not repo-local (in-tree
    .venv was misclassified as first-party because locality was root-containment
    only)."""
    root = os.path.realpath(".")
    origin = os.path.join(root, ".venv", "lib", "py", "site-packages", "acme", "__init__.py")

    from rebar.grounding import resolve as _resolve

    loc = {"module": "acme", "attr": None, "origin": origin}
    monkeypatch.setattr(
        _resolve,
        "resolve_in_environment",
        lambda name, container=None, language=None: loc,
    )
    out = _resolve_symbol()("acme")
    assert out.startswith("EXISTS")
    assert "third-party/stdlib" in out
    assert "repo-local" not in out


def test_resolve_symbol_first_party_src_is_repo_local(monkeypatch) -> None:
    """Genuine first-party source (under the root, NOT in a dependency root) still
    classifies as repo-local — the fix must not over-exclude."""
    root = os.path.realpath(".")
    origin = os.path.join(root, "src", "rebar", "grounding", "resolve.py")

    from rebar.grounding import resolve as _resolve

    loc = {"module": "rebar.grounding.resolve", "attr": None, "origin": origin}
    monkeypatch.setattr(
        _resolve,
        "resolve_in_environment",
        lambda name, container=None, language=None: loc,
    )
    out = _resolve_symbol()("rebar.grounding.resolve")
    assert out.startswith("EXISTS")
    assert "repo-local" in out
