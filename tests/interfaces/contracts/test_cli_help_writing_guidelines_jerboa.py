"""Content contract for ticket alamode-greasy-jerboa (F20).

F12 committed one help artifact for every visible route and generates the CLI
reference from those artifacts. Several parser descriptions and option help
strings that F12 newly exposed predated the documentation writing rules in
``docs/documentation-policy.md`` and carried em dashes, semicolons, clause-joining
colons, and casual connectors.

This is a content-specific behavior test over the named affected commands. It
asserts the specific corrected wording is present and that the specific removed
fragments are gone. It is deliberately NOT a repository-wide punctuation, banned
word, or character scanner: it names each command and each substring it checks,
so it enforces the outcome of this change rather than a general prose gate.
"""

from __future__ import annotations

import pytest

from rebar._cli import _help

# Commands whose help this change rewrote at its parser source.
AFFECTED = (
    "verify-completion",
    "sign-review",
    "review-plan",
    "review-code",
    "scan-spec",
    "explain",
    "verify-identity",
    "verify-authorship",
    "verify-opcert",
    "verify-commit-ticket",
    "enrich",
    "trusted-env",
    "remote-cert",
)

# Exact fragments removed from the affected help. Each is a construct the writing
# rules forbid (em dash, semicolon-joined clause, clause-joining colon, or a casual
# ``+`` / arrow connector) that used to appear in the named command's help.
REMOVED_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "verify-completion": (
        "definitions of done; for bugs",
        "auto \u2014 on for epics",
        "attestation \u2014 what a later",
        "verifier run \u2014 so this flag",
        "model API key; see",
    ),
    "sign-review": ("REVIEW_RESULT sidecar \u2014 WITHOUT",),
    "review-plan": (
        "on a ticket: a deterministic",
        "find \u2192 verify \u2192 decide \u2192 coach",
        "PASS SIGNS one \u2014 that attestation",
        "claim gate consumes \u2014 so this flag",
        "claim gate unsatisfied; recover",
        "and exit; does NOT inspect",
        "no re-sign); prints the verdict",
        "INDETERMINATE review: reuse",
        "missing unit; a PASS/BLOCK",
        "and --check; compatible",
        "end-result view \u2014 the per-unit",
    ),
    "review-code": ("(repeatable; default: deterministic",),
    "scan-spec": ("(repeatable; default: all open epics)",),
    "explain": ("\u2014",),  # the '({guides}) — e.g.' em-dash form is gone
    "verify-identity": (
        "merge-gate; also available",
        "grandfathered: reported but never",
    ),
    "verify-opcert": (
        "grandfathered: reported but never",
        "(default: cwd); resolves the ticket store",
    ),
    "verify-commit-ticket": ("(default: cwd); resolves the ticket store",),
    "enrich": ("as JSON; omit to drain",),
    "trusted-env": (
        "for add; <public_key-or-index>",
        "(default: cwd); resolves the ticket store",
    ),
    "remote-cert": (
        "(story ee0b)",
        "\u2014",
        "SigV4-signs",
    ),
}

# Positive corrected wording the rewrite introduced, one representative phrase per
# command. These assert the meaning survived the rewrite.
PRESENT_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "verify-completion": ("Run `rebar verify-completion --check` to confirm",),
    "sign-review": ("Use it to recover a signature",),
    "review-plan": ("find, verify, decide, then coach",),
    "review-code": ("repeatable (default: deterministic",),
    "scan-spec": ("repeatable (default: all open epics)",),
    "explain": ("For example",),
    "verify-identity": ("This is the authorship merge-gate",),
    "verify-opcert": ("which is the op-cert merge-gate",),
    "verify-commit-ticket": ("resolves the ticket store",),
    "enrich": ("Omit to drain",),
    "trusted-env": ("for add, or",),
    "remote-cert": ("self-authenticating",),
}


def _help_text(sub: str) -> str:
    text = _help.subcommand_help(sub)
    assert text is not None, f"missing committed help for {sub}"
    return text


def _normalized(sub: str) -> str:
    """Committed help wraps at a fixed width. Collapse runs of whitespace so a
    substring check does not fail on an incidental line break."""
    return " ".join(_help_text(sub).split())


@pytest.mark.parametrize("sub", AFFECTED)
def test_removed_fragments_absent(sub: str) -> None:
    text = _normalized(sub)
    for fragment in REMOVED_FRAGMENTS.get(sub, ()):
        assert fragment not in text, f"{sub} help still contains removed fragment: {fragment!r}"


@pytest.mark.parametrize("sub", AFFECTED)
def test_corrected_wording_present(sub: str) -> None:
    text = _normalized(sub)
    for fragment in PRESENT_FRAGMENTS.get(sub, ()):
        assert fragment in text, f"{sub} help is missing corrected wording: {fragment!r}"


def test_overview_summaries_free_of_removed_constructs() -> None:
    """The generated overview one-liners for the affected commands carry no em dash
    or semicolon, since those summaries are the same parser descriptions."""
    overview = _help.overview()
    lines = {
        line.split()[0]: line
        for line in overview.splitlines()
        if line.startswith("  ") and line.strip()
    }
    for sub in AFFECTED:
        line = lines.get(sub)
        if line is None:
            continue
        assert "\u2014" not in line, f"overview summary for {sub} still has an em dash"
        assert "\u2013" not in line, f"overview summary for {sub} still has an en dash"
        assert ";" not in line, f"overview summary for {sub} still has a semicolon"
