"""Ticket aff0 (HELD-OUT edge oracle): probe adapter edges + capability degradation.

Withheld from the implementer: the branches that separate a real probe port from a
happy-path fake — the 4xx/5xx classification edges, the GET-only invariant, the
missing-env error, the capability-LACKING → UNREACHABLE degradation, and the proof the
neutral vocabulary stayed at root with no vendor import.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from rebar_reconciler import inbound_probe
from rebar_reconciler.adapters.jira import probe as jira_probe

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[4]
_REC = _REPO / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _REC / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── adapter classification edges ─────────────────────────────────────────────
def test_classifies_archived_or_moved() -> None:
    for code in (404, 410, 403):
        r = jira_probe.classify_probe_response("PROJ-3", code, {})
        assert r.branch == inbound_probe.ProbeBranch.ARCHIVED_OR_MOVED, code


def test_classifies_unreachable() -> None:
    for code in (500, 502, 503, 401):
        r = jira_probe.classify_probe_response("PROJ-4", code, {})
        assert r.branch == inbound_probe.ProbeBranch.UNREACHABLE, code


def test_request_is_get_only() -> None:
    req = jira_probe._make_request("https://example.atlassian.net", "PROJ-1", "user", "tok")
    assert req.get_method() == "GET"


def test_missing_env_raises_probe_config_error(monkeypatch) -> None:
    for var in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REBAR_ROOT", "/nonexistent-so-no-config")
    with pytest.raises(inbound_probe.ProbeConfigError, match="JIRA_"):
        jira_probe._resolve_env()


# ── the neutral vocabulary stayed at root, with no vendor import ─────────────
#
# NON-VACUITY (bug 8a5e, same rot class as bug 34c2). This guard used to read exactly one
# file, `inbound_probe.py`. That module is now 55 lines of pure vocabulary whose only
# imports are stdlib — its own docstring says the mechanics live in `adapters/jira/probe.py`
# — so the offender set was structurally empty and the guard could not fail. The rot risk is
# not hypothetical: if a later split moved half the neutral vocabulary into a sibling
# (`probe_vocab.py`, say), the guard would keep reading `inbound_probe.py` and pass while the
# sibling imported vendor code freely.
#
# The repair is the one proven on bug 34c2: derive the scan POPULATION rather than pin it,
# then assert the population covers the vocabulary the contract is about. The population is
# the root module plus the transitive closure of its intra-package imports, so any module the
# vocabulary is split into is scanned automatically — the root must import it back for
# `inbound_probe.<name>` to keep resolving.


def _module_imports(tree: ast.AST) -> set[str]:
    """Every dotted module path named by an `import` / `from ... import` in `tree`."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
            modules.update(f"{node.module or ''}.{a.name}" for a in node.names)
    return modules


def _neutral_layer_sources(pkg_dir: Path, root: str) -> dict[str, str]:
    """``{module name: source}`` for `root` and every sibling in `pkg_dir` it reaches.

    Parameterised on the package directory so the teeth test below can drive the same walk
    over a throwaway package — proving the walk actually FOLLOWS a relocation, which a
    synthetic-source test cannot show.
    """
    sources: dict[str, str] = {}
    queue = [root]
    while queue:
        name = queue.pop()
        if name in sources:
            continue
        path = pkg_dir / f"{name}.py"
        if not path.is_file():
            continue
        sources[name] = path.read_text()
        for module in _module_imports(ast.parse(sources[name])):
            tail = module.rsplit(".", 1)[-1]
            if (pkg_dir / f"{tail}.py").is_file():
                queue.append(tail)
    return sources


def _vendor_imports(sources: dict[str, str]) -> list[str]:
    """Every vendor import across `sources`, as ``"<module>:<imported path>"``."""
    offenders: list[str] = []
    for name, src in sorted(sources.items()):
        for module in sorted(_module_imports(ast.parse(src))):
            if "adapters.jira" in module or "acli_subprocess" in module:
                offenders.append(f"{name}:{module}")
    return offenders


def test_root_inbound_probe_is_neutral_vocabulary() -> None:
    """The neutral probe layer still exports the neutral vocab and imports nothing from
    ``adapters.jira``/``acli_subprocess`` at any depth — vendor mechanics live behind the
    adapter seam, and a vendor import at the root would invert that dependency."""
    assert {b.value for b in inbound_probe.ProbeBranch} == {
        "present_resolved",
        "present_filtered",
        "archived_or_moved",
        "unreachable",
    }
    assert issubclass(inbound_probe.ProbeConfigError, RuntimeError)

    offenders = _vendor_imports(_neutral_layer_sources(_REC, "inbound_probe"))
    assert not offenders, f"the neutral probe layer must stay vendor-free; imports: {offenders}"


def test_the_neutrality_guard_scans_the_modules_that_define_the_vocabulary() -> None:
    """ANTI-VACUITY (bug 8a5e). The guard above is only meaningful if the source it scans is
    where the neutral vocabulary actually lives. Assert the POPULATION, not just the verdict,
    so a split that relocates part of the vocabulary fails the build instead of silently
    leaving it unpoliced."""
    sources = _neutral_layer_sources(_REC, "inbound_probe")
    defined = {
        node.name
        for src in sources.values()
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.ClassDef)
    }
    missing = {"ProbeBranch", "ProbeConfigError"} - defined
    assert not missing, (
        f"the neutrality guard scans {sorted(sources)}, which do not define {sorted(missing)} "
        f"— the neutral vocabulary has moved outside the scanned layer, so a vendor import "
        f"beside it would NOT fail the build. Re-aim _neutral_layer_sources()."
    )


def test_the_neutrality_guard_follows_the_vocabulary_into_a_sibling(tmp_path: Path) -> None:
    """TEETH for the widened scan. A synthetic-source test proves the offender predicate
    works but cannot detect the guard being aimed at the wrong FILE — which is the whole
    defect class here. So drive the real walk over a throwaway package shaped like the
    relocation we fear: a clean root module that re-exports vocabulary from a sibling, and
    the sibling carrying the vendor import.

    A guard pinned to the root alone reports nothing here; the widened walk must find it.
    """
    (tmp_path / "root_probe.py").write_text(
        "from rebar_reconciler.probe_vocab import ProbeBranch\n\n__all__ = ['ProbeBranch']\n"
    )
    (tmp_path / "probe_vocab.py").write_text(
        "from rebar_reconciler.adapters.jira import probe\n\nProbeBranch = probe\n"
    )

    pinned_only = {"root_probe": (tmp_path / "root_probe.py").read_text()}
    assert not _vendor_imports(pinned_only), (
        "precondition: the root module is itself clean, so a guard pinned to it sees nothing"
    )

    offenders = _vendor_imports(_neutral_layer_sources(tmp_path, "root_probe"))
    assert any(o.startswith("probe_vocab:") for o in offenders), (
        f"the vendor import in the sibling holding the relocated vocabulary went unreported "
        f"(offenders: {offenders!r}) — the guard is still effectively pinned to one file"
    )
