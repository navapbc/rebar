"""Live deps.dev oracle check for the T0 dependency-existence lane (story 2554).

The hermetic unit tier monkeypatches the HTTP seam; this is the external
counterpart that hits the REAL deps.dev v3 existence endpoint, proving the
end-to-end gauntlet against the live oracle (the spike2_deps.py E4 result, now in
the suite). Real/normalized names → ``refuted``; a hallucinated/slop name → never
a false absence (an ``abstain``). Marked ``external`` (excluded from the default
run; needs REBAR_RUN_EXTERNAL=1 and network to api.deps.dev). Run locally::

    REBAR_RUN_EXTERNAL=1 pytest -m external tests/external/test_deps_live.py
"""

from __future__ import annotations

import pytest

from rebar.grounding import deps
from rebar.grounding import evidence as ev

pytestmark = pytest.mark.external


def _ref(name: str, eco: str) -> dict[str, object]:
    return {"kind": "dependency", "name": name, "ecosystem": eco}


@pytest.mark.parametrize(
    "name,eco",
    [
        ("requests", "pypi"),
        ("scikit_learn", "pypi"),  # PEP-503 normalization → scikit-learn
        ("react", "npm"),
        ("serde", "cargo"),
        ("github.com/pkg/errors", "golang"),
    ],
)
def test_live_real_packages_refute(name: str, eco: str) -> None:
    rec = deps.refute_package(_ref(name, eco))
    assert rec["outcome"] == ev.OUTCOME_REFUTED, rec
    ev.validate(rec)


@pytest.mark.parametrize(
    "name,eco",
    [
        ("superfast-jsonify-9000-slop-zzz", "pypi"),
        ("reactt-totally-not-real-xyz-9001", "npm"),
        ("os", "pypi"),  # stdlib → abstain(stdlib), never absent
    ],
)
def test_live_hallucinated_and_stdlib_never_false_absent(name: str, eco: str) -> None:
    rec = deps.refute_package(_ref(name, eco))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN, rec  # NEVER absent / refuted-as-absence
    assert rec["reason"] in ev.ABSTAIN_REASONS
    ev.validate(rec)
