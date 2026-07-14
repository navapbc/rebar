"""Tests for the CLI-reference generator (ticket e866).

The generator (scripts/gen_cli_reference.py) emits docs/cli-reference.md from the CLI's
own help data: the 50 help-backed subcommands (rebar._cli._help) with full usage text,
plus the 16 intercept-arm commands with curated one-liners whose key set is drift-gated
against the intercept ladder in rebar._cli.__init__.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN_PATH = REPO_ROOT / "scripts" / "gen_cli_reference.py"


def _load():
    spec = importlib.util.spec_from_file_location("gen_cli_reference", GEN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load()

_LADDER = {
    "config",
    "criteria",
    "enrich",
    "explain",
    "identity",  # epic gnu-whale-ichor: identity entity + self-identity + key add/revoke
    "jira-onboard",
    "llm",
    "prompt",
    "reconcile",
    "review",
    "review-code",
    "review-plan",
    "scan-spec",
    "sign-review",
    "verify-authorship",  # epic gnu-whale-ichor / AC7: back-compat alias for verify-identity
    "verify-commit-ticket",
    "verify-completion",
    "verify-identity",  # epic gnu-whale-ichor / AC7: authenticated-authorship merge-gate
    "verify-opcert",  # epic sonic-columned-sturgeon / 4214: required-environment op-cert merge-gate
    "trusted-env",  # story 4214: maintain .rebar/trusted_environments.yaml (add/revoke keys)
    "remote-cert",  # story ee0b: trusted op-cert gate service client
    "workflow",
}


# ─────────────────────────── HAPPY PATH (shown to implementer) ────────────────


def test_render_lists_every_help_backed_command():
    """Every known_subcommands() entry appears, backtick-wrapped, in the output."""
    from rebar._cli import _help

    doc = gen.render()
    for cmd in _help.known_subcommands():
        assert f"`{cmd}`" in doc, f"help-backed command {cmd!r} missing from reference"


def test_render_lists_every_intercept_command():
    """All 16 intercept-arm commands appear, backtick-wrapped, in the output."""
    doc = gen.render()
    for cmd in _LADDER:
        assert f"`{cmd}`" in doc, f"intercept command {cmd!r} missing from reference"


def test_check_mode_clean_against_committed_tree():
    """The committed docs/cli-reference.md matches the generator (exit 0)."""
    assert gen.main(["--check"]) == 0


# ─────────────────────────── EDGE CASES (HELD OUT) ────────────────────────────


def test_intercept_dict_matches_ladder():
    """The curated INTERCEPT_COMMANDS key set equals the ladder parsed from _cli."""
    assert set(gen.INTERCEPT_COMMANDS) == _LADDER
    assert set(gen.INTERCEPT_COMMANDS) == set(gen.ladder_intercepts())


def test_substring_commands_are_distinct(tmp_path: Path):
    """`review` and `review-plan` are BOTH present as distinct backtick tokens — the
    reference must not let one mask the other (backtick-wrapping makes them exact)."""
    doc = gen.render()
    assert "`review`" in doc
    assert "`review-plan`" in doc
    assert "`review-code`" in doc


def test_help_backed_usage_text_included():
    """A help-backed command's actual usage text (not just its name) is emitted."""
    doc = gen.render()
    # `create`'s pinned help begins with "Usage: rebar create"
    assert "Usage: rebar create" in doc


def test_parity_selfcheck_fails_on_intercept_drift(monkeypatch):
    """If INTERCEPT_COMMANDS drifts from the ladder, generation fails loudly (no silent
    incomplete doc). Removing a curated entry must raise."""
    broken = dict(gen.INTERCEPT_COMMANDS)
    broken.pop("workflow")
    monkeypatch.setattr(gen, "INTERCEPT_COMMANDS", broken)
    with pytest.raises((ValueError, AssertionError, RuntimeError)):
        gen.render()


def test_ladder_intercepts_parses_source():
    """ladder_intercepts() derives the 16 names from _cli/__init__.py source, not a
    hardcoded copy — so a new intercept arm is detected."""
    assert gen.ladder_intercepts() == _LADDER


# ─────────────────────────── E2E via main() / drift (HELD OUT) ────────────────


def test_check_mode_detects_stale_committed_doc(tmp_path: Path, monkeypatch):
    """main(--check) exits non-zero when the committed doc is stale vs the generator."""
    stale = tmp_path / "cli-reference.md"
    stale.write_text("# CLI reference\n\nstale, missing everything\n", encoding="utf-8")
    monkeypatch.setattr(gen, "DOC_PATH", stale, raising=False)
    assert gen.main(["--check"]) != 0


def test_generate_writes_full_doc(tmp_path: Path, monkeypatch):
    """Running the generator (no --check) writes a doc containing all commands."""
    out = tmp_path / "cli-reference.md"
    monkeypatch.setattr(gen, "DOC_PATH", out, raising=False)
    assert gen.main([]) == 0
    written = out.read_text()
    from rebar._cli import _help

    for cmd in list(_help.known_subcommands())[:5]:
        assert f"`{cmd}`" in written
    for cmd in _LADDER:
        assert f"`{cmd}`" in written
