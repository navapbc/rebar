"""HELD-OUT: Cloud's three whole-project readers must page to exhaustion (bug 0fad).

WHY THIS MODULE EXISTS. ``adapters/jira/acli_graph.py``'s ``get_parent_map``,
``get_comment_map`` and ``get_issuelinks_map`` each walk an ENTIRE project through the
opaque ``nextPageToken`` cursor of ``POST /rest/api/3/search/jql``. Until this module,
NO test drove any of them past a single page: the only coverage
(``mutate/test_acli_cross_mixin_binding.py``) feeds one payload carrying ``isLast: True``.

That gap sits on top of a defect class this codebase has shipped THREE times, always as
a silent truncation of a whole-project read — never as an error:
  * change 1105 — DC ``get_parent_map`` advanced by the REQUESTED page size and stopped on
    a short page; measured 20 of 250 parents recovered (92% silently lost);
  * [rebar:9263-b404-cb6c-463f] — the same bug in DC ``get_issuelinks_map`` /
    ``get_comment_map``;
  * [rebar:deac-4f5a-856a-49bd] — the same bug in ``fetcher._iter_pages``, which lost the
    ISSUES themselves and so made every unseen bound issue a deletion candidate.
DC now has ``test_dc_transport_pagination_heldout.py`` pinning all of this. Cloud had
nothing, which is backwards relative to risk: Cloud is the deployment in production use.

WHAT THE INVESTIGATION FOUND. Cloud's readers page CORRECTLY today — they are cursor-driven
(echo the server's opaque token back) rather than offset-driven, so the advance-by-requested-
size arithmetic that broke DC does not exist here to get wrong. This module is therefore a
REGRESSION NET, not a bug reproduction: every oracle below has been mutation-checked to go
red when the loop is broken after the first page or made to stop on a short page.

The runaway-cursor hole this module originally pinned as a strict-xfail —
[rebar:cabc-7a98-d173-4d7c], a server repeating the same cursor token spun the loop until
the transport failed and the fail-open handler swallowed THAT into a silent partial map —
is now CLOSED [rebar:ab7f-f0cc-7384-43a7]: all three readers route through the module's
single shared ``_iter_cursor_pages`` walk, which raises ``RunawayPaginationError`` on a
same-token-twice stall, and the readers re-raise it PAST their fail-open handlers.

DELIBERATE LITERALS. ``_EXPECTED_PAGE_SIZE`` below is a module-local literal spelling what
the reader is expected to request, NOT an import of the reader's own ``page_size``. Deriving
it from the code under test makes the assertion move with the bug and it can never fail.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

from rebar_reconciler.adapters.jira.acli import AcliClient
from rebar_reconciler.adapters.jira.acli_graph import (
    RunawayPaginationError,
    _iter_cursor_pages,
)

_JIRA_ADAPTER_DIR = (
    pathlib.Path(__file__).resolve().parents[3] / "src/rebar/_engine/rebar_reconciler/adapters/jira"
)

# Module-local literals — see the module docstring. 250 items at 100/page forces THREE
# pages, so a fencepost error at the page boundary is observable rather than accidentally
# passing on a two-page walk.
_SEEDED_ISSUES = 250
_EXPECTED_PAGE_SIZE = 100
_EXPECTED_PAGES = 3
# A server that caps pages BELOW what was requested — the documented shape that caught the
# original DC bug (a lowered `jira.search.views.default.max`). 250 at 40/page = 7 pages.
_SHORT_PAGE_CAP = 40
_SHORT_PAGE_COUNT = 7
# Bound for the runaway-cursor probe so a spinning reader fails the test instead of hanging
# the suite. The harness — not the reader — enforces this; that is the point of the xfail.
_RUNAWAY_CALL_CAP = 25


# ---------------------------------------------------------------------------
# The three readers under test, each with the field it requests and the shape it
# returns, so one cursor server drives all three.
# ---------------------------------------------------------------------------


def _parent_fields(i: int) -> dict[str, Any]:
    return {"parent": {"key": f"DIG-EPIC-{i}"}}


def _comment_fields(i: int) -> dict[str, Any]:
    return {"comment": {"comments": [{"id": f"c-{i}", "body": f"body-{i}"}], "total": 1}}


def _issuelinks_fields(i: int) -> dict[str, Any]:
    return {"issuelinks": [{"id": f"L-{i}", "type": {"name": "Blocks"}}]}


_READERS: dict[str, tuple[str, Any, Any]] = {
    # method -> (requested field, per-issue fields payload, expected map value)
    "get_parent_map": ("parent", _parent_fields, lambda i: f"DIG-EPIC-{i}"),
    "get_comment_map": (
        "comment",
        _comment_fields,
        lambda i: {"comments": [{"id": f"c-{i}", "body": f"body-{i}"}], "total": 1},
    ),
    "get_issuelinks_map": (
        "issuelinks",
        _issuelinks_fields,
        lambda i: [{"id": f"L-{i}", "type": {"name": "Blocks"}}],
    ),
}


class _CursorServer:
    """A ``/rest/api/3/search/jql`` stand-in that paginates by opaque cursor token.

    Substituted for ``_direct_rest_post_json`` — the seam the readers call — so every
    request BODY the reader builds is recorded verbatim and the cursor echo can be asserted
    on it (AC: "the second request carries the token the first response returned").

    ``page_cap`` caps a page BELOW whatever ``maxResults`` asked for; ``terminator`` selects
    which of the two documented stop conditions the final page uses.
    """

    def __init__(
        self,
        fields_for: Any,
        total: int = _SEEDED_ISSUES,
        page_cap: int | None = None,
        terminator: str = "isLast",
    ) -> None:
        self.issues = [{"key": f"DIG-{i}", "fields": fields_for(i)} for i in range(total)]
        self.page_cap = page_cap
        self.terminator = terminator
        self.bodies: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.paths: list[str] = []

    def __call__(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.paths.append(path)
        self.bodies.append(dict(body))
        if len(self.bodies) > _RUNAWAY_CALL_CAP:
            # Hang-proofing, and it is load-bearing for the mutation checks: a reader that
            # ignores one of the two stop conditions re-requests the same offset forever,
            # so without this the mutant HANGS instead of failing. The readers' fail-open
            # `except Exception` converts this into their silent-partial-map path, which is
            # exactly what the recovered-set oracles then report.
            raise AssertionError(
                f"the walk never terminated: {len(self.bodies)} requests for "
                f"{len(self.issues)} issues — the cursor loop is spinning"
            )
        token = body.get("nextPageToken")
        offset = int(str(token).split(":")[1]) if token else 0
        want = body.get("maxResults", 0)
        size = min(want, self.page_cap) if self.page_cap is not None else want
        page = self.issues[offset : offset + size]
        nxt = offset + len(page)
        resp: dict[str, Any] = {"issues": page}
        if nxt >= len(self.issues):
            if self.terminator == "isLast":
                # Also hand back a live token: `isLast` ALONE must stop the loop.
                resp["isLast"] = True
                resp["nextPageToken"] = f"cursor:{nxt}"
            elif self.terminator == "absent-token":
                # No `isLast`, no token: the null-cursor condition ALONE must stop it.
                pass
            elif self.terminator == "null-token":
                resp["nextPageToken"] = None
            else:  # pragma: no cover — defensive: unknown terminator is a test-authoring bug
                raise AssertionError(f"unknown terminator {self.terminator!r}")
        else:
            resp["nextPageToken"] = f"cursor:{nxt}"
        self.responses.append(resp)
        return resp


def _client(server: Any) -> AcliClient:
    c = AcliClient(
        jira_url="https://test.atlassian.net",
        user="svc@example.com",
        api_token="fake-token",
        jira_project="DIG",
    )
    c._direct_rest_post_json = server  # type: ignore[method-assign]
    return c


def _run(method: str, **server_kw: Any) -> tuple[dict[str, Any], _CursorServer]:
    _field, fields_for, _expected = _READERS[method]
    server = _CursorServer(fields_for, **server_kw)
    out = getattr(_client(server), method)("DIG")
    return out, server


# ---------------------------------------------------------------------------
# AC 1 — every seeded item is recovered across MORE than one page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", sorted(_READERS))
def test_reader_recovers_every_seeded_issue_across_three_pages(method: str) -> None:
    """THE CORE ORACLE. A reader that stops after page one silently loses 60% of the
    project here, and nothing raises: the differ just sees issues with no parent / no
    comments / no links. Assert the RECOVERED SET, not a count — a reader returning the
    right NUMBER of wrong items must fail too.
    """
    out, server = _run(method)
    expected_value = _READERS[method][2]
    assert set(out) == {f"DIG-{i}" for i in range(_SEEDED_ISSUES)}, (
        f"{method} recovered {len(out)} of {_SEEDED_ISSUES} issues — the whole-project map "
        f"is TRUNCATED, so issues past the first page look parentless / comment-less / "
        f"link-less to the differ. Missing: "
        f"{sorted({f'DIG-{i}' for i in range(_SEEDED_ISSUES)} - set(out))[:5]}…"
    )
    assert len(server.bodies) == _EXPECTED_PAGES, (
        f"{method} issued {len(server.bodies)} requests for {_SEEDED_ISSUES} issues at "
        f"{_EXPECTED_PAGE_SIZE}/page; expected {_EXPECTED_PAGES}"
    )
    # Payload identity: pages must not be mis-assembled onto the wrong keys.
    for i in (0, _EXPECTED_PAGE_SIZE + 5, _SEEDED_ISSUES - 1):
        assert out[f"DIG-{i}"] == expected_value(i), (
            f"{method}: DIG-{i} carries another issue's payload — pages were "
            f"mis-assembled: {out[f'DIG-{i}']!r}"
        )


@pytest.mark.parametrize("method", sorted(_READERS))
def test_reader_requests_the_documented_page_size_on_every_request(method: str) -> None:
    """A reader that quietly dropped ``maxResults`` would take Jira's default (50) and
    double its request count; a reader that asked for a huge page would be truncated
    server-side. ``_EXPECTED_PAGE_SIZE`` is a literal here ON PURPOSE — importing the
    reader's own ``page_size`` would make this assertion move with the code.
    """
    _out, server = _run(method)
    assert [b.get("maxResults") for b in server.bodies] == [_EXPECTED_PAGE_SIZE] * (
        _EXPECTED_PAGES
    ), f"{method} request page sizes: {[b.get('maxResults') for b in server.bodies]}"
    assert all(p == "/rest/api/3/search/jql" for p in server.paths), server.paths
    assert all(b.get("fields") == [_READERS[method][0]] for b in server.bodies), (
        f"{method} must request only its own field: {[b.get('fields') for b in server.bodies]}"
    )


# ---------------------------------------------------------------------------
# AC 2 — the cursor ADVANCES: request N+1 carries the token response N returned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", sorted(_READERS))
def test_the_cursor_token_advances_across_requests(method: str) -> None:
    """The cursor contract is stateful and nothing pinned it. Asserted on the recorded
    request BODIES: the first request must carry NO token (sending one blind is an API
    error), and each later request must echo the token the PREVIOUS response returned.
    A reader that re-sent the first token would refetch page one forever; one that sent a
    stale or synthesised token would skip or duplicate a page.
    """
    _out, server = _run(method)
    assert "nextPageToken" not in server.bodies[0], (
        f"{method}'s FIRST request must omit nextPageToken; it sent "
        f"{server.bodies[0].get('nextPageToken')!r}"
    )
    for n in range(1, len(server.bodies)):
        assert server.bodies[n].get("nextPageToken") == server.responses[n - 1].get(
            "nextPageToken"
        ), (
            f"{method} request {n} carried cursor "
            f"{server.bodies[n].get('nextPageToken')!r} but response {n - 1} handed back "
            f"{server.responses[n - 1].get('nextPageToken')!r} — the cursor did not advance"
        )
    # And the tokens are all distinct: a re-sent token is a stalled walk, not progress.
    sent = [b.get("nextPageToken") for b in server.bodies[1:]]
    assert len(set(sent)) == len(sent), f"{method} re-sent a cursor token: {sent}"


@pytest.mark.parametrize("method", sorted(_READERS))
def test_a_short_page_does_not_terminate_the_walk(method: str) -> None:
    """THE DC BUG, TRANSPLANTED. A server capping every page below the requested size
    (a lowered ``jira.search.views.default.max``) is exactly what made DC's reader treat a
    short first page as "that is all there is". Cloud must key termination on the CURSOR,
    never on ``len(page) < requested``.
    """
    out, server = _run(method, page_cap=_SHORT_PAGE_CAP)
    assert len(out) == _SEEDED_ISSUES, (
        f"{method} recovered {len(out)} of {_SEEDED_ISSUES} against a server capping pages "
        f"at {_SHORT_PAGE_CAP}: a SHORT page was mistaken for the LAST page"
    )
    assert len(server.bodies) == _SHORT_PAGE_COUNT, (
        f"{method} issued {len(server.bodies)} requests; {_SEEDED_ISSUES} items at "
        f"{_SHORT_PAGE_CAP}/page needs {_SHORT_PAGE_COUNT}"
    )


# ---------------------------------------------------------------------------
# AC 3 — BOTH documented stop conditions, asserted independently
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", sorted(_READERS))
def test_islast_alone_terminates_the_walk(method: str) -> None:
    """Condition one, in isolation: the final page sets ``isLast: True`` AND still hands
    back a live ``nextPageToken``. A reader that only checked the token would page past the
    end (here: refetch forever, since the server keeps serving that offset), so this
    isolates ``isLast`` as sufficient.
    """
    out, server = _run(method, terminator="isLast")
    assert len(out) == _SEEDED_ISSUES, (
        f"{method} recovered {len(out)} of {_SEEDED_ISSUES} — truncated before isLast"
    )
    assert server.responses[-1].get("isLast") is True
    assert server.responses[-1].get("nextPageToken"), (
        "test setup: the final page must still offer a token, or this proves nothing"
    )
    assert len(server.bodies) == _EXPECTED_PAGES, (
        f"{method} kept paging past isLast: {len(server.bodies)} requests"
    )


@pytest.mark.parametrize("method", sorted(_READERS))
@pytest.mark.parametrize("terminator", ["absent-token", "null-token"])
def test_an_exhausted_cursor_alone_terminates_the_walk(method: str, terminator: str) -> None:
    """Condition two, in isolation: the final page carries NO ``isLast`` — the token is
    absent (or explicitly null). A reader that only checked ``isLast`` would spin. Both
    spellings are covered because the API documents the token as optional, and ``None`` and
    "missing" take different branches in a naive ``resp["nextPageToken"]`` read.
    """
    out, server = _run(method, terminator=terminator)
    assert len(out) == _SEEDED_ISSUES, f"{method}/{terminator} recovered {len(out)}"
    assert not server.responses[-1].get("isLast"), (
        "test setup: the final page must NOT set isLast, or this proves nothing"
    )
    assert len(server.bodies) == _EXPECTED_PAGES, (
        f"{method} issued {len(server.bodies)} requests for {_EXPECTED_PAGES} pages: an "
        f"exhausted ({terminator}) cursor did NOT stop the walk — it kept re-requesting, "
        f"which on a live server is an unbounded spin against Jira"
    )


@pytest.mark.parametrize("method", sorted(_READERS))
def test_an_empty_project_yields_an_empty_map_without_raising(method: str) -> None:
    """An empty project is a normal state, not a truncation."""
    out, server = _run(method, total=0)
    assert out == {}
    assert len(server.bodies) == 1, (
        f"{method} issued {len(server.bodies)} requests against an EMPTY project; one "
        f"empty page is the end of the walk, not a reason to keep asking"
    )


# ---------------------------------------------------------------------------
# AC 4 — a repeated cursor token must not spin forever
# ---------------------------------------------------------------------------


class _RepeatTokenServer:
    """Hands back the SAME cursor token forever — the "same-token-twice" cursor stall the
    fetcher raises ``SilentTruncationError`` on. Self-limits at ``_RUNAWAY_CALL_CAP`` so a
    spinning reader fails the test rather than hanging CI.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.hard_stopped = False

    def __call__(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.calls > _RUNAWAY_CALL_CAP:
            self.hard_stopped = True
            raise RuntimeError("runaway cursor: harness stop, the reader never gave up")
        return {
            "issues": [{"key": "DIG-1", "fields": {"parent": {"key": "DIG-EPIC-1"}}}],
            "nextPageToken": "same-token-forever",
        }


@pytest.mark.parametrize("method", sorted(_READERS))
def test_a_repeated_cursor_token_stops_the_walk_on_its_own(method: str) -> None:
    """Formerly the strict-xfail for [rebar:cabc-7a98-d173-4d7c]; the guard landed with
    [rebar:ab7f-f0cc-7384-43a7]. A reader that sees the same non-null cursor twice
    consecutively has learned the walk is stalled and must stop itself within two
    requests, WITHOUT the harness having to break the spin — and it must stop LOUDLY:
    ``RunawayPaginationError`` escapes the fail-open handler rather than degrading to a
    silent partial map, because a stalled cursor IS the silent-truncation defect class
    this module exists to pin.
    """
    server = _RepeatTokenServer()
    with pytest.raises(RunawayPaginationError):
        getattr(_client(server), method)("DIG")
    assert not server.hard_stopped, (
        f"the reader never stopped on a repeated cursor: it issued {server.calls} requests "
        f"and only the harness's hard stop ended the spin"
    )
    assert server.calls <= 2, (
        f"a stalled cursor should end the walk within two requests; got {server.calls}"
    )


class _FaultOnSecondCallServer:
    """Serves one good page (with a live cursor), then raises an ordinary transport
    fault — the mid-walk failure the readers' fail-open contract degrades around.
    """

    def __init__(self, fields_for: Any) -> None:
        self.fields_for = fields_for
        self.calls = 0

    def __call__(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            return {
                "issues": [{"key": "DIG-0", "fields": self.fields_for(0)}],
                "nextPageToken": "cursor:1",
            }
        raise RuntimeError("transport fault mid-walk")


@pytest.mark.parametrize("method", sorted(_READERS))
def test_an_ordinary_mid_walk_fault_still_degrades_open(method: str) -> None:
    """The runaway raise must not have NARROWED fail-open: any other mid-walk transport
    fault still degrades gracefully — no raise, and the pages recovered before the fault
    are returned (the documented partial-map degradation, ticket 8b25).
    """
    _field, fields_for, expected = _READERS[method]
    server = _FaultOnSecondCallServer(fields_for)
    out = getattr(_client(server), method)("DIG")
    assert server.calls == 2, f"expected the walk to hit the fault on call 2; {server.calls}"
    assert out == {"DIG-0": expected(0)}, (
        f"{method} must keep page-1 results when a later page faults; got {out!r}"
    )


# ---------------------------------------------------------------------------
# The shared cursor walk itself — the ONE loop all three readers route through
# ---------------------------------------------------------------------------


def _iter_body(field: str) -> dict[str, Any]:
    return {"jql": "project = DIG", "maxResults": _EXPECTED_PAGE_SIZE, "fields": [field]}


def test_the_shared_walk_yields_every_page_and_echoes_the_cursor() -> None:
    server = _CursorServer(_parent_fields)
    pages = list(_iter_cursor_pages(server, _SEARCH_JQL_PATH, _iter_body("parent")))
    assert pages == server.responses
    assert len(pages) == _EXPECTED_PAGES
    assert "nextPageToken" not in server.bodies[0]
    for i in range(1, len(server.bodies)):
        assert server.bodies[i]["nextPageToken"] == server.responses[i - 1]["nextPageToken"]


@pytest.mark.parametrize("terminator", ["isLast", "absent-token", "null-token"])
def test_the_shared_walk_stops_on_either_documented_terminator(terminator: str) -> None:
    server = _CursorServer(_parent_fields, terminator=terminator)
    pages = list(_iter_cursor_pages(server, _SEARCH_JQL_PATH, _iter_body("parent")))
    assert len(pages) == _EXPECTED_PAGES
    assert len(server.bodies) == _EXPECTED_PAGES


def test_the_shared_walk_handles_a_single_page_project() -> None:
    server = _CursorServer(_parent_fields, total=5)
    pages = list(_iter_cursor_pages(server, _SEARCH_JQL_PATH, _iter_body("parent")))
    assert len(pages) == 1
    assert len(server.bodies) == 1


def test_the_shared_walk_raises_on_a_repeated_cursor_token() -> None:
    server = _RepeatTokenServer()
    with pytest.raises(RunawayPaginationError):
        list(_iter_cursor_pages(server, _SEARCH_JQL_PATH, _iter_body("parent")))
    assert server.calls == 2, f"the stall is provable at request 2; walked {server.calls}"


def test_the_shared_walk_lets_transport_faults_propagate() -> None:
    """Degradation POLICY (410-loud, warn-and-degrade, partial map) belongs to each
    reader's handler, not the shared walk — so the walk must not swallow anything.
    """

    def boom(path: str, body: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("transport fault")

    with pytest.raises(RuntimeError, match="transport fault"):
        list(_iter_cursor_pages(boom, _SEARCH_JQL_PATH, _iter_body("parent")))


def test_the_shared_walk_ends_quietly_on_a_non_dict_response() -> None:
    server_calls: list[int] = []

    def malformed(path: str, body: dict[str, Any]) -> Any:
        server_calls.append(1)
        return ["not", "a", "dict"]

    pages = list(_iter_cursor_pages(malformed, _SEARCH_JQL_PATH, _iter_body("parent")))
    assert pages == []
    assert len(server_calls) == 1


def test_all_three_readers_route_through_the_shared_walk() -> None:
    """STRUCTURAL: zero hand-rolled cursor loops remain in the readers. Each reader's
    body calls ``_iter_cursor_pages`` and contains no ``while`` — a fourth copy of the
    walk is where all three historical truncation bugs lived, so hand-rolling one back
    into a reader is itself the regression.
    """
    src = dict(_scanned_sources())["acli_graph.py"]
    tree = ast.parse(src)
    readers = {"get_parent_map", "get_comment_map", "get_issuelinks_map"}
    seen: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name not in readers:
            continue
        seen.add(fn.name)
        calls_shared_walk = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_iter_cursor_pages"
            for n in ast.walk(fn)
        )
        assert calls_shared_walk, f"{fn.name} no longer routes through _iter_cursor_pages"
        hand_loops = [n for n in ast.walk(fn) if isinstance(n, ast.While)]
        assert not hand_loops, (
            f"{fn.name} grew a hand-rolled while-loop back (line {hand_loops[0].lineno})"
        )
    assert seen == readers, f"structural scan missed reader(s): {sorted(readers - seen)}"


# ---------------------------------------------------------------------------
# AC 6 — the structural guard: a FOURTH unpaginated whole-project reader cannot be
# added silently on Cloud either (DC's equivalent guard scans only the DC transport)
# ---------------------------------------------------------------------------

_SEARCH_JQL_PATH = "/rest/api/3/search/jql"


def _scanned_sources() -> list[tuple[str, str]]:
    """The (name, source) pairs the structural guard actually reads.

    Shared with the coverage test below so a glob or path that drifted — which would make
    the guard pass VACUOUSLY, scanning nothing — is caught rather than being invisible.
    """
    return [(p.name, p.read_text()) for p in sorted(_JIRA_ADAPTER_DIR.glob("*.py"))]


def _unpaginated_search_jql_readers(src: str | None = None) -> list[str]:
    """Every function that POSTs to ``/rest/api/3/search/jql`` without a cursor loop.

    A whole-project read is paginated iff its function body (a) loops and (b) mentions
    ``nextPageToken`` — the only cursor this endpoint offers. An AST scan rather than a
    grep, because this module's own prose names both repeatedly.

    ``src`` exists ONLY so the teeth test below can aim this same predicate at a synthetic
    offender; omitting it scans the live Cloud adapter, which is what the real guard does.
    """
    sources = [("<synthetic>", src)] if src is not None else _scanned_sources()

    offenders: list[str] = []
    for name, text in sources:
        tree = ast.parse(text)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            posts_search = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith("_direct_rest_post")
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == _SEARCH_JQL_PATH
                for node in ast.walk(fn)
            )
            if not posts_search:
                continue
            loops = any(isinstance(n, ast.While | ast.For) for n in ast.walk(fn))
            cursors = any(
                isinstance(n, ast.Constant) and n.value == "nextPageToken" for n in ast.walk(fn)
            )
            if not (loops and cursors):
                offenders.append(f"{name}:{fn.name} (line {fn.lineno})")
    return offenders


def test_no_cloud_whole_project_reader_skips_the_cursor_loop() -> None:
    """THE CRITERION THAT CONVERTS "I tested three" INTO "a fourth cannot be added
    silently". DC's structural guard scans the DC transport source only, so this seam was
    open on Cloud; with both in place the defect class is closed on EITHER deployment.
    """
    offenders = _unpaginated_search_jql_readers()
    assert not offenders, (
        f"function(s) in adapters/jira POST {_SEARCH_JQL_PATH} without a nextPageToken "
        f"cursor loop and will silently return only the FIRST page: {offenders}"
    )


_SYNTHETIC_OFFENDER_SRC = """\
class C:
    def unpaginated(self, project):
        resp = self._direct_rest_post_json("/rest/api/3/search/jql", {"jql": project})
        return resp["issues"]

    def paginated(self, project):
        token = None
        while True:
            body = {"jql": project}
            if token is not None:
                body["nextPageToken"] = token
            resp = self._direct_rest_post_json("/rest/api/3/search/jql", body)
            if resp.get("isLast"):
                break
            token = resp.get("nextPageToken")
            if not token:
                break
        return resp

    def other_endpoint(self, key):
        return self._direct_rest_post_json(f"/rest/api/3/issue/{key}/comment", {})
"""


def test_the_structural_guard_fires_on_a_synthetic_unpaginated_reader() -> None:
    """TEETH for the guard itself: a structural test that cannot fail is decoration.

    Drives the SAME predicate the real guard consumes (no second inline copy of the AST
    walk — that duplication is what lets a guard rot unnoticed) and asserts it names the
    offending function. ``paginated`` and ``other_endpoint`` are the negative controls:
    narrowing the predicate so it flags either turns this red.
    """
    offenders = _unpaginated_search_jql_readers(_SYNTHETIC_OFFENDER_SRC)
    assert offenders == ["<synthetic>:unpaginated (line 2)"], (
        f"the shared AST predicate must report exactly the cursor-less reader on line 2 "
        f"and neither the paginated reader nor the non-search POST; got {offenders!r}"
    )


def test_the_structural_guard_actually_sees_the_three_known_readers() -> None:
    """COVERAGE TEETH. A predicate whose glob or path had drifted would scan nothing and
    the guard above would pass vacuously. Pin that the scan reaches the three readers this
    ticket is about — they are paginated, so they must be found-and-accepted, and the file
    they live in must exist where the guard looks.
    """
    scanned = dict(_scanned_sources())
    assert "acli_graph.py" in scanned, (
        f"the guard's own source list does not include acli_graph.py — it scanned "
        f"{sorted(scanned)}, so the whole guard is VACUOUS"
    )
    src = scanned["acli_graph.py"]
    assert _SEARCH_JQL_PATH in src, (
        f"the guard scans {_JIRA_ADAPTER_DIR}/acli_graph.py but that file no longer POSTs "
        f"{_SEARCH_JQL_PATH} — the whole-project readers moved and the guard is now blind"
    )
    tree = ast.parse(src)
    found = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        and any(isinstance(n, ast.Constant) and n.value == _SEARCH_JQL_PATH for n in ast.walk(fn))
    }
    assert {"get_parent_map", "get_comment_map", "get_issuelinks_map"} <= found, (
        f"expected all three whole-project readers in acli_graph.py; found {sorted(found)}"
    )
