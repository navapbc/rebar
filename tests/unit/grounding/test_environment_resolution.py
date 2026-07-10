"""Environment-aware symbol resolution (bug 406f / succinct-formable-kite).

A plan/code-review reviewer's file tools — and the ctags repo-wide index the
grounding oracle's T1 lane builds — are REPO-SCOPED: a symbol that lives in a
third-party dependency under ``site-packages`` is structurally invisible, so the
reviewer wrongly asserts it "does not exist" and BLOCKs a valid plan/diff.

These tests pin the deterministic fix: :func:`rebar.grounding.resolve.refute_absence`
(via the oracle facade) now consults the INSTALLED environment through importlib,
so a symbol importable from an installed dependency resolves as ``refuted`` (its
asserted absence is disproved) instead of a not-found ``abstain``. An unresolvable
name still abstains — the lane stays confirm-only and never manufactures an absence.

The third-party symbol used is ``pydantic`` / ``pydantic.BaseModel`` — a HARD
runtime dependency of rebar's ``[agents]`` surface, so it genuinely lives in
site-packages of any environment that can run the LLM gate (asserted below).
"""

from __future__ import annotations

import importlib.util

import pytest

from rebar.grounding import evidence as ev
from rebar.grounding import oracle, resolve

# The fix leans on a REAL installed third-party dependency. Skip (rather than
# silently pass) if the environment somehow lacks it, so the assertions below
# always exercise the genuine site-packages path.
_HAVE_PYDANTIC = importlib.util.find_spec("pydantic") is not None
pytestmark = pytest.mark.skipif(
    not _HAVE_PYDANTIC, reason="pydantic must be installed to exercise the site-packages path"
)


def test_pydantic_is_a_site_packages_third_party_symbol() -> None:
    """Guard: the symbol under test really is an installed, non-repo dependency."""
    spec = importlib.util.find_spec("pydantic")
    assert spec is not None and spec.origin is not None
    assert "site-packages" in spec.origin  # genuinely third-party, not the rebar tree


def test_bare_third_party_import_is_refuted_not_absent(tmp_path) -> None:
    """A bare ``import pydantic`` reference resolves as EXISTING (refuted), even
    against an empty repo where the ctags index has zero definitions for it."""
    rec = oracle.refute_absence(
        {"kind": "import", "name": "pydantic", "language": "python"},
        repo_root=str(tmp_path),
    )
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["coverage"]["backend"] == resolve.BACKEND_ENV
    ev.validate(rec)  # every emitted record stays contract-valid


def test_third_party_member_is_refuted_not_absent(tmp_path) -> None:
    """A dotted ``pydantic.BaseModel`` member — which the T1 ctags lane abstains on
    (member binding is T2 territory) — is refuted via an actual import + getattr."""
    rec = oracle.refute_absence(
        {"kind": "member", "name": "pydantic.BaseModel", "language": "python"},
        repo_root=str(tmp_path),
    )
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["coverage"]["backend"] == resolve.BACKEND_ENV
    ev.validate(rec)


def test_container_plus_name_resolves() -> None:
    """A ``from pydantic import BaseModel`` shape (name + container) resolves."""
    loc = resolve.resolve_in_environment("BaseModel", container="pydantic", language="python")
    assert loc is not None
    assert loc["module"] == "pydantic" and loc["attr"] == "BaseModel"


def test_stdlib_symbol_is_refuted(tmp_path) -> None:
    """A stdlib module (``json``) also resolves in the environment — the repo index
    cannot see it either, so it must not read as absent."""
    rec = oracle.refute_absence(
        {"kind": "import", "name": "json", "language": "python"},
        repo_root=str(tmp_path),
    )
    assert rec["outcome"] == ev.OUTCOME_REFUTED


# -- No regression: genuinely-unresolvable references still abstain ------------


def test_unknown_module_still_abstains(tmp_path) -> None:
    """A name that resolves NOWHERE (repo index nor environment) still abstains —
    confirm-only: the lane never asserts an absence."""
    rec = oracle.refute_absence(
        {"kind": "import", "name": "zzz_definitely_not_a_module_406f", "language": "python"},
        repo_root=str(tmp_path),
    )
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] in ev.ABSTAIN_REASONS
    ev.validate(rec)


def test_missing_member_on_real_module_still_abstains(tmp_path) -> None:
    """A nonexistent attribute on a real installed module does NOT false-refute:
    the import succeeds but the getattr fails, so it stays an abstain."""
    rec = oracle.refute_absence(
        {"kind": "member", "name": "pydantic.NoSuchAttribute406f", "language": "python"},
        repo_root=str(tmp_path),
    )
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    ev.validate(rec)


def test_local_receiver_member_is_not_false_refuted() -> None:
    """``self.foo`` / ``cfg.value`` (a local receiver, not a module) must not
    resolve — the module segment isn't importable, so no false confirmation."""
    assert resolve.resolve_in_environment("self.foo", language="python") is None
    assert resolve.resolve_in_environment("cfg.some_attr", language="python") is None


def test_non_python_language_is_not_resolved() -> None:
    """The environment lane is Python-only; a declared other language yields None
    (never mis-resolving a same-named Python module for a Rust/Go reference)."""
    assert resolve.resolve_in_environment("pydantic", language="rust") is None


def test_injection_safe_name_is_rejected() -> None:
    """A non-identifier string is never handed to importlib as a module path."""
    assert resolve.resolve_in_environment("os; rm -rf /", language="python") is None
    assert resolve.resolve_in_environment("", language="python") is None
