"""Story A (fe57) — hermetic Jira fixtures + verified fake, foundation checks.

Two guarantees this tier owns:

  * **No secrets leak** (AC1): a COMPLEMENTARY scan over ``tests/fixtures/jira/*.json``
    re-asserts the capture-time scrub held — no real emails, Jira accountIds, or the
    org-identifying Jira tenant host are present. The detectors here are deliberately
    BROADER than the scrubber's (any ``<digits>:<uuid-ish>`` accountId; any
    ``<sub>.atlassian.net`` host that isn't the redaction placeholder), so a narrowed
    scrubber regression is caught here rather than sharing the scrubber's blind spot.
  * **Consumed only via the production path** (AC2/AC4): the ``FakeAcliClient`` is
    routed through ``fetcher.compute_snapshot`` (the real enrichment merge) and the
    merged snapshot carries the nested ``comment`` / ``issuelinks`` / ``parent``
    shapes VERBATIM — proving the fake adds no shape massaging and that nothing
    reads the fixtures and hand-reshapes them.

The full producer->consumer round-trip contract (schema + differ + applier) is
story B; this file only pins story A's deliverables.
"""

from __future__ import annotations

import json
import re

import pytest
from _fakes import FIXTURE_DIR, FakeAcliClient, install

# Complementary leakage detectors — BROADER than the scrubber's (not copies of it),
# so a narrowed scrubber regression surfaces here instead of sharing a blind spot.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Any digits:hex-uuid-ish run — looser than the scrubber's fixed UUID layout.
_ACCOUNTID_RE = re.compile(r"\d{3,}(?::|%3A)[0-9a-fA-F]{6,}-[0-9a-fA-F-]{4,}")
# Any Jira tenant host; only the redaction placeholder subdomain is allowed.
_TENANT_HOST_RE = re.compile(r"https://([a-z0-9][a-z0-9-]*)\.atlassian\.net")
_ALLOWED_EMAILS = {"redacted@example.invalid"}
_ALLOWED_TENANTS = {"example"}


@pytest.mark.integration
def test_fixtures_carry_no_secrets() -> None:
    """Fixtures carry no real emails, Jira accountIds, or org-identifying host."""
    fixtures = sorted(FIXTURE_DIR.glob("*.json"))
    assert fixtures, f"no fixtures found under {FIXTURE_DIR}"
    for path in fixtures:
        text = path.read_text()
        emails = {e for e in _EMAIL_RE.findall(text) if e not in _ALLOWED_EMAILS}
        assert not emails, f"{path.name} leaks email(s): {sorted(emails)}"
        accountids = set(_ACCOUNTID_RE.findall(text))
        assert not accountids, f"{path.name} leaks Jira accountId(s)"
        tenants = {t for t in _TENANT_HOST_RE.findall(text) if t not in _ALLOWED_TENANTS}
        assert not tenants, f"{path.name} leaks Jira tenant host(s): {sorted(tenants)}"


@pytest.mark.integration
def test_fake_routes_fixtures_through_production_fetch(monkeypatch) -> None:
    """The fake flows fixtures through the production enrichment merge, unmassaged."""
    from rebar_reconciler import fetcher

    install(monkeypatch, fetcher)
    monkeypatch.setenv("JIRA_PROJECT", "REB")

    snapshot = fetcher.compute_snapshot("story-a-smoke")

    # The fixtures reached the snapshot via _build_snapshot (search + the three
    # enrichment maps), not by a direct fixture read.
    assert "REB-431" in snapshot, "fake search issues did not flow into the snapshot"

    raw_comment = json.loads((FIXTURE_DIR / "comment_map.json").read_text())
    raw_links = json.loads((FIXTURE_DIR / "issuelinks_map.json").read_text())

    entry = snapshot["REB-431"]
    # Nested ``comment`` key (bug 0ee6 shape) — present and VERBATIM (no reshaping).
    assert "comment" in entry, "comment enrichment did not merge"
    assert "comments" in entry["comment"], "comment field lost its nested 'comments' list"
    assert entry["comment"] == raw_comment["REB-431"], "fake reshaped the comment field"
    # ``issuelinks`` key (bug 3f04 shape) — present and VERBATIM.
    assert entry["issuelinks"] == raw_links["REB-431"], "fake reshaped issuelinks"
    assert entry["issuelinks"], "REB-431 should carry non-empty issuelinks"
    # ``parent`` merged as {"key": ...} from the parent map.
    assert entry.get("parent") == {"key": "REB-430"}, "parent enrichment did not merge"

    # REB-430 (the epic) is top-level: authoritative EMPTY links list, no parent.
    epic = snapshot["REB-430"]
    assert epic.get("issuelinks") == [], "empty-links authoritative shape lost"
    assert "parent" not in epic, "top-level issue should carry no parent key"

    # REB-407 exercises the non-null assignee shape end-to-end.
    assert isinstance(snapshot["REB-407"].get("assignee"), dict)


@pytest.mark.integration
def test_fake_search_pagination_contract() -> None:
    """FakeAcliClient.search_issues slices like the real client (terminates _iter_pages)."""
    fake = FakeAcliClient()
    page = fake.search_issues("project = REB", start_at=0, max_results=100)
    assert page, "first page should be non-empty"
    # A start_at past the end yields [] so the fetcher's pagination loop terminates.
    assert fake.search_issues("project = REB", start_at=len(page), max_results=100) == []
