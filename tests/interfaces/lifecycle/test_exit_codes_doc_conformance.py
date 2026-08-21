"""Conformance oracle for ticket f0ff — every public dispatcher arm must have a
row in ``docs/exit-codes.md``.

The document declares itself the single source of truth for "each public
dispatcher arm", yet shipped with 41 rows against 75 real arms. The arm set is
derived MECHANICALLY (the union of the two authoritative sources) so a new
command that lacks a row fails this check — the CI guard the ticket adds (AC4).
"""

from __future__ import annotations

import re
from pathlib import Path

_DOC = Path(__file__).resolve().parents[3] / "docs" / "exit-codes.md"

# A per-command table row starts with `| \`<name>\`` where <name> is a lowercase
# subcommand (the numeric code-table rows start with a digit, so are excluded).
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
