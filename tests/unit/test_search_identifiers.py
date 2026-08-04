"""Pure unit tests: ``search_states`` matches a ticket by its identifiers.

Ticket dfe3-7ea0-44f4-430b — ``rebar search`` must surface a ticket when the
query is that ticket's canonical ``ticket_id``, its ``alias``, or its bound Jira
key, case-insensitively, without special-casing them as "exact" (they fold into
the same substring haystack). ``search_states`` stays PURE: these tests drive the
match on hand-built state dicts — no filesystem, no binding store — proving the
haystack widening lives in the reducer while the Jira-key enrichment is the
caller's job.
"""

from __future__ import annotations

from rebar.reducer.search import search_states


def _state(**over) -> dict:
    st = {
        "status": "open",
        "ticket_type": "task",
        "title": "unrelated title",
        "description": "unrelated description",
        "tags": [],
        "comments": [],
        "ticket_id": "0303-692c-55dc-4a18",
        "alias": "doctorial-semiironic-wrasse",
    }
    st.update(over)
    return st


def _ids(results) -> set:
    return {t["ticket_id"] for t in results}


def test_matches_by_ticket_id() -> None:
    st = _state()
    assert _ids(search_states([st], "0303-692c-55dc-4a18")) == {"0303-692c-55dc-4a18"}


def test_matches_by_alias() -> None:
    st = _state()
    assert _ids(search_states([st], "doctorial-semiironic-wrasse")) == {"0303-692c-55dc-4a18"}


def test_matches_by_bound_jira_key() -> None:
    st = _state(jira_key="REB-1654")
    assert _ids(search_states([st], "REB-1654")) == {"0303-692c-55dc-4a18"}


def test_unrelated_identifier_does_not_match() -> None:
    st = _state(jira_key="REB-1654")
    assert search_states([st], "REB-9999") == []
    assert search_states([st], "9999-aaaa-bbbb-cccc") == []
    assert search_states([st], "some-other-alias") == []


def test_jira_key_match_is_case_insensitive() -> None:
    # The stored key is upper-case (canonical Jira); querying either case matches.
    st = _state(jira_key="REB-1654")
    assert _ids(search_states([st], "REB-1654")) == {"0303-692c-55dc-4a18"}
    assert _ids(search_states([st], "reb-1654")) == {"0303-692c-55dc-4a18"}


def test_ticket_id_and_alias_case_insensitive() -> None:
    st = _state()
    assert _ids(search_states([st], "0303-692C-55DC-4A18")) == {"0303-692c-55dc-4a18"}
    assert _ids(search_states([st], "DOCTORIAL-SEMIIRONIC-WRASSE")) == {"0303-692c-55dc-4a18"}


def test_identifier_widening_does_not_leak_into_text_haystack() -> None:
    # A state WITHOUT a jira_key must not match a Jira-key query (no false
    # positive from the widening) — only the bound state does.
    bound = _state(jira_key="REB-1654")
    unbound = _state(ticket_id="1111-2222-3333-4444", alias="other-alias-here")
    assert _ids(search_states([bound, unbound], "REB-1654")) == {"0303-692c-55dc-4a18"}
