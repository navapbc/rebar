"""HELD-OUT: the Jira-key → local-id derivation the live oracle depends on (bug 23ed).

WHY THIS EXISTS AS A UNIT TEST. The live provenance test asserts that a DC-created issue
produces a local ticket whose id is ``_jira_key_to_local_id(remote_key)``. That assertion is
only as good as the derivation it calls, and the live test runs only in the DC harness job —
so if the derivation changed, the live test would go red in a 20-minute CI job with a
confusing message, on a machine most contributors cannot reproduce.

More importantly: the ORIGINAL live assertion was ``remote_key in json.dumps(ticket)``, which
could never pass, because this function lowercases and the inbound CREATE payload carries no
raw Jira key. That defect survived the entire epic. Pinning the derivation here means the
*premise* of the live assertion is checked on every change, cheaply, rather than only when a
harness happens to be available.
"""

from __future__ import annotations

import pytest

from rebar_reconciler.inbound_translate import _jira_key_to_local_id


@pytest.mark.parametrize(
    ("jira_key", "expected"),
    [
        ("DIG-123", "jira-dig-123"),
        ("RBJVTVC-1", "jira-rbjvtvc-1"),
        ("ABC-1", "jira-abc-1"),
    ],
)
def test_the_derivation_prefixes_and_lowercases(jira_key: str, expected: str) -> None:
    """THE PROPERTY THE LIVE ORACLE DEPENDS ON. Lowercasing is the half that made the old
    substring assertion unsatisfiable, so it is asserted explicitly rather than implied."""
    assert _jira_key_to_local_id(jira_key) == expected


def test_the_raw_uppercase_key_is_not_a_substring_of_the_local_id() -> None:
    """TEETH, and the regression this bug is really about.

    This is the exact condition the old live assertion relied on
    (``remote_key in json.dumps(ticket)``). It is FALSE, and asserting that it is false is
    what stops someone reinstating a substring scan and re-hiding the test's verdict."""
    remote_key = "RBJVTVC-1"
    local_id = _jira_key_to_local_id(remote_key)
    assert remote_key not in local_id, (
        f"{remote_key!r} IS a substring of {local_id!r} — the derivation stopped lowercasing, "
        f"which would silently make a substring-scan oracle look correct again"
    )


def test_the_derivation_is_idempotent() -> None:
    """An already-prefixed local id must pass through unchanged, or a second inbound pass
    would mint ``jira-jira-dig-123`` and the ticket would fork."""
    assert _jira_key_to_local_id("jira-dig-123") == "jira-dig-123"
    assert _jira_key_to_local_id(_jira_key_to_local_id("DIG-123")) == "jira-dig-123"
