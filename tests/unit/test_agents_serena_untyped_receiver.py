"""Guard the untyped-receiver caveat in the navigation guidance (bug dbf9).

`AGENTS.md` §"Navigating the codebase" documented `grep`'s blind spot (a symbol named as a
string) but not Serena's second one: an attribute access on a receiver whose static type is
`Any` — an unannotated parameter, or one explicitly annotated `Any` — cannot be bound to a
definition by Pyright, so `find_referencing_symbols` returns an EMPTY result rather than an
error. Reproduced on `AcliRestMixin/set_entity_property` (zero references; three real call
sites), with `AcliRestMixin/_direct_rest_put_raw` in the same class as the control.

`docs/code-navigation.md` opens by stating why this matters: the rule "replaced an earlier one
that was wrong in an important case — and a rule that is wrong some of the time teaches agents
to discount it all of the time". These tests keep the rule from being wrong in this case.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "AGENTS.md"
NAV_DOC = ROOT / "docs" / "code-navigation.md"


def _nav_section() -> str:
    text = AGENTS.read_text(encoding="utf-8")
    start = text.index("## Navigating the codebase")
    end = text.index("\n## Git workflow", start)
    return text[start:end]


def test_agents_nav_table_names_the_untyped_receiver_failure_mode() -> None:
    """The table routes an `Any`-typed receiver to grep, alongside the string-named case."""
    section = _nav_section().lower()
    assert "receiver" in section
    assert "`any`" in section
    # The existing string-literal row must survive alongside the new one.
    assert "monkeypatch.setattr" in section


def test_agents_nav_states_the_empty_result_asymmetry() -> None:
    """A non-empty Serena result is trustworthy; an empty one is not, unless typed."""
    section = _nav_section().lower()
    assert "empty" in section
    assert "confirm" in section or "check" in section


def test_agents_nav_row_count_covers_four_needs() -> None:
    """The table gains a row rather than rewording an existing one."""
    rows = [
        line for line in _nav_section().splitlines() if line.startswith("| ") and "---" not in line
    ]
    # header + 4 need-rows
    assert len(rows) == 5, f"expected a header and 4 rows, got {len(rows)}: {rows}"


def test_nav_doc_carries_the_untyped_receiver_reproduction() -> None:
    """`docs/code-navigation.md` evidences the claim, as it does for epic 061c S1."""
    doc = NAV_DOC.read_text(encoding="utf-8")
    assert "set_entity_property" in doc
    assert "_direct_rest_put_raw" in doc, "the control that proves Serena is not simply broken"
    # The three real call sites the empty result hides. The third moved from
    # ``apply_inbound_records.py`` into ``apply_inbound_events.py`` when that module was
    # split at its concern boundary (ticket 6f51-f8a4-b4fb-450c); the doc cites the writer's
    # true home, so this anchor follows it. The census is unweakened — still three sites.
    assert "dispatch_one.py" in doc
    assert "binding_store.py" in doc
    assert "apply_inbound_events.py" in doc


def test_nav_doc_row_count_phrase_matches_the_table() -> None:
    """The doc's opening sentence counts the AGENTS.md table's rows; keep them in step."""
    doc = NAV_DOC.read_text(encoding="utf-8")
    assert "three-row table" not in doc
    assert "four-row table" in doc
