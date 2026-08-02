"""HELD-OUT: the DC transport's whole-project readers must page (bug 9263).

THE DEFECT. `get_issuelinks_map` and `get_comment_map` each called
`self.search_issues(f"project = {project_key}")` with no offset loop, taking the default
`max_results=50`. Beyond 50 issues, links and comments were silently invisible: issues past
the first page appeared to have NO links (so the differ can emit spurious link creations and
cannot detect removals at all) and their comments never synced. Nothing raised; the pass
converged and reported success.

THIS IS THE THIRD INSTANCE OF ONE DEFECT CLASS, and the reason it exists is the point.
`get_parent_map` had exactly this bug and was fixed alone, in place, instead of sweeping every
`search_issues` caller — leaving these two behind, and a third (`fetcher._iter_pages`, bug
deac) found later. So the fix here is not "page these two": it is ONE shared pager that all
three route through, plus a STRUCTURAL test that fails the build if a fourth caller ever
takes the default again. A hand-rolled loop repeated four times is what produced this ticket.

The client shape below — serving N issues while capping EVERY page BELOW the requested size —
is a lowered `jira.search.views.default.max`, the documented DC hardening, and is the shape
that caught the original `get_parent_map` bug.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

_TRANSPORT_SRC = (
    pathlib.Path(__file__).resolve().parents[3]
    / "src/rebar/_engine/rebar_reconciler/adapters/jira_datacenter/transport.py"
)


class _PageCappingClient:
    """Serves ``total`` issues but caps EVERY page at ``server_cap``, whatever is requested."""

    def __init__(self, total: int = 250, server_cap: int = 20) -> None:
        self.issues = [
            {
                "key": f"DC-{i}",
                "fields": {"issuelinks": [{"id": f"L{i}", "type": {"name": "Blocks"}}]},
            }
            for i in range(total)
        ]
        self.server_cap = server_cap
        self.comment_calls: list[str] = []

    def search_issues(self, jql, startAt=0, maxResults=50, fields=None, **kw):  # noqa: N803
        return self.issues[startAt : startAt + min(maxResults, self.server_cap)]

    def comments(self, key):
        self.comment_calls.append(key)
        return [{"id": f"c-{key}", "body": "hi"}]


def _transport(client) -> JiraDataCenterTransport:
    t = JiraDataCenterTransport.__new__(JiraDataCenterTransport)
    t._client = client  # type: ignore[attr-defined]
    t.project = "DC"  # type: ignore[attr-defined]
    return t


def test_get_issuelinks_map_pages_to_exhaustion() -> None:
    """THE BUG, half one. Beyond the default 50 every issue looked link-less."""
    client = _PageCappingClient(total=250, server_cap=20)
    links = _transport(client).get_issuelinks_map("DC")
    assert len(links) == 250, (
        f"got {len(links)} of 250 issues — the link map is truncated, so issues past the "
        f"first page appear to have NO links: the differ can emit spurious link creations "
        f"and cannot detect removals on them at all"
    )


def test_link_payloads_survive_paging_not_just_the_key_count() -> None:
    """TEETH. A pager that returned the right number of KEYS while dropping the payload
    would satisfy a count-only assertion. Check issues from the SECOND and THIRD pages."""
    client = _PageCappingClient(total=250, server_cap=20)
    links = _transport(client).get_issuelinks_map("DC")
    for key in ("DC-25", "DC-45", "DC-249"):
        assert links.get(key), f"{key} (beyond page 1) carries no links: {links.get(key)!r}"
        assert links[key][0]["id"] == f"L{key.split('-')[1]}", (
            f"{key}'s link payload is not its own — pages were mis-assembled: {links[key]!r}"
        )


def test_get_comment_map_pages_to_exhaustion_and_fetches_each_issue() -> None:
    """THE BUG, half two — and comments cost one extra call per issue, so assert the
    per-issue fetch actually happened for issues beyond the first page."""
    client = _PageCappingClient(total=250, server_cap=20)
    comments = _transport(client).get_comment_map("DC")
    assert len(comments) == 250, f"got {len(comments)} of 250 issues in the comment map"
    assert "DC-249" in client.comment_calls, (
        "comments were never fetched for an issue beyond the first page"
    )
    assert len(client.comment_calls) == 250


def test_get_parent_map_still_recovers_everything() -> None:
    """REGRESSION GUARD. `get_parent_map` was already correct; routing it through the shared
    pager must not change that. This is the test whose original failure (20 of 250) defined
    the defect class."""

    class _ParentClient(_PageCappingClient):
        def search_issues(self, jql, startAt=0, maxResults=50, fields=None, **kw):  # noqa: N803
            page = self.issues[startAt : startAt + min(maxResults, self.server_cap)]
            return [{"key": i["key"], "fields": {"parent": {"key": "DC-EPIC"}}} for i in page]

    parents = _transport(_ParentClient(total=250, server_cap=20)).get_parent_map("DC")
    assert len(parents) == 250
    assert parents["DC-249"] == "DC-EPIC"


def test_get_parent_map_degradation_contract_is_preserved() -> None:
    """`get_parent_map` promises the inbound pass a SOFT failure: log a WARNING and return
    {} so the pass falls back to its parentless path rather than aborting. Routing it
    through a shared helper is exactly where that would be silently lost."""

    class _ExplodingClient:
        def search_issues(self, *a, **k):
            raise RuntimeError("boom")

    assert _transport(_ExplodingClient()).get_parent_map("DC") == {}


def test_an_empty_project_yields_an_empty_map_without_raising() -> None:
    """An empty project is a normal state, not a truncation."""

    class _EmptyClient(_PageCappingClient):
        def search_issues(self, *a, **k):
            return []

    t = _transport(_EmptyClient(total=0))
    assert t.get_issuelinks_map("DC") == {}
    assert t.get_comment_map("DC") == {}


# ---------------------------------------------------------------------------
# The structural guard — what stops the FOURTH instance
# ---------------------------------------------------------------------------


def _search_issues_calls_taking_the_default(src: str | None = None) -> list[str]:
    """Every `self.search_issues(...)` / `self._client.search_issues(...)` call in the
    transport that passes NO explicit result cap.

    An AST scan rather than a grep: the point is to catch a call, not a mention in a
    docstring or comment (this module's own prose names the method repeatedly).

    `src` exists ONLY so the teeth test below can aim this same predicate at a synthetic
    offender; omitting it scans the live transport, which is what the real guard does.
    """
    tree = ast.parse(src if src is not None else _TRANSPORT_SRC.read_text())
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "search_issues"):
            continue
        kwargs = {kw.arg for kw in node.keywords}
        if not ({"maxResults", "max_results"} & kwargs):
            offenders.append(f"line {node.lineno}")
    return offenders


def test_no_search_issues_call_relies_on_the_default_page_size() -> None:
    """THE CRITERION THAT CONVERTS "I fixed two" INTO "a fourth cannot be added silently".

    Every whole-project read must go through the shared pager (which passes an explicit
    page size); a bare `search_issues(jql)` takes `max_results=50` and silently truncates.
    """
    offenders = _search_issues_calls_taking_the_default()
    assert not offenders, (
        f"search_issues call(s) in the DC transport rely on the default max_results=50 and "
        f"will silently truncate past 50 issues: {offenders}. Route them through the shared "
        f"pager instead."
    )


_SYNTHETIC_OFFENDER_SRC = """\
class T:
    def unpaginated(self, k):
        return self.search_issues(f'project = {k}')

    def camel(self, k):
        return self._client.search_issues(f'project = {k}', startAt=0, maxResults=100)

    def snake(self, k):
        return self._client.search_issues(f'project = {k}', max_results=100)
"""


def test_the_structural_guard_fires_on_a_synthetic_unpaginated_caller() -> None:
    """TEETH for the guard itself. A structural test that cannot fail is decoration — and
    this one exists precisely because the previous audit of this seam under-counted.

    This drives the SAME predicate the real guard consumes (no second inline copy of the
    AST walk — that duplication is what let the guard rot unnoticed), and asserts it names
    the offending line: "returns a list" is satisfied by `return []`. The two paginated
    calls are the negative control — the predicate must accept BOTH spellings of the cap,
    so narrowing the accepted keyword set turns this red too.
    """
    offenders = _search_issues_calls_taking_the_default(_SYNTHETIC_OFFENDER_SRC)
    assert offenders == ["line 3"], (
        f"the shared AST predicate must report exactly the unpaginated call on line 3 of "
        f"the synthetic source and neither paginated call (maxResults on line 6, "
        f"max_results on line 9); it reported {offenders!r}"
    )


@pytest.mark.parametrize("method", ["get_issuelinks_map", "get_comment_map", "get_parent_map"])
def test_all_three_whole_project_readers_exist_and_page(method: str) -> None:
    """All three route through one pager. Asserted by behaviour (each recovers 250 from a
    page-capping client) rather than by reading the source, so an implementation that
    duplicates the loop a fourth time still has to be CORRECT."""
    client = _PageCappingClient(total=250, server_cap=20)
    result = getattr(_transport(client), method)("DC")
    assert len(result) == 250, f"{method} recovered {len(result)} of 250"
