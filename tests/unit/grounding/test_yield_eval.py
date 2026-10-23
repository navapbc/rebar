"""Measure refutation yield on cross-file imports in rebar's source tree.

Universal Ctags indexes the source corpus. The test requires yield above 50%
without hard-coding the observed rate, then mutates refuted names into missing
controls and requires zero false refutations. This separates useful resolution
from invented matches."""

from __future__ import annotations

import ast
import os
import shutil

import pytest

from rebar.grounding import evidence as ev
from rebar.grounding import resolve as r

pytestmark = pytest.mark.unit

_HAVE_CTAGS = shutil.which("ctags") is not None
requires_ctags = pytest.mark.skipif(not _HAVE_CTAGS, reason="universal-ctags not on PATH")

# The independent real corpus: rebar's own source tree.
_SRC_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "rebar")
)
#: A floor for the measured yield. The spike measured 88%; the relative-import subset
#: here measures ~75% (the rest are legitimately collisions / dotted refs the guard
#: abstains). We assert a conservative floor, not the exact value (it is MEASURED).
_YIELD_FLOOR = 0.50


def _iter_internal_references(root: str) -> list[dict]:
    """Collect relative imports that refer across files in the source corpus.
    Exclude absolute standard-library and third-party imports from the internal
    yield denominator."""
    refs: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root)
            try:
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=path)
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0:
                    for alias in node.names:
                        if alias.name != "*":
                            refs.append({"kind": "import", "name": alias.name, "in_file": rel})
    return refs


@pytest.fixture(scope="module")
def corpus_index():
    if not _HAVE_CTAGS:
        pytest.skip("universal-ctags not on PATH")
    assert os.path.isdir(_SRC_ROOT), f"corpus root missing: {_SRC_ROOT}"
    idx, result = r.build_index(_SRC_ROOT, timeout=180)
    if idx is None:
        pytest.skip(f"ctags index over corpus unavailable: {result.abstain_reason}")
    return idx


@requires_ctags
def test_refutation_yield_and_zero_false_refute_on_real_corpus(corpus_index, capsys) -> None:
    refs = _iter_internal_references(_SRC_ROOT)
    assert len(refs) >= 50, f"corpus too small to be meaningful: {len(refs)} refs"

    refuted_names: list[str] = []
    abstained = 0
    for ref in refs:
        rec = r.refute_absence(ref, repo_root=_SRC_ROOT, index=corpus_index)
        ev.validate(rec)  # every emitted record satisfies the S1 contract
        # The cardinal invariant: never an asserted absence (there is none); a probe is
        # either a refutation (the symbol exists) or an abstain — never a false-refute.
        assert rec["outcome"] in (ev.OUTCOME_REFUTED, ev.OUTCOME_ABSTAIN)
        if rec["outcome"] == ev.OUTCOME_REFUTED:
            refuted_names.append(ref["name"])
        else:
            abstained += 1

    total = len(refuted_names) + abstained
    yield_frac = len(refuted_names) / total if total else 0.0

    # ── false-refute control: hallucinate each refuted name; every one MUST abstain ──
    distinct = sorted(set(refuted_names))
    false_refutes = []
    for name in distinct:
        halluc = name + "_xyzzy"  # a name that demonstrably does not exist
        rec = r.refute_absence(
            {"kind": "import", "name": halluc}, repo_root=_SRC_ROOT, index=corpus_index
        )
        ev.validate(rec)
        if rec["outcome"] == ev.OUTCOME_REFUTED:
            false_refutes.append(halluc)

    # Print the MEASURED numbers (visible with -s); not hard-asserted to an exact %.
    with capsys.disabled():
        print(
            f"\n[AC2 yield eval] corpus={_SRC_ROOT}\n"
            f"  internal cross-file refs : {total}\n"
            f"  refuted (resolved)       : {len(refuted_names)}\n"
            f"  abstained (collision/dotted/not-found): {abstained}\n"
            f"  MEASURED refute yield    : {yield_frac * 100:.1f}%\n"
            f"  hallucinated controls    : {len(distinct)}\n"
            f"  FALSE-REFUTES            : {len(false_refutes)}"
        )

    # Load-bearing assertions: the floor on yield (measured, not pinned) + 0 false-refute.
    assert yield_frac > _YIELD_FLOOR, f"yield {yield_frac:.1%} below floor {_YIELD_FLOOR:.0%}"
    assert false_refutes == [], f"false-refute on hallucinated controls: {false_refutes}"


@requires_ctags
def test_unique_real_symbol_resolves_dotted_member_abstains(corpus_index) -> None:
    """Spot-check the corpus directly: a real unique symbol refutes; a member abstains.

    Grounds the yield number in two concrete, hand-verifiable corpus facts so the eval
    is not just an aggregate.
    """
    # 'GroundingContractError' is defined exactly once (evidence.py) -> a unique refute.
    rec = r.refute_absence(
        {"kind": "import", "name": "GroundingContractError"},
        repo_root=_SRC_ROOT,
        index=corpus_index,
    )
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_REFUTED

    # A dotted/member reference is T2 -> never refuted at T1.
    rec2 = r.refute_absence(
        {"kind": "member", "name": "evidence.GroundingContractError"},
        repo_root=_SRC_ROOT,
        index=corpus_index,
    )
    ev.validate(rec2)
    assert rec2["outcome"] == ev.OUTCOME_ABSTAIN
