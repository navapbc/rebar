"""Polyglot fixtures guard against false refutation.

Common names, dotted members, unsupported languages, workspace members, and
distribution-name mismatches abstain rather than invent a target. Language
extension files widen routing. A mocked registry checks present, missing, and
standard-library dependency cases without network access. Every evidence
record validates against the grounding contract."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rebar.grounding import deps, engine_b
from rebar.grounding import evidence as ev
from rebar.grounding import resolve as r

pytestmark = pytest.mark.unit

_HAVE_CTAGS = shutil.which("ctags") is not None
requires_ctags = pytest.mark.skipif(not _HAVE_CTAGS, reason="universal-ctags not on PATH")


# ── the polyglot fixture corpus (Python + JS + Go + Ruby + an unsupported lang) ──

# 'handler' is defined TWICE (py + go) -> the cross-language collision hazard.
# 'Widget' is unique (js). 'normalize' is unique (rb). 'main.bf' is an unsupported lang.
_CORPUS = {
    "py/core.py": (
        "class Widget:\n"
        "    pass\n"
        "def handler(req):\n"  # collision def #1
        "    return req\n"
        "def unique_py_symbol():\n"
        "    return 1\n"
    ),
    "go/svc.go": (
        "package main\n"
        "func handler() int { return 0 }\n"  # collision def #2 (cross-language)
        "func ServeOnce() int { return 1 }\n"
    ),
    "rb/lib.rb": "def normalize(s)\n  s.strip\nend\n",
    "js/app.jsx": "function RenderWidget() { return 1; }\n",
    "exotic/main.bf": "++++++++[>++++++++<-]>+.\n",  # Brainfuck: an unsupported language
}


@pytest.fixture
def corpus(tmp_path: Path) -> str:
    for rel, content in _CORPUS.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


@pytest.fixture
def index(corpus):
    if not _HAVE_CTAGS:
        pytest.skip("universal-ctags not on PATH")
    idx, result = r.build_index(corpus)
    if idx is None:
        pytest.skip(f"ctags index unavailable: {result.abstain_reason}")
    return idx


def _refute(name, corpus, index, *, kind="symbol", **extra):
    rec = r.refute_absence({"kind": kind, "name": name, **extra}, repo_root=corpus, index=index)
    ev.validate(rec)
    return rec


# ── HAZARD 1: common-name collision (defined > 1 time) → abstain(ambiguous) ───


@requires_ctags
def test_cross_language_collision_abstains_not_refuted(corpus, index) -> None:
    # 'handler' is defined in BOTH py and go -> the collision class -> NOT refuted.
    rec = _refute("handler", corpus, index)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "ambiguous"
    assert rec["outcome"] != ev.OUTCOME_REFUTED


@requires_ctags
def test_unique_names_per_language_refute(corpus, index) -> None:
    # The polyglot control: each genuinely-unique symbol refutes (one per language).
    for name in ("Widget", "ServeOnce", "normalize", "RenderWidget", "unique_py_symbol"):
        rec = _refute(name, corpus, index)
        assert rec["outcome"] == ev.OUTCOME_REFUTED, name


# ── HAZARD 2: member / dotted reference → abstain (T2, not refuted at T1) ──────


@requires_ctags
def test_member_dotted_reference_abstains(corpus, index) -> None:
    rec = _refute("widget.handler", corpus, index, kind="member")
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["outcome"] != ev.OUTCOME_REFUTED
    # a bare 'symbol' whose NAME is dotted is caught by the same dotted gate.
    rec2 = _refute("recv.normalize", corpus, index, kind="symbol")
    assert rec2["outcome"] == ev.OUTCOME_ABSTAIN
    # A dotted member has a unique bare final segment. Its member gate must
    # abstain before a bare-name lookup could falsely refute.
    rec3 = _refute("recv.normalize", corpus, index, kind="member")
    assert rec3["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec3["reason"] == "ambiguous"
    assert "member" in (rec3.get("detail") or "").lower()


@requires_ctags
def test_hallucinated_polyglot_names_abstain_never_refuted(corpus, index) -> None:
    for halluc in ("Widgett", "Handlerr", "normaliez", "render_widget_xyzzy"):
        rec = _refute(halluc, corpus, index)
        assert rec["outcome"] == ev.OUTCOME_ABSTAIN, halluc
        assert rec["outcome"] != ev.OUTCOME_REFUTED


# ── HAZARD 3: unsupported-language file → abstain(unsupported_lang) ────────────


@requires_ctags
def test_unsupported_language_abstains(corpus, index) -> None:
    rec = _refute("SomeSymbol", corpus, index, language="Brainfuck")
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "unsupported_lang"


# ── HAZARD 4: language-extension slot is honored (routing widens) ─────────────


def test_grounding_toml_optlib_slot_is_read_and_threads_into_ctags(corpus) -> None:
    # A project .rebar/grounding.toml declaring an optlib + a custom language is read,
    # the language becomes 'supported', and the optlib threads into the ctags cmd.
    rebar_dir = Path(corpus) / ".rebar"
    rebar_dir.mkdir(exist_ok=True)
    (rebar_dir / "grounding.toml").write_text(
        "[grounding]\n"
        'ctags_optlib_dirs = ["tools/optlibs"]\n'
        'ctags_options = ["tools/cobol.ctags"]\n'
        'supported_languages = ["COBOL"]\n'
    )
    cfg = r.load_config(corpus)
    assert "COBOL" in cfg.supported_languages
    assert r._language_supported("cobol", cfg) is True  # the slot widened routing
    cmd = r._ctags_cmd(corpus, optlib_dirs=cfg.ctags_optlib_dirs, options=cfg.ctags_options)
    assert any("--optlib-dir=" in part and "tools/optlibs" in part for part in cmd)
    assert any("--options=" in part and "tools/cobol.ctags" in part for part in cmd)


def test_sgconfig_customlanguages_slot_widens_astgrep_routing(corpus) -> None:
    # A project .rebar/sgconfig.yml customLanguages entry registers a tree-sitter
    # grammar; its extensions widen ast-grep routing so a detector in that language is
    # applicable (not skipped) and the sgconfig path is read.
    rebar_dir = Path(corpus) / ".rebar"
    rebar_dir.mkdir(exist_ok=True)
    (rebar_dir / "sgconfig.yml").write_text(
        "customLanguages:\n  mojo:\n    libraryPath: mojo.so\n    extensions: [mojo]\n"
    )
    (Path(corpus) / "main.mojo").write_text("fn main(): pass\n")
    sgconfig, custom_exts = engine_b._resolve_astgrep_sgconfig(Path(corpus))
    assert sgconfig and sgconfig.endswith("sgconfig.yml")
    assert custom_exts == {"mojo": {".mojo"}}
    # a mojo detector now routes as applicable (the slot widened routing).
    from rebar.grounding.detectors import registry as reg_mod

    det = reg_mod.Detector(
        id="project.mojo.smell",
        backend=reg_mod.BACKEND_ASTGREP,
        namespace="project",
        source_path=str(Path(corpus) / "rule.yml"),
        rule={"language": "mojo"},
        envelope={"job": ev.JOB_SMELL, "tier": ev.TIER_T1},
    )
    applicable, _ = engine_b._is_applicable(
        det, engine_b._repo_extensions(Path(corpus)), Path(corpus), custom_exts
    )
    assert applicable is True


# ── HAZARD 5: DEPS lane — workspace member + import-vs-dist mismatch ───────────


def _dep(name: str, eco: str = "pypi") -> dict:
    return {"kind": "dependency", "name": name, "ecosystem": eco}


def test_workspace_member_abstains_never_absent(monkeypatch) -> None:
    # A workspace member abstains even when the registry responds successfully.
    monkeypatch.setattr(
        deps, "_http_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no probe"))
    )
    rec = deps.refute_package(
        _dep("my-internal-crate", "cargo"), workspace_members={"my-internal-crate"}
    )
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "private_or_internal_suspected"


def test_import_vs_distribution_name_mismatch_abstains(monkeypatch) -> None:
    # bs4 is the IMPORT name; the DISTRIBUTION is beautifulsoup4 -> ambiguous abstain,
    # never a false absence (the probe must not even run for a known import alias).
    monkeypatch.setattr(
        deps, "_http_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no probe"))
    )
    rec = deps.refute_package(_dep("bs4", "pypi"))
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "ambiguous"
    assert "beautifulsoup4" in rec["detail"]


def test_enumerated_workspace_feeds_refutation_guard(tmp_path, monkeypatch) -> None:
    # Enumerated Cargo members abstain. A public dependency can refute.
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "rootcrate"\n[workspace]\nmembers = ["crates/foo"]\n'
        '[dependencies]\nserde = "1"\n'
    )
    res = deps.enumerate_dependencies(tmp_path)
    members = res["workspace_members"]
    assert "rootcrate" in members
    monkeypatch.setattr(deps, "_http_get", lambda url, timeout=10.0: 200)
    internal = deps.refute_package(_dep("rootcrate", "cargo"), workspace_members=members)
    ev.validate(internal)
    assert internal["outcome"] == ev.OUTCOME_ABSTAIN
    public = deps.refute_package(_dep("serde", "cargo"), workspace_members=members)
    ev.validate(public)
    assert public["outcome"] == ev.OUTCOME_REFUTED


# ── DEPS-lane eval: real → refute, hallucinated/stdlib/slop → abstain ─────────


def test_stdlib_short_circuit_guards_the_guard(monkeypatch) -> None:
    # A permissive registry exposes the standard-library short circuit.
    # Standard-library names still abstain before a dependency probe.
    monkeypatch.setattr(deps, "_http_get", lambda url, timeout=10.0: 200)
    for name, eco in (("os", "pypi"), ("fmt", "golang")):
        rec = deps.refute_package(_dep(name, eco))
        ev.validate(rec)
        assert rec["outcome"] == ev.OUTCOME_ABSTAIN, (
            f"{name}: stdlib must not refute under a 200 registry"
        )
        assert "stdlib" in (rec.get("detail") or "").lower()
    # control: a genuine package still refutes under the same 200 router.
    assert deps.refute_package(_dep("requests", "pypi"))["outcome"] == ev.OUTCOME_REFUTED


def test_deps_lane_eval_real_refutes_slop_abstains(monkeypatch) -> None:
    """The deps analogue of the AC2 yield eval: real packages refute, everything else
    abstains — and NOT a single record is a false absence (there is no absent outcome).
    """
    real = {"react", "serde", "requests"}

    def _router(url: str, timeout: float = 10.0) -> int:
        # Only the real packages 200; slop names 404.
        return 200 if any(url.endswith("/" + n) for n in real) else 404

    monkeypatch.setattr(deps, "_http_get", _router)

    refs = [
        # real -> refute
        (_dep("react", "npm"), ev.OUTCOME_REFUTED),
        (_dep("serde", "cargo"), ev.OUTCOME_REFUTED),
        (_dep("requests", "pypi"), ev.OUTCOME_REFUTED),
        # hallucinated/slop -> abstain (404)
        (_dep("reactt-not-real-xyz", "npm"), ev.OUTCOME_ABSTAIN),
        (_dep("serde-fake-9000", "cargo"), ev.OUTCOME_ABSTAIN),
        # stdlib -> abstain (short-circuits before the probe)
        (_dep("os", "pypi"), ev.OUTCOME_ABSTAIN),
        (_dep("fmt", "golang"), ev.OUTCOME_ABSTAIN),
    ]
    refuted = abstained = 0
    for ref, expected in refs:
        rec = deps.refute_package(ref)
        ev.validate(rec)
        # the cardinal invariant: never an asserted absence.
        assert rec["outcome"] in (ev.OUTCOME_REFUTED, ev.OUTCOME_ABSTAIN)
        assert rec["outcome"] == expected, f"{ref['name']}: {rec['outcome']} != {expected}"
        if rec["outcome"] == ev.OUTCOME_REFUTED:
            refuted += 1
        else:
            abstained += 1
    assert refuted == 3 and abstained == 4
