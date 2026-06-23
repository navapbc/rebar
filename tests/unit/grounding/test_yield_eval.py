"""S6 / AC2 — refutation-YIELD + false-refute EVAL on an INDEPENDENT real corpus.

Mirrors spike E3 (88% yield / 0 false-refute on rebar's own source). The corpus is
**not** a self-planted fixture — it is rebar's OWN source tree (``src/rebar``), a real
body of real internal cross-file imports. We:

* build a ctags T1 index over ``src/rebar`` with the REAL universal-ctags binary;
* harvest the real CROSS-FILE INTERNAL references — the relative-import bindings
  (``from .mod import Name``), which are genuine internal symbols defined elsewhere in
  the tree (the spike's corpus shape, not stdlib/third-party noise);
* run :func:`resolve.refute_absence` over each and MEASURE the refute yield (fraction
  resolved to ``refuted``). The yield is *measured, not hard-asserted to a specific %*
  (a self-authored fixture would be self-fulfilling) — we only assert it is reasonably
  high (> 50%), the load-bearing floor;
* assert ZERO false-refute on a HALLUCINATED control set (every refuted name mutated
  with an ``_xyzzy`` suffix): every control MUST abstain, never refute.

This is the load-bearing eval of the epic: the T1 floor refutes most real references
yet structurally cannot manufacture a refutation for a name that does not exist.
"""

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
    """Harvest CROSS-FILE INTERNAL references from the corpus.

    A relative import (``from .x import Name``, level > 0) binds a name that is DEFINED
    elsewhere in the same tree — a real cross-file internal reference, exactly the spike
    corpus. (Absolute imports of stdlib/third-party names are excluded: they are not
    internal, so a T1 internal-index abstain on them is correct, not a miss.)
    """
    refs: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
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
