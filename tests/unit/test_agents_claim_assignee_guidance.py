"""Guard the claim-assignee guidance in the canonical agent guide (bug 5cc0).

`AGENTS.md` used to instruct `claim <id> --assignee <you>` unconditionally. An explicit
`--assignee` always wins over the configured `ticket.default_assignee`
(`_commands/claim.py`: `if assignee is None: assignee = _config_default_assignee(...)`,
covered by `tests/unit/test_claim_default_assignee.py`), so the agent path never reached
the configured default — and `<you>` / `assignee="me"` invite a bare handle the reconciler
cannot resolve to a remote user, which the inbound differ then clears back to `""`
(bug 544e). `docs/config.md` already documents the correct rule; these tests keep the two
documents from contradicting each other.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "AGENTS.md"


def _agents_text() -> str:
    return AGENTS.read_text(encoding="utf-8")


def test_agents_md_does_not_instruct_a_bare_handle_assignee() -> None:
    """No site tells the agent to substitute itself as the `claim` assignee."""
    text = _agents_text()
    assert "--assignee <you>" not in text
    assert 'assignee="me"' not in text


def test_agents_md_claim_guidance_names_the_configured_default() -> None:
    """The claim instruction points at `ticket.default_assignee` instead of overriding it."""
    text = _agents_text()
    assert "ticket.default_assignee" in text
    assert "`claim <id>`" in text


def test_agents_md_requires_a_resolvable_identity_for_an_explicit_assignee() -> None:
    """Where an explicit assignee is documented, it must be a resolvable identity."""
    text = _agents_text()
    lowered = text.lower()
    assert "jira-resolvable" in lowered
    assert "accountid" in lowered
    assert "bare handle" in lowered


def test_agents_md_links_the_authoritative_config_section() -> None:
    """The guidance defers to `docs/config.md`, which already states the rule."""
    assert "docs/config.md#ticketdefault_assignee" in _agents_text()


def test_agents_md_agrees_with_docs_config_on_assignee_identity() -> None:
    """`docs/config.md` remains the authority the AGENTS.md rule is derived from."""
    config_doc = (ROOT / "docs" / "config.md").read_text(encoding="utf-8")
    assert "## `ticket.default_assignee` — applied at CLAIM, not at create" in config_doc
    assert "Jira-resolvable identity" in config_doc
