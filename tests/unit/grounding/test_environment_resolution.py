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

Third-party symbol under test: ``yaml`` / ``yaml.safe_load`` (PyYAML) — a CORE
runtime dependency of rebar (``[project].dependencies``), so it is present in the
DEFAULT install and these third-party-path assertions run on EVERY CI job. The
``pydantic`` cases add coverage on the ``[agents]`` jobs where that dep is present;
each is guarded with ``pytest.importorskip`` so it SKIPS cleanly (never fails) on the
default suite, which does not install ``[agents]``. The stdlib and negative cases
never import a third-party dep and always run.
"""

from __future__ import annotations

import importlib.util

import pytest

from rebar.grounding import evidence as ev
from rebar.grounding import oracle, resolve

# The core-dep third-party proof leans on PyYAML. It is a hard core dependency, so
# it is installed in every job; the guard only future-proofs against a stripped env.
_CORE_TP_PKG = "yaml"
_CORE_TP_MEMBER = "yaml.safe_load"


def _require(pkg: str) -> None:
    """Skip (not fail) when a third-party package is not importable in this job."""
    pytest.importorskip(pkg)


# ── Core third-party dep (PyYAML): the ALWAYS-ON site-packages proof ───────────


def test_core_third_party_pkg_is_in_site_packages() -> None:
    """Guard: the always-on symbol really is an installed, non-repo dependency."""
    _require(_CORE_TP_PKG)
    spec = importlib.util.find_spec(_CORE_TP_PKG)
    assert spec is not None and spec.origin is not None
    assert "site-packages" in spec.origin  # genuinely third-party, not the rebar tree


def test_core_third_party_import_is_refuted_not_absent(tmp_path) -> None:
    """A bare ``import yaml`` reference resolves as EXISTING (refuted), even against
    an empty repo where the ctags index has zero definitions for it."""
    _require(_CORE_TP_PKG)
    rec = oracle.refute_absence(
        {"kind": "import", "name": _CORE_TP_PKG, "language": "python"},
        repo_root=str(tmp_path),
    )
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["coverage"]["backend"] == resolve.BACKEND_ENV
    ev.validate(rec)  # every emitted record stays contract-valid


def test_core_third_party_member_is_refuted_not_absent(tmp_path) -> None:
    """A dotted ``yaml.safe_load`` member — which the T1 ctags lane abstains on
    (member binding is T2 territory) — is refuted via an actual import + getattr."""
    _require(_CORE_TP_PKG)
    rec = oracle.refute_absence(
        {"kind": "member", "name": _CORE_TP_MEMBER, "language": "python"},
        repo_root=str(tmp_path),
    )
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["coverage"]["backend"] == resolve.BACKEND_ENV
    ev.validate(rec)


def test_core_third_party_container_plus_name_resolves() -> None:
    """A ``from yaml import safe_load`` shape (name + container) resolves."""
    _require(_CORE_TP_PKG)
    loc = resolve.resolve_in_environment("safe_load", container="yaml", language="python")
    assert loc is not None
    assert loc["module"] == "yaml" and loc["attr"] == "safe_load"


# ── [agents]-only dep (pydantic): extra coverage where site-packages has it ────


def test_pydantic_import_is_refuted_when_present(tmp_path) -> None:
    """On an ``[agents]`` job (pydantic present) the same third-party path holds;
    SKIPS on the default suite that does not install pydantic."""
    pytest.importorskip("pydantic")
    rec = oracle.refute_absence(
        {"kind": "import", "name": "pydantic", "language": "python"},
        repo_root=str(tmp_path),
    )
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["coverage"]["backend"] == resolve.BACKEND_ENV
    ev.validate(rec)


def test_pydantic_member_is_refuted_when_present(tmp_path) -> None:
    """``pydantic.BaseModel`` refutes via import + getattr where pydantic exists."""
    pytest.importorskip("pydantic")
    rec = oracle.refute_absence(
        {"kind": "member", "name": "pydantic.BaseModel", "language": "python"},
        repo_root=str(tmp_path),
    )
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    ev.validate(rec)


# ── stdlib: always importable, always runs (no third-party guard) ──────────────


def test_stdlib_symbol_is_refuted(tmp_path) -> None:
    """A stdlib module (``json``) also resolves in the environment — the repo index
    cannot see it either, so it must not read as absent."""
    rec = oracle.refute_absence(
        {"kind": "import", "name": "json", "language": "python"},
        repo_root=str(tmp_path),
    )
    assert rec["outcome"] == ev.OUTCOME_REFUTED


# ── No regression: genuinely-unresolvable references still abstain (always run) ─


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
    """A nonexistent attribute on a real installed module (stdlib ``json``) does NOT
    false-refute: the import succeeds but the getattr fails, so it stays an abstain."""
    rec = oracle.refute_absence(
        {"kind": "member", "name": "json.NoSuchAttribute406f", "language": "python"},
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
    assert resolve.resolve_in_environment("yaml", language="rust") is None


def test_injection_safe_name_is_rejected() -> None:
    """A non-identifier string is never handed to importlib as a module path."""
    assert resolve.resolve_in_environment("os; rm -rf /", language="python") is None
    assert resolve.resolve_in_environment("", language="python") is None
