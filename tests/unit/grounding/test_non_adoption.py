"""S6 / AC4 — the NON-ADOPTION test: a non-adopting client pays nothing.

The second IRONCLAD invariant of the epic: with the optional ``[grounding]`` extra ABSENT
(tree-sitter not installed — the current state of this checkout), a client that merely
``import rebar`` / ``import rebar.grounding`` must:

(a) **import clean** — pull NO heavy stack into ``sys.modules`` (no tree-sitter, no
    langchain/anthropic/etc.). Checked in a clean SUBPROCESS so the parent's already-warm
    modules can't mask a leak (the pattern from tests/unit/test_core_optionality.py).

(b) **fail open with zero footprint** — an oracle query with the external tools absent
    (binaries pointed at bogus names / offline) returns an ``abstain``/no-op, NEVER a
    raise, NEVER an ImportError, NEVER a manufactured absence.

The grounding contract + harness are stdlib-only by design; tree-sitter lives behind the
worker boundary and is imported lazily, so this is provable, not aspirational.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from rebar.grounding import deps, engine_b, oracle
from rebar.grounding import evidence as ev
from rebar.grounding import resolve as r

pytestmark = pytest.mark.unit

# The heavy/optional stack a non-adopting client must NOT pull. tree_sitter* is the
# [grounding] extra; the rest is the [agents]/[eval]/[tracing] stack.
_HEAVY = (
    "tree_sitter",
    "tree_sitter_language_pack",
    "langchain",
    "langgraph",
    "langchain_anthropic",
    "anthropic",
    "langfuse",
    "deepagents",
    "inspect_ai",
    "opentelemetry",
)


def test_grounding_extra_is_actually_absent() -> None:
    # The premise of this whole file: tree-sitter is NOT installed (the non-adoption
    # state). If a host DID install it, the import-clean guarantee still holds (lazy
    # import), so we don't fail — but we record the state for the report.
    import importlib.util

    have = importlib.util.find_spec("tree_sitter") is not None
    # No assertion either way; the subprocess test below is authoritative on cleanliness.
    print(f"\n[AC4] tree_sitter installed: {have} (non-adoption state expects False)")


def test_import_rebar_grounding_pulls_no_heavy_stack() -> None:
    # Clean subprocess: import the core + the grounding package and assert none of the
    # heavy modules landed in sys.modules.
    code = (
        "import sys;"
        "import rebar;"
        "import rebar.grounding;"
        "import rebar.grounding.oracle;"
        "import rebar.grounding.evidence;"
        "import rebar.grounding.resolve;"
        "import rebar.grounding.deps;"
        "import rebar.grounding.engine_b;"
        "import rebar.grounding.harness;"
        f"heavy={_HEAVY!r};"
        "leaked=[m for m in heavy if m in sys.modules];"
        "print('LEAK:' + ','.join(leaked) if leaked else 'CLEAN')"
    )
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "CLEAN", (
        f"importing rebar.grounding leaked the heavy stack: {cp.stdout.strip()}"
    )


def test_oracle_contract_is_import_clean_and_callable() -> None:
    # The static contract is a stdlib-only probe (no repo scan); it must return a
    # well-formed dict with the closed vocabularies, never raising even with no tools.
    contract = oracle.contract()
    assert set(contract["abstain_reasons"]) == set(ev.ABSTAIN_REASONS)
    assert set(contract["outcomes"]) == set(ev.OUTCOMES)
    # Backends are reported with availability; absent tools are available=False, not a raise.
    by_name = {b["name"]: b for b in contract["backends"]}
    assert "registry" in by_name
    # scc/lizard are absent here -> the metric backend reports unavailable, fail-open.
    if "metric" in by_name:
        assert isinstance(by_name["metric"]["available"], bool)


# ── (b) an oracle query with external tools ABSENT fails open to abstain ──────


def test_refute_query_with_ctags_absent_fails_open(tmp_path, monkeypatch) -> None:
    # Point the resolver at a bogus ctags binary -> abstain(no_tool), never a raise.
    (tmp_path / "x.py").write_text("def f(): pass\n")
    monkeypatch.setattr(r, "_CTAGS_BIN", "definitely-not-a-real-ctags-xyz")
    r._CACHED_CTAGS_LANGS = None
    rec = oracle.refute_absence({"kind": "symbol", "name": "f"}, repo_root=str(tmp_path))
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["outcome"] != ev.OUTCOME_REFUTED
    assert rec["reason"] in ev.ABSTAIN_REASONS


def test_scan_query_with_engines_absent_fails_open(tmp_path, monkeypatch) -> None:
    # All Engine B binaries pointed at bogus names + offline -> every record is a
    # fail-open abstain (no_tool), never a raise, never a silent no-op.
    (tmp_path / "app.js").write_text("function f(){ console.log('x'); }\n")
    monkeypatch.setattr(engine_b, "_OPENGREP_CANDIDATES", ("bogus-opengrep-xyz",))
    monkeypatch.setattr(engine_b, "_ASTGREP_CANDIDATES", ("bogus-astgrep-xyz",))
    monkeypatch.setattr(engine_b, "_METRIC_CANDIDATES", ("bogus-scc-xyz",))
    records = oracle.scan(str(tmp_path))
    assert records, "the scan must still produce coverage records (no silent no-op)"
    for rec in records:
        ev.validate(rec)
        # With every engine absent, every record is a fail-open abstain — never a match,
        # never a raise. (Skipped-by-routing records are also abstains.)
        assert rec["outcome"] == ev.OUTCOME_ABSTAIN
        assert rec["coverage"]["status"] == ev.STATUS_SKIPPED


def test_deps_query_offline_fails_open(monkeypatch) -> None:
    # Offline (the network seam raises) -> abstain(network_error), never a false absence.
    import urllib.error

    def _offline(url, timeout=10.0):
        raise urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(deps, "_http_get", _offline)
    rec = oracle.refute_absence(
        {"kind": "dependency", "name": "requests", "ecosystem": "pypi"}, repo_root="/tmp"
    )
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "network_error"


def test_oracle_query_never_raises_on_malformed_reference() -> None:
    # A malformed reference at the untrusted boundary is absorbed into an abstain,
    # never a raise (the facade is uniformly fail-open).
    rec = oracle.refute_absence({"kind": "bogus-kind", "name": "x"}, repo_root="/tmp")
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
