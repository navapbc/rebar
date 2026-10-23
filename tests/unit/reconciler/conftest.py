"""Fixtures for reconciler unit tests and three conflict-resolution classes.

Autouse fixtures sandbox REBAR_ROOT and pin DIG plus Cloud credentials. Tests
may override those defaults. State, additive, and set divergence fixtures model
conflict classes. The ACLI stub supplies capped paginated JQL pages."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _sandbox_repo_root(tmp_path, monkeypatch):
    """Redirect the reconciler's repo-root fallback to a per-test temp dir.

    Without REBAR_ROOT set, the source falls back to ``Path(__file__).parents[4]``
    which resolves to the rebar repo root — so any test that exercises a real
    dispatch/CLI path would create ``.tickets-tracker`` in the working tree and
    trip the repo-root leak guard. Tests that pass an explicit ``repo_root``
    argument are unaffected (the source prefers the argument over the env).
    """
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    return


@pytest.fixture(autouse=True)
def _default_jira_project(monkeypatch):
    """Pin ``JIRA_PROJECT=DIG`` for reconciler unit tests (overridable per-test).

    Bug 626d made the configured project load-bearing: the inbound JQL is now
    scoped to ``jira.project`` (an empty key raises), and the outbound applier
    refuses to target a project other than the configured one. These legacy tests
    use DIG-keyed fixtures and were written against the historical implicit
    ``project_default="DIG"``. Config discovery would otherwise pick up whatever
    ``[jira] project`` the repo's ``rebar.toml`` carries (REB), breaking
    hermeticity. The env override (env > config) pins DIG deterministically; a test
    that needs another project sets ``JIRA_PROJECT`` itself (it runs after this
    fixture and wins).

    Bug ad85 additionally made Cloud credentials load-bearing: ``_build_jira_backend``
    now fails loudly (``BackendEnvError``) when ``JIRA_URL`` / ``JIRA_USER`` /
    ``JIRA_API_TOKEN`` are absent or ``JIRA_USER`` is not an email, at parity with the
    DC ``JIRA_PAT`` guard — rather than building an anonymous client that 401s later.
    Nearly every reconcile path builds the (default Cloud) backend — the applier and
    the outbound differ read ``select_backend(...).project`` — so pin hermetic, valid
    creds here just as ``JIRA_PROJECT`` is pinned. A test exercising the absent/invalid
    -credential path deletes/overrides these itself (it runs after this fixture).
    """
    monkeypatch.setenv("JIRA_PROJECT", "DIG")
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_USER", "reconciler-tests@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-api-token")
    return


@pytest.fixture
def state_divergence():
    """State-class divergence: local 'In Progress', remote 'Done'."""
    return {"field": "status", "local": "In Progress", "remote": "Done"}


@pytest.fixture
def additive_divergence():
    """Additive-class divergence: local description A, remote description B."""
    return {
        "field": "description",
        "local": "Local description content A",
        "remote": "Remote description content B",
    }


@pytest.fixture
def set_divergence():
    """Set-class divergence: local {X,Y}, remote {Y,Z}."""
    return {
        "field": "labels",
        "local": ["X", "Y"],
        "remote": ["Y", "Z"],
        "expected_union": {"X", "Y", "Z"},
    }


@pytest.fixture
def paginating_acli_stub():
    """Return a factory that produces a callable simulating ACLI paginated JQL fetch.

    The factory accepts:
      - pages: a list of issue-dicts (the full working set, in canonical order)
      - max_results_cap: integer; the ACLI behaviour at boundaries (default 100)

    Returns a callable stub(jql, start_at, max_results) -> dict with shape:
        {
          "issues": [...],          # the slice of `pages` between start_at and start_at+max_results
          "startAt": start_at,
          "maxResults": effective_max,  # min(max_results, max_results_cap)
          "total": len(pages),
        }
    Slicing follows real ACLI: start_at out-of-range returns empty issues list
    with total=len(pages).
    """

    def _factory(pages, max_results_cap=100):
        def _stub(jql, start_at=0, max_results=100):
            effective_max = min(max_results, max_results_cap)
            slice_ = pages[start_at : start_at + effective_max]
            return {
                "issues": slice_,
                "startAt": start_at,
                "maxResults": effective_max,
                "total": len(pages),
            }

        return _stub

    return _factory
