"""Conformance oracle for ticket f0ff — every public dispatcher arm must have a
row in ``docs/exit-codes.md``.

The document declares itself the single source of truth for "each public
dispatcher arm", yet shipped with 41 rows against 75 real arms. The arm set is
derived MECHANICALLY (the union of the two authoritative sources) so a new
command that lacks a row fails this check — the CI guard the ticket adds (AC4).

Held-out oracles merged from test_exit_codes_doc_conformance_heldout: Held-out oracle for ticket
f0ff — cases the implementer does NOT see while filling in the per-command table against the happy-
path conformance test.
"""

from __future__ import annotations

import re
from pathlib import Path

_DOC = Path(__file__).resolve().parents[3] / "docs" / "exit-codes.md"


_ROW_RE = re.compile(r"^\| `([a-z][a-z0-9-]+)`", re.MULTILINE)


def dispatcher_arms() -> set[str]:
    """The authoritative set of public dispatcher arms, enumerated from the route
    registry itself (RP-05 S5): the union of the intercept class and every live,
    non-hidden canonical spelling. Derived via ``derive_policy_sets`` + route
    attributes rather than reconstructing the ``_cli`` policy frozensets by hand, so a
    new route class cannot silently escape the exit-code doc-conformance guard."""
    from rebar._cli._registry import ROUTES, derive_policy_sets

    derived = derive_policy_sets(ROUTES)
    intercepts = set(derived["_INTERCEPTS"])
    hidden = set(derived["_HIDDEN_ALIASES"])
    canonical = {r.name for r in ROUTES if not r.retired and r.name not in hidden}
    return (canonical | intercepts) - hidden


def documented_arms(text: str | None = None) -> set[str]:
    doc = text if text is not None else _DOC.read_text(encoding="utf-8")
    return set(_ROW_RE.findall(doc))


def undocumented_arms(arms: set[str], documented: set[str]) -> set[str]:
    """Pure set-diff: arms with no documentation row."""
    return arms - documented


def test_every_dispatcher_arm_has_an_exit_code_row() -> None:
    arms = dispatcher_arms()
    missing = undocumented_arms(arms, documented_arms())
    assert not missing, (
        f"{len(missing)} dispatcher arm(s) have no row in docs/exit-codes.md "
        f"(the single source of truth): {sorted(missing)}"
    )


# ── held-out oracles (merged from test_exit_codes_doc_conformance_heldout) ──


def _text() -> str:
    return _DOC.read_text(encoding="utf-8")


def test_conformance_guard_flags_an_undocumented_arm() -> None:
    # A synthetic new command with no row is reported; once documented it clears.
    assert undocumented_arms({"brand-new-cmd"}, set()) == {"brand-new-cmd"}
    assert undocumented_arms({"brand-new-cmd"}, {"brand-new-cmd"}) == set()


def test_guard_derives_arms_from_both_authoritative_sources() -> None:
    # The arm set is the union of the two authoritative sources; a derivation
    # bug that silently empties one source (e.g. the empty-``_INTERCEPTS``
    # import trap) would under-count and let arms go undocumented. Rather than
    # pin a magic total that drifts as verbs are added/retired, prove the
    # derivation stays anchored to BOTH sources and hasn't collapsed.
    import rebar._cli as cli
    from rebar._cli._help import known_subcommands

    arms = dispatcher_arms()
    expected = set(known_subcommands()) | set(cli._INTERCEPTS)
    assert arms == expected
    # Neither source silently emptied, and the union is non-trivial.
    assert set(known_subcommands()) <= arms
    assert set(cli._INTERCEPTS) <= arms
    assert len(arms) > 50


def test_no_arm_is_undocumented_after_the_fix() -> None:
    assert undocumented_arms(dispatcher_arms(), documented_arms()) == set()


def test_codes_3_4_12_75_78_are_documented() -> None:
    codes = set(re.findall(r"^\| `(\d+)`", _text(), re.MULTILINE))
    for code in ("3", "4", "12", "75", "78"):
        assert code in codes, f"exit code {code} is not documented in the codes table"


def test_forward_compat_sentence_present() -> None:
    # The contract sentence: an unknown/unlisted non-zero code MUST be treated as
    # failure. Require the tokens co-located in a single line so a stray "must" /
    # "fail" elsewhere in the document cannot satisfy it.
    for line in _text().splitlines():
        low = line.lower()
        if (
            "must" in low
            and re.search(r"non-?zero", low)
            and ("fail" in low)
            and re.search(r"unknown|unlisted|unrecognized|not listed|any other", low)
        ):
            return
    raise AssertionError(
        "docs/exit-codes.md lacks the forward-compatibility sentence: an unknown "
        "non-zero exit code MUST be treated as failure"
    )
