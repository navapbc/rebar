"""HELD-OUT pin on the ACLI transition path's status resolution (story J2, epic e369).

This file is HELD OUT from the implementation subagent.

J2 unifies the two drifted local->Jira status maps into one definition in
``adapters/jira_family/``. The two pre-existing copies DISAGREED on exactly one
key, so a single unified dict cannot preserve both callers — the story decides it
explicitly rather than smuggling it:

  * ``outbound_fields._LOCAL_TO_JIRA_STATUS`` mapped ``deleted`` -> ``"Done"``
    (as does ``rebar_reconciler/config.py``'s third copy);
  * ``jira_fields._LOCAL_STATUS_TO_JIRA`` OMITTED ``deleted``, so the ACLI path
    fell through to ``status.replace("_", " ").title()`` -> ``"Deleted"`` — not a
    state in the live DIG workflow ({To Do, In Progress, In Review, Done}), so
    Jira would REJECT that transition rather than apply it.

Two of the three copies already agreed on ``Done``, and ``"Deleted"`` is a latent
bug rather than behaviour worth preserving. So the unified map adopts
``deleted`` -> ``"Done"``, and this is J2's ONE declared observable change.

The tests assert through the real production entry point (``acli.transition_issue``)
and its contractual return value, not against the map object — the map is an
implementation detail, the resolved transition name is the contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from rebar_reconciler.adapters.jira import acli

_SETTINGS = SimpleNamespace(url="https://example.invalid", user="u", api_token="t")


def _resolve_via_transition_issue(status: str) -> tuple[str, str]:
    """Drive the real ``transition_issue`` and capture the name it resolved to.

    Returns ``(returned_status, name_sent_to_the_transport)`` so the assertion can
    check BOTH the contractual return value and what actually went to Jira.
    """
    sent: list[str] = []

    class _FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def transition_issue_by_name(self, jira_key: str, name: str) -> None:
            sent.append(name)

    with (
        mock.patch.object(acli, "AcliClient", _FakeClient),
        # PT008 is excluded here: it would swap this zero-arg lambda for a
        # `return_value=` MagicMock, which accepts ANY call shape. The lambda
        # pins production to calling `resolve_jira_settings()` with no args.
        mock.patch.object(acli, "resolve_jira_settings", lambda: _SETTINGS),  # noqa: PT008
    ):
        result = acli.transition_issue("PROJ-1", status)

    assert len(sent) == 1, "the transport must be driven exactly once"
    return result["status"], sent[0]


@pytest.mark.parametrize(
    ("local_status", "expected"),
    [
        ("idea", "IDEA"),
        ("open", "To Do"),
        ("in_progress", "In Progress"),
        ("blocked", "In Progress"),
        ("closed", "Done"),
        ("cancelled", "Done"),
    ],
)
def test_every_previously_handled_status_resolves_unchanged(
    local_status: str, expected: str
) -> None:
    """The contrast case for the one declared change: every OTHER status the ACLI
    path already handled keeps resolving to exactly the same Jira state name."""
    returned, sent = _resolve_via_transition_issue(local_status)
    assert returned == expected
    assert sent == expected


def test_deleted_now_resolves_to_done_instead_of_the_invalid_deleted() -> None:
    """J2's ONE declared observable change.

    Before: ``deleted`` was absent from the ACLI-side map and fell through to
    ``.title()``, producing ``"Deleted"`` — a state the live DIG workflow does not
    have, so Jira rejected the transition and the status never landed.
    After: it resolves to ``"Done"``, the value two of the three pre-existing
    copies already used.
    """
    returned, sent = _resolve_via_transition_issue("deleted")
    assert returned == "Done", (
        "deleted must resolve to Done via the unified map; 'Deleted' is the "
        "pre-J2 latent bug this story fixes"
    )
    assert sent == "Done"


def test_title_case_fallback_is_retained_for_unmapped_names() -> None:
    """The ``.title()`` fallback is independently load-bearing and must SURVIVE the
    unification: ``transition_issue`` accepts either a local status name or an
    already-Jira name, and the latter passes through it unchanged. Only the
    ``deleted`` outcome changes — the fallback itself does not go away.
    """
    returned, sent = _resolve_via_transition_issue("In Progress")
    assert returned == "In Progress"
    assert sent == "In Progress"

    # an unmapped snake_case name still normalises through the fallback
    returned, sent = _resolve_via_transition_issue("in_review")
    assert returned == "In Review"
    assert sent == "In Review"
