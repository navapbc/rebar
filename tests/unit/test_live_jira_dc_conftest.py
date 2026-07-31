"""Unit coverage for the live_jira_dc harness fixtures (bug f391, epic e369).

WHY THESE ARE UNIT TESTS. The behaviour under test lives in a LIVE-tier conftest
that cannot run on every workstation — the harness base image is linux/amd64 only
(``tests/external/live_jira_dc/README.md``) — and the defect it fixes only fires
after 10 accumulated tokens. Waiting for the live tier to prove the sweep is
correct means the sweep's own failure modes (deleting a human's PAT; raising when
the endpoint is unavailable) would first be observed against a real instance. So
the PARSING and DEGRADATION paths are pinned here, against a stubbed ``_request``,
and the live job supplies the acceptance evidence that the cap is no longer hit.

The module is loaded by PATH rather than imported as ``conftest``: pytest owns
that name, and a second module claiming it collides.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_CONFTEST = pathlib.Path(__file__).resolve().parents[1] / "external/live_jira_dc/conftest.py"


def _load_harness_conftest():
    spec = importlib.util.spec_from_file_location("_live_jira_dc_conftest_under_test", _CONFTEST)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return _load_harness_conftest()


# ---------------------------------------------------------------------------
# The scope fix itself — the actual bug
# ---------------------------------------------------------------------------


def test_jira_dc_pat_is_session_scoped(harness) -> None:
    """THE BUG. Function scope mints one PAT per requesting test against a fixed
    budget of 10, so the suite dies at setup — in whatever module happened to run
    eleventh. Session scope makes consumption O(1) in test count, which is what
    makes the headroom structural instead of a coincidence of today's count."""
    # pytest 8.4+ exposes the decorator's arguments on ``_fixture_function_marker``
    # (older releases used ``_pytestfixturefunction``); read whichever is present so
    # this pins the SCOPE rather than a pytest internal's spelling.
    marker = getattr(
        harness.jira_dc_pat,
        "_fixture_function_marker",
        getattr(harness.jira_dc_pat, "_pytestfixturefunction", None),
    )
    assert marker is not None, "jira_dc_pat is not a pytest fixture at all"
    scope = marker.scope
    assert scope == "session", (
        f"jira_dc_pat is {scope!r}-scoped: it mints a NEW Jira DC Personal Access "
        f"Token per requesting test and never deletes it, so the suite exhausts "
        f"DC's 10-token-per-user cap and dies at setup"
    )


def test_the_pat_fixture_documents_the_ten_token_cap(harness) -> None:
    """The cap is a non-obvious DC limit with no Cloud analogue. Undocumented, the
    next author widens the scope back out and re-creates the bug."""
    doc = harness.jira_dc_pat.__doc__ or ""
    assert "10" in doc and "SESSION" in doc.upper(), (
        "the fixture docstring does not record the 10-token cap and the session-scope "
        "decision, so the reason for the scope is invisible at the point of change"
    )


# ---------------------------------------------------------------------------
# The sweep — parsing
# ---------------------------------------------------------------------------


def _stub_request(harness, monkeypatch, *, list_response, deleted=None):
    """Replace the module's single HTTP helper. Every call routes through
    ``_request``, so this is the whole network surface."""
    calls: list[tuple] = []

    def _fake(path, *, method="GET", payload=None, token=None, basic_auth=None, timeout=30):
        calls.append((method, path))
        if method == "DELETE":
            return (deleted if deleted is not None else 204), None
        return list_response

    monkeypatch.setattr(harness, "_request", _fake)
    return calls


def test_the_sweep_finds_leftover_harness_tokens(harness, monkeypatch) -> None:
    """A token left by a crashed run must be found so its budget can be reclaimed."""
    _stub_request(
        harness,
        monkeypatch,
        list_response=(
            200,
            [
                {"id": 1, "name": "rebar-j5-harness-aaaa1111"},
                {"id": 2, "name": "rebar-j5-harness-bbbb2222"},
            ],
        ),
    )
    found = harness._leaked_harness_tokens()
    assert sorted(t["name"] for t in found) == [
        "rebar-j5-harness-aaaa1111",
        "rebar-j5-harness-bbbb2222",
    ]


def test_the_sweep_never_touches_a_token_it_did_not_create(harness, monkeypatch) -> None:
    """TEETH, and the failure mode that would make this fix WORSE than the bug.

    The admin account may hold PATs a human created. A sweep that reclaimed budget
    by deleting those would destroy a credential the harness has no claim on. Only
    the ``rebar-j5-harness-`` prefix is ours."""
    _stub_request(
        harness,
        monkeypatch,
        list_response=(
            200,
            [
                {"id": 1, "name": "rebar-j5-harness-aaaa1111"},
                {"id": 2, "name": "my-personal-token"},
                {"id": 3, "name": "jenkins-deploy"},
                {"id": 4, "name": "rebar-j5-harness"},  # prefix-adjacent, not a match
            ],
        ),
    )
    found = harness._leaked_harness_tokens()
    assert [t["name"] for t in found] == ["rebar-j5-harness-aaaa1111"], (
        "the sweep selected a token it did not mint — it would delete a credential "
        "belonging to someone else"
    )


def test_a_token_without_an_id_is_skipped(harness, monkeypatch) -> None:
    """The creation response was only ever asserted to carry ``rawToken``; whether
    the LIST response always carries ``id`` is unverified against a live instance.
    An entry without one cannot be addressed by the DELETE endpoint, so it is
    skipped rather than used to build a malformed URL."""
    _stub_request(
        harness,
        monkeypatch,
        list_response=(200, [{"name": "rebar-j5-harness-aaaa1111"}]),
    )
    assert harness._leaked_harness_tokens() == []


# ---------------------------------------------------------------------------
# The sweep — degradation. A sweep that raises replaces one setup failure
# with another.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "list_response",
    [
        (403, {"message": "forbidden"}),
        (404, None),
        (500, "gateway blew up"),
        (200, {"unexpected": "shape"}),
        (200, None),
    ],
    ids=["forbidden", "missing-endpoint", "server-error", "non-list-body", "empty-body"],
)
def test_the_sweep_returns_empty_when_it_cannot_enumerate(
    harness, monkeypatch, list_response
) -> None:
    """Mirrors ``_leaked_scratch_projects``'s stated posture: cannot enumerate ⇒ do
    not invent a failure, and do not claim cleanliness either. Raising here would
    block every run against an instance whose PAT endpoint is unavailable — a
    reason wholly unrelated to the code under test."""
    _stub_request(harness, monkeypatch, list_response=list_response)
    assert harness._leaked_harness_tokens() == []
    assert harness._sweep_leaked_harness_tokens() == []


def test_a_failed_delete_does_not_abort_the_session(harness, monkeypatch) -> None:
    """Best-effort by design. If the reclaim was genuinely insufficient, the mint
    that follows fails with Jira's own explicit limit error — a better diagnostic
    than an assertion here, and one that cannot fire spuriously."""
    _stub_request(
        harness,
        monkeypatch,
        list_response=(200, [{"id": 1, "name": "rebar-j5-harness-aaaa1111"}]),
        deleted=403,
    )
    assert harness._sweep_leaked_harness_tokens() == []  # reported, not raised


def test_the_sweep_deletes_by_id_and_reports_what_it_reclaimed(harness, monkeypatch) -> None:
    """The reclaim must actually issue DELETEs against the token-id endpoint, and
    name what it swept — a silent sweep is indistinguishable from a no-op in the
    CI log, which is the only place this is ever diagnosed."""
    calls = _stub_request(
        harness,
        monkeypatch,
        list_response=(
            200,
            [
                {"id": 7, "name": "rebar-j5-harness-aaaa1111"},
                {"id": 9, "name": "rebar-j5-harness-bbbb2222"},
            ],
        ),
    )
    swept = harness._sweep_leaked_harness_tokens()
    assert sorted(swept) == ["rebar-j5-harness-aaaa1111", "rebar-j5-harness-bbbb2222"]
    deletes = [path for method, path in calls if method == "DELETE"]
    assert deletes == ["/rest/pat/latest/tokens/7", "/rest/pat/latest/tokens/9"]


def test_an_already_gone_token_counts_as_reclaimed(harness, monkeypatch) -> None:
    """404 means the budget is free, which is the postcondition this owes — the
    same reasoning ``track_issue`` already applies to a cascade-deleted issue."""
    _stub_request(
        harness,
        monkeypatch,
        list_response=(200, [{"id": 1, "name": "rebar-j5-harness-aaaa1111"}]),
        deleted=404,
    )
    assert harness._sweep_leaked_harness_tokens() == ["rebar-j5-harness-aaaa1111"]
