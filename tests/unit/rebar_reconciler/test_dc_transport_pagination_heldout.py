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


# ===========================================================================
# OFFSET-STALL (ticket 18a4-9df8-6373-4d9f) — the runaway-pagination sibling
# ===========================================================================
#
# THE DEFECT, second axis. Everything above hardens the PAGE-SIZE axis: a server that
# caps pages below the requested size must not be read as exhausted. This section covers
# the OFFSET axis, which that fix left open: `_paged_search`'s only loop exit is
# `if not batch: break`, and nothing verifies the server HONOURED `startAt`. A DC instance
# that ignores/repeats `startAt` re-serves the same non-empty page forever, so the exit is
# unreachable — measured on the unguarded pager: 26 calls (harness cap), requesting offsets
# [0, 3, 6, 9, 12, ...] while receiving page 1 every time, `out` growing without bound.
#
# Same defect class as bug cabc (Cloud's `acli_graph` cursor spin, fixed by ab7f in
# 30c522cde9 with `RunawayPaginationError`) and `fetcher._iter_pages` (offset-stall guard at
# fetcher.py:336-344, bug deac). Those two are the cited authority for the contract asserted
# here: a paged whole-project read whose server stops advancing is a TRUNCATED read, and it
# must abort LOUDLY rather than return a silent partial.
#
# WHY THE GUARD IS STRICTER THAN `fetcher._iter_pages`'s. fetcher returns cleanly (no raise)
# when the repeated page is SHORT, reasoning that a client which returns fewer items than
# asked and then repeats itself "has nothing further to give". That reasoning does NOT
# transfer to DC. This pager exists (bug 9263) precisely because a hardened DC caps EVERY
# page below the requested size via `jira.search.views.default.max` — so on DC a short page
# is the NORMAL case WITH more to give, and adopting fetcher's short-page branch would
# silently return 20 of 250: the exact 92%-loss defect this pager was built to refuse. Hence
# both page shapes below are parameterized and BOTH must raise.
#
# The guard cannot false-positive: a `startAt`-honouring server serves DIFFERENT issues at
# each offset, which is what `_PageCappingClient` above proves (250/250, raising nothing).

import importlib.util  # noqa: E402
import json as _json  # noqa: E402
import sys  # noqa: E402
from unittest.mock import patch  # noqa: E402

from rebar_reconciler._backend import BackendPaginationStallError  # noqa: E402

_FETCHER_PATH = (
    pathlib.Path(__file__).resolve().parents[3] / "src/rebar/_engine/rebar_reconciler/fetcher.py"
)


class _OffsetBlindClient:
    """A DC server that IGNORES ``startAt``: every request gets the SAME page.

    The documented real-world shape this models is a proxy/plugin that drops the query
    parameter, or a search endpoint whose index resets — both observed as "pagination never
    advances". ``page_len`` selects whether the repeated page is SHORT (< the requested
    ``maxResults``, the hardened-DC shape) or FULL.
    """

    def __init__(self, page_len: int = 3) -> None:
        self.calls: list[int] = []
        self.page = [
            {
                "key": f"DC-{i}",
                "fields": {
                    "issuelinks": [{"id": f"L{i}", "type": {"name": "Blocks"}}],
                    "parent": {"key": "DC-EPIC"},
                },
            }
            for i in range(page_len)
        ]

    def search_issues(self, jql, startAt=0, maxResults=50, fields=None, **kw):  # noqa: N803
        self.calls.append(startAt)
        # A guarded pager stops at 2 calls. The cap keeps an UNGUARDED pager from hanging
        # the suite, and is deliberately far above 2 so "stopped at 2" is a real assertion
        # and not an artefact of the cap.
        if len(self.calls) > 25:
            raise AssertionError(
                f"the pager made {len(self.calls)} calls against a startAt-ignoring server "
                f"— it never self-terminated (offsets requested: {self.calls[:10]}...). "
                f"This is the runaway spin ticket 18a4 exists to stop."
            )
        return list(self.page)

    def comments(self, key):
        return [{"id": f"c-{key}", "body": "hi"}]

    def fields(self):
        return []


@pytest.mark.parametrize(
    ("page_len", "shape"),
    [(3, "SHORT page (the hardened-DC shape)"), (100, "FULL page")],
)
def test_paged_search_aborts_loudly_when_the_server_ignores_start_at(
    page_len: int, shape: str
) -> None:
    """THE BUG. `startAt` is not honoured, so paging can never advance — abort, do not spin.

    Asserts BOTH halves of the contract: the loud error, and self-termination WITHIN 2 calls
    (the second call is what proves the repeat; a third would already be a spin).
    """
    client = _OffsetBlindClient(page_len=page_len)
    transport = _transport(client)

    with pytest.raises(BackendPaginationStallError) as caught:
        transport._paged_search("project = DC", page_size=100)

    assert len(client.calls) == 2, (
        f"{shape}: the pager made {len(client.calls)} calls before aborting; the stall is "
        f"detectable on the SECOND response (the first repeat), so 2 is the contract. "
        f"Offsets requested: {client.calls}"
    )
    # The message must name the actionable fact — which offset stalled — or an operator
    # cannot tell this apart from a transient fault.
    assert "startAt" in str(caught.value), (
        f"the error must name startAt as the un-honoured parameter; got: {caught.value!r}"
    )


def test_the_stall_error_escapes_get_parent_maps_fail_open_handler() -> None:
    """AC2, transport half. `get_parent_map` wraps the drain in a bare
    `except Exception: warn; return {}` degradation contract (_hierarchy.py:91), which
    would convert this abort into a SILENT empty parent map — the differ then treats every
    issue as parentless. Measured on the unguarded path: `{}` after 26 calls, raising
    nothing. The stall must be re-raised AHEAD of that clause, as the Cloud sibling does
    (acli_graph.py:304-306)."""
    transport = _transport(_OffsetBlindClient())
    transport._epic_link_field_id = None  # type: ignore[attr-defined]

    with pytest.raises(BackendPaginationStallError):
        transport.get_parent_map("DC")


def test_the_stall_error_is_importable_through_the_transport_facade() -> None:
    """AC4. `transport.py` is the documented re-export facade every DC importer reaches
    through (its own `__all__` docstring says so); a reader that cannot NAME the error
    cannot re-raise it past its fail-open handler."""
    from rebar_reconciler.adapters.jira_datacenter import transport as _dc_transport

    assert getattr(_dc_transport, "BackendPaginationStallError", None) is (
        BackendPaginationStallError
    ), "BackendPaginationStallError is not re-exported through the transport facade"
    assert "BackendPaginationStallError" in _dc_transport.__all__, (
        "the re-export is not recorded in __all__, so it reads as an accident"
    )


# ---------------------------------------------------------------------------
# The fetcher boundary — where a transport-level abort is silently absorbed
# ---------------------------------------------------------------------------
#
# `_paged_search` feeds THREE whole-project readers, and ALL THREE are consumed by
# `fetcher.fetch_snapshot` behind broad `except Exception` fail-open handlers
# (fetcher.py:610 parent, :652 comment, :688 issuelink). Piercing only the transport's own
# fail-open would leave the stall absorbed one layer up: the pass writes a silently degraded
# snapshot and reports success — which is the whole silent-loss class, just relocated. So
# the escape is asserted THROUGH the real `fetch_snapshot`.


def _load_fetcher():
    spec = importlib.util.spec_from_file_location("fetcher_18a4", _FETCHER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetcher_18a4"] = mod
    spec.loader.exec_module(mod)
    return mod


class _StalledEnrichmentClient:
    """Base scan succeeds; the named enrichment reader raises the stall error.

    The fault is injected at the enrichment call itself — inside the try block whose
    `except` clause is under test — so the handler actually executes. Injecting above it
    would leave the clause unrun.
    """

    def __init__(self, stalling: str) -> None:
        self._stalling = stalling
        self._served: set[str] = set()

    def search_issues(self, jql: str, start_at: int = 0, max_results: int = 50):
        # One non-empty page per JQL, then empty — a well-behaved base scan, so the only
        # thing that can raise is the enrichment under test.
        if jql in self._served:
            return []
        self._served.add(jql)
        return [{"key": "DC-1", "fields": {"summary": "one"}}]

    def _maybe_stall(self, name: str):
        if name == self._stalling:
            raise BackendPaginationStallError(
                "search endpoint is not honouring startAt (test injection)"
            )
        return {}

    def get_parent_map(self, project_key: str):
        return self._maybe_stall("get_parent_map")

    def get_comment_map(self, project_key: str):
        return self._maybe_stall("get_comment_map")

    def get_issuelinks_map(self, project_key: str):
        return self._maybe_stall("get_issuelinks_map")


@pytest.mark.parametrize("stalling", ["get_parent_map", "get_comment_map", "get_issuelinks_map"])
def test_the_stall_error_escapes_every_fetch_snapshot_enrichment_fail_open(
    tmp_path, stalling: str
) -> None:
    """AC2, fetcher half — one case per broad handler. A stalled whole-project read means
    the enrichment is TRUNCATED, not absent: degrading around it writes a snapshot the
    differ treats as authoritative (missing parents read as parentless, missing issuelinks
    as "no links", so the inbound differ cannot detect removals). Loud beats fail-open."""
    fetcher = _load_fetcher()
    client = _StalledEnrichmentClient(stalling)

    with patch.object(fetcher, "_load_acli", return_value=client):
        with pytest.raises(BackendPaginationStallError):
            fetcher.fetch_snapshot(f"18a4-stall-{stalling}", repo_root=tmp_path)


@pytest.mark.parametrize("stalling", ["get_parent_map", "get_comment_map", "get_issuelinks_map"])
def test_ordinary_enrichment_failures_still_fail_open_and_write_the_snapshot(
    tmp_path, stalling: str
) -> None:
    """NEGATIVE CONTROL, and the reason this fix is narrow. Each of those three handlers
    exists to keep a pass alive through a TRANSIENT enrichment fault, and that contract must
    survive untouched — a re-raise clause that swallowed the distinction (or a bare `raise`
    added to the wrong clause) would abort every pass on any hiccup. Only the STALL is loud.

    The transport-side twin of this control is
    `test_get_parent_map_degradation_contract_is_preserved` above."""

    class _OrdinaryFailure(_StalledEnrichmentClient):
        def _maybe_stall(self, name: str):
            if name == self._stalling:
                raise RuntimeError("transient enrichment hiccup")
            return {}

    fetcher = _load_fetcher()
    with patch.object(fetcher, "_load_acli", return_value=_OrdinaryFailure(stalling)):
        out = fetcher.fetch_snapshot(f"18a4-open-{stalling}", repo_root=tmp_path)

    assert out.exists(), "an ordinary enrichment failure must still write a degraded snapshot"
    assert "DC-1" in _json.loads(out.read_text()), (
        "the degraded snapshot must still carry the base scan's issues"
    )
