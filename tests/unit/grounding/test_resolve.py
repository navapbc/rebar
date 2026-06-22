"""Unit tests for rebar.grounding.resolve — the T1 refutation resolver (Engine A).

These exercise the real universal-ctags binary over a small polyglot fixture
built in ``tmp_path`` (Python + JavaScript + Go), and assert the spike's two
load-bearing properties:

* the **0-false-refute guard** (spike E2): a unique bare name → ``refuted``; a
  common-name collision (defined twice) → ``abstain(ambiguous)``; a dotted/member
  ref → ``abstain``; a hallucinated name → ``abstain`` (NEVER ``refuted``).
* **fail-open** (S1 harness): no ctags binary / timeout / unsupported language →
  ``abstain``, never a raise, never an asserted absence.

Every evidence record emitted is validated against the S1 JSON-Schema contract
via ``ev.validate``. Tests needing ctags are skipped (not failed) where the
binary is absent, so the suite stays green on a host without it.
"""

from __future__ import annotations

import shutil

import pytest

from rebar.grounding import evidence as ev
from rebar.grounding import resolve as r

pytestmark = pytest.mark.unit

_HAVE_CTAGS = shutil.which("ctags") is not None
requires_ctags = pytest.mark.skipif(not _HAVE_CTAGS, reason="universal-ctags not on PATH")


# ── fixture: a small polyglot repo (Python + JS + Go) ────────────────────────

# 'config' is defined TWICE (core.py + util.py) -> the collision class. Every
# other name is unique. 'TicketStore' is imported in api.py to exercise an
# import-kind reference resolving to the class def.
_FIXTURE = {
    "pkg/core.py": (
        "class TicketStore:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "def reconcile_tickets(store):\n"
        "    return store\n"
        "def config():\n"
        "    return {}\n"
    ),
    "pkg/util.py": (
        "def normalize_name(name):\n"
        "    return name.strip()\n"
        "def config():\n"  # 'config' collision (2nd def)
        "    return None\n"
    ),
    "pkg/api.py": "from .core import TicketStore\n",
    "web/app.js": "function renderWidget() { return 1; }\nconst HARDCODED = 2;\n",
    "svc/main.go": "package main\nfunc ServeRequest() int { return 0 }\n",
}


@pytest.fixture
def repo(tmp_path):
    """Materialize the polyglot fixture under ``tmp_path``; return its root str."""
    for rel, content in _FIXTURE.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


@pytest.fixture
def index(repo):
    idx, result = r.build_index(repo)
    if idx is None:
        pytest.skip(f"ctags index unavailable: {result.abstain_reason} ({result.detail})")
    return idx


def _refute(name, repo, index, *, kind="symbol", **extra):
    ref = {"kind": kind, "name": name, **extra}
    rec = r.refute_absence(ref, repo_root=repo, index=index)
    ev.validate(rec)  # every emitted record MUST satisfy the S1 contract
    return rec


# ── reference-in schema / validate_reference ─────────────────────────────────


def test_reference_kinds_is_the_closed_five_set():
    assert r.REFERENCE_KINDS == frozenset({"symbol", "import", "dependency", "file", "member"})


def test_validate_reference_accepts_and_trims():
    out = r.validate_reference({"kind": "symbol", "name": "  Foo  ", "language": " python "})
    assert out == {"kind": "symbol", "name": "Foo", "language": "python"}


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "macro", "name": "x"},  # kind outside the closed set
        {"kind": "symbol"},  # missing name
        {"kind": "symbol", "name": "   "},  # blank name
        {"name": "x"},  # missing kind
        "not-a-dict",
    ],
)
def test_validate_reference_rejects_malformed(bad):
    with pytest.raises(r.ReferenceError):
        r.validate_reference(bad)


@pytest.mark.parametrize(
    "name,expected",
    [("Foo", False), ("foo_bar", False), ("a.b", True), ("recv.attr", True), ("pkg/mod", True)],
)
def test_is_member_name(name, expected):
    assert r.is_member_name(name) is expected


# ── the guard: unique -> refuted; collision -> ambiguous; member/halluc -> abstain


@requires_ctags
def test_unique_name_is_refuted(repo, index):
    rec = _refute("TicketStore", repo, index)
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec.get("reason") is None  # builders drop the null reason key entirely
    assert rec["provenance_tier"] == ev.TIER_T1
    assert rec["coverage"]["backend"] == r.BACKEND_CTAGS
    assert rec["coverage"]["status"] == ev.STATUS_RAN
    assert rec["location"]["file"].endswith("core.py")


@requires_ctags
def test_unique_name_in_each_language_is_refuted(repo, index):
    # polyglot: a unique symbol per language all resolve (py already covered above)
    for name in ("renderWidget", "ServeRequest", "normalize_name"):
        rec = _refute(name, repo, index)
        assert rec["outcome"] == ev.OUTCOME_REFUTED, name


@requires_ctags
def test_collision_name_abstains_ambiguous(repo, index):
    # 'config' is defined twice -> the collision class -> NOT refuted.
    rec = _refute("config", repo, index)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "ambiguous"


@requires_ctags
def test_hallucinated_name_abstains_never_refuted(repo, index):
    # The 0-false-refute safety property: a name not in the index is ABSTAIN,
    # never refuted, never "absent".
    for halluc in ("TicketStoer", "frobnicate", "renderWidgett"):
        rec = _refute(halluc, repo, index)
        assert rec["outcome"] == ev.OUTCOME_ABSTAIN, halluc
        assert rec["outcome"] != ev.OUTCOME_REFUTED


@requires_ctags
def test_member_dotted_reference_abstains(repo, index):
    # member binding is T2 -> never refute a dotted ref at T1.
    rec = _refute("store.reconcile_tickets", repo, index, kind="member")
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    # a bare 'symbol' kind whose NAME is dotted is also caught by the dotted gate.
    rec2 = _refute("ticket.normalize_name", repo, index, kind="symbol")
    assert rec2["outcome"] == ev.OUTCOME_ABSTAIN


@requires_ctags
def test_import_kind_resolves_like_symbol(repo, index):
    rec = _refute("TicketStore", repo, index, kind="import")
    assert rec["outcome"] == ev.OUTCOME_REFUTED


# ── the spike E2 naive-vs-guarded contrast, asserted ─────────────────────────


@requires_ctags
def test_spike_e2_naive_false_refutes_guarded_does_not(repo, index):
    """Mirror spike E2: naive bare name-existence false-refutes; the guard = 0 FR.

    A 'false-refute' = refuting a reference whose SPECIFIC target does not exist
    (a collision, a member, or a hallucination). The naive resolver (bare
    name-in-index) false-refutes the collision; the guarded resolver (the one
    under test) sends every such case to abstain.
    """
    hazards = [
        ("config", "symbol"),  # collision: exists twice
        ("store.reconcile_tickets", "member"),  # member: not bindable at T1
        ("frobnicate", "symbol"),  # hallucinated: not in the index
    ]

    def naive(name):
        # bare repo-wide name existence, no guard (the spike's _resolve_naive)
        return "refute" if index.lookup(name.split(".")[0]) else "abstain"

    naive_false_refutes = 0
    guarded_false_refutes = 0
    for name, kind in hazards:
        # naive: 'config' resolves on bare existence -> a false-refute.
        if naive(name) == "refute":
            naive_false_refutes += 1
        rec = _refute(name, repo, index, kind=kind)
        if rec["outcome"] == ev.OUTCOME_REFUTED:
            guarded_false_refutes += 1

    assert naive_false_refutes >= 1, "spike premise: naive false-refutes at least the collision"
    assert guarded_false_refutes == 0, "the guard restores 0 false-refute (spike E2)"


# ── kind=file: plain path existence ──────────────────────────────────────────


def test_file_kind_existing_path_refuted(repo):
    rec = r.refute_absence({"kind": "file", "name": "pkg/core.py"}, repo_root=repo)
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["location"]["file"] == "pkg/core.py"
    assert rec["coverage"]["backend"] == r.BACKEND_FS


def test_file_kind_missing_path_abstains(repo):
    rec = r.refute_absence({"kind": "file", "name": "pkg/does_not_exist.py"}, repo_root=repo)
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["outcome"] != ev.OUTCOME_REFUTED


def test_file_kind_path_escape_is_not_refuted(repo):
    # a `../`-escaping path must never refute against a file outside the repo.
    rec = r.refute_absence({"kind": "file", "name": "../../../etc/hosts"}, repo_root=repo)
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN


# ── kind=dependency routes to the T0 deps lane (S3), not resolved here ────────


def test_dependency_kind_abstains_routed_to_t0(repo):
    rec = r.refute_absence({"kind": "dependency", "name": "requests"}, repo_root=repo)
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["provenance_tier"] == ev.TIER_T0
    assert "S3" in rec["detail"]


# ── fail-open: no tool / timeout / unsupported language ──────────────────────


def test_no_ctags_binary_abstains(repo, monkeypatch):
    # point the resolver at a non-existent binary -> harness 'no_tool' abstain.
    monkeypatch.setattr(r, "_CTAGS_BIN", "definitely-not-a-real-ctags-xyz")
    r._CACHED_CTAGS_LANGS = None  # bust the language cache for this test
    rec = r.refute_absence({"kind": "symbol", "name": "TicketStore"}, repo_root=repo)
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "no_tool"
    assert rec["coverage"]["status"] == ev.STATUS_SKIPPED


@requires_ctags
def test_timeout_abstains(repo, monkeypatch):
    # force an immediate timeout -> harness reaps the child -> 'timeout' abstain.
    rec = r.refute_absence({"kind": "symbol", "name": "TicketStore"}, repo_root=repo, timeout=1e-9)
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "timeout"


@requires_ctags
def test_unsupported_language_abstains(repo, index):
    # a language ctags can't parse, with no project optlib -> unsupported_lang.
    rec = r.refute_absence(
        {"kind": "symbol", "name": "SomeProgram", "language": "Brainfuck-9000"},
        repo_root=repo,
        index=index,
    )
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "unsupported_lang"


@requires_ctags
def test_known_language_is_supported(repo, index):
    # a stock-ctags language must NOT trip the unsupported gate.
    rec = r.refute_absence(
        {"kind": "symbol", "name": "TicketStore", "language": "Python"},
        repo_root=repo,
        index=index,
    )
    assert rec["outcome"] == ev.OUTCOME_REFUTED


# ── project language-extensibility config (.rebar/grounding.toml) ─────────────


def test_load_config_absent_returns_empty(tmp_path):
    cfg = r.load_config(str(tmp_path))
    assert cfg == r.GroundingConfig()


def test_load_config_reads_slot(tmp_path):
    rebar_dir = tmp_path / ".rebar"
    rebar_dir.mkdir()
    (rebar_dir / "grounding.toml").write_text(
        "[grounding]\n"
        'ctags_optlib_dirs = ["tools/optlibs"]\n'
        'ctags_options = ["tools/cobol.ctags"]\n'
        'supported_languages = ["COBOL"]\n'
    )
    cfg = r.load_config(str(tmp_path))
    assert "COBOL" in cfg.supported_languages
    assert cfg.ctags_optlib_dirs[0].endswith("tools/optlibs")
    assert cfg.ctags_options[0].endswith("tools/cobol.ctags")


def test_config_declared_language_is_supported(tmp_path):
    cfg = r.GroundingConfig(supported_languages=frozenset({"COBOL"}))
    assert r._language_supported("cobol", cfg) is True


def test_config_with_optlib_is_permissive(tmp_path):
    cfg = r.GroundingConfig(ctags_optlib_dirs=("/some/dir",))
    # an optlib is configured -> we can't enumerate its langs, so be permissive.
    assert r._language_supported("ExoticLang", cfg) is True


@requires_ctags
def test_extensibility_slot_threads_into_ctags_cmd(repo):
    cfg = r.GroundingConfig(ctags_optlib_dirs=("/opt/optlibs",), ctags_options=("/opt/x.ctags",))
    cmd = r._ctags_cmd(repo, optlib_dirs=cfg.ctags_optlib_dirs, options=cfg.ctags_options)
    assert "--optlib-dir=/opt/optlibs" in cmd
    assert "--options=/opt/x.ctags" in cmd


# ── deterministic code/diff reference extractor ──────────────────────────────


def test_extract_references_from_python_source():
    src = "from rebar.grounding import evidence as ev, harness\nimport os, sys\nfrom .mod import *\n"
    refs = r.extract_references(src, in_file="x.py")
    names = {ref["name"] for ref in refs}
    assert names == {"ev", "harness", "os", "sys"}  # 'as' binds to ev; wildcard dropped
    assert all(ref["kind"] == "import" for ref in refs)
    assert all(ref["in_file"] == "x.py" for ref in refs)


def test_extract_references_unknown_language_is_empty():
    assert r.extract_references("fn main() {}", language="rust") == []


def test_extract_references_from_diff_only_added_imports():
    diff = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+from pkg import Thing\n"
        " context_line\n"
        "-from old import Gone\n"
    )
    refs = r.extract_references_from_diff(diff)
    names = {ref["name"] for ref in refs}
    assert names == {"Thing"}  # added import only; removed/context excluded


# ── extracted references round-trip through refute_absence ───────────────────


@requires_ctags
def test_extracted_import_resolves_against_index(repo, index):
    # api.py imports TicketStore; extract it and refute -> refuted.
    src = "from .core import TicketStore\n"
    refs = r.extract_references(src, in_file="pkg/api.py")
    assert refs
    rec = r.refute_absence(refs[0], repo_root=repo, index=index)
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_REFUTED
