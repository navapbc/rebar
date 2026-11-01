"""Held-out ACLI status-resolution contract for unified Jira mappings (J2).

The former ACLI map omitted deleted and fell back to Deleted, which the DIG
workflow rejects. The unified mapping resolves deleted to Done. Assert the
resolved transition through acli.transition_issue, not the internal map."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from rebar.reconciler.adapters.jira import acli

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
