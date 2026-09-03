"""Both PROSE copies of the confirmable-verb list are gated against ``_CONFIRM_SCOPE``.

The 17 confirmable mutation verbs appear three times: the derived ``_CONFIRM_SCOPE`` (canonical),
the ``EDITORIAL_PREAMBLE`` emitted into ``docs/cli-reference.md``, and a hand-authored sentence in
``docs/user-guide.md``. Only the curated ``MUTATION_VERBS`` dict was gated, so adding a
``confirmable=True`` route left both prose copies stale — in exactly the documents that tell an
agent which commands ask for confirmation (mirror F14).

All three agree at 17/17 today, so the gate is preventive; these tests are what give it teeth.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:  # the generators are scripts, not an installed package
    sys.path.insert(0, str(_SCRIPTS))

import gen_cli_reference as gen  # noqa: E402

from rebar._cli import _CONFIRM_SCOPE  # noqa: E402

_CANONICAL = set(_CONFIRM_SCOPE)


def _user_guide_text() -> str:
    return gen.USER_GUIDE_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("label", "text"),
    [("EDITORIAL_PREAMBLE", gen.EDITORIAL_PREAMBLE), ("docs/user-guide.md", _user_guide_text())],
)
def test_each_prose_copy_matches_the_canonical_scope(label: str, text: str) -> None:
    """AC4: both copies agree with ``_CONFIRM_SCOPE`` on the committed tree."""
    assert gen.prose_verb_drift(label, text, _CANONICAL) is None


def test_the_committed_check_passes() -> None:
    """AC4/AC5: the whole check — dict keys AND both prose copies — is green at rest."""
    gen._check_mutation_verbs()


@pytest.mark.parametrize("label", ["EDITORIAL_PREAMBLE", "docs/user-guide.md"])
def test_an_added_prose_verb_is_reported(label: str) -> None:
    """AC1/AC2/AC3: a verb the canonical set lacks is named as extra, against that document."""
    text = "Every mutating verb (`create`, `claim`, `teleport`)"
    drift = gen.prose_verb_drift(label, text, _CANONICAL)
    assert drift is not None
    assert label in drift
    assert "teleport" in drift.split("stale/extra")[1]


@pytest.mark.parametrize("label", ["EDITORIAL_PREAMBLE", "docs/user-guide.md"])
def test_a_dropped_prose_verb_is_reported(label: str) -> None:
    """AC1/AC2/AC3: a canonical verb the prose omits is named as missing, in that direction."""
    kept = sorted(_CANONICAL - {"claim"})
    text = "Every mutating verb (" + ", ".join(f"`{verb}`" for verb in kept) + ")"
    drift = gen.prose_verb_drift(label, text, _CANONICAL)
    assert drift is not None
    assert label in drift
    assert "claim" in drift.split("stale/extra")[0]


def test_a_rewritten_sentence_fails_loudly_rather_than_silently() -> None:
    """A passage the regex can no longer find must not read as agreement.

    This is the failure mode a set comparison alone would invert: no match would yield an
    empty verb set, which differs from canonical and so happens to fail — but reports all 17
    as missing, pointing at the verbs instead of at the parser that stopped working.
    """
    assert gen.prose_mutation_verbs("The verbs that confirm are: `create`, `claim`.") is None
    drift = gen.prose_verb_drift("somewhere", "no such sentence", _CANONICAL)
    assert drift is not None
    assert "could not locate" in drift


def test_the_dict_keys_comparison_is_retained() -> None:
    """AC5: the original curated-dict arm still exists and still fires."""
    original = dict(gen.MUTATION_VERBS)
    try:
        gen.MUTATION_VERBS.pop("claim")
        with pytest.raises(ValueError, match="MUTATION_VERBS is out of sync"):
            gen._check_mutation_verbs()
    finally:
        gen.MUTATION_VERBS.clear()
        gen.MUTATION_VERBS.update(original)
    gen._check_mutation_verbs()
