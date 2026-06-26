"""Verified-fake Jira client that replays captured fixtures through production.

Story A (fe57-7712-1e3f-45f4) of epic f89d. ``FakeAcliClient`` serves the
scrubbed real Jira payloads under ``tests/fixtures/jira/`` from the SAME method
signatures the production ``AcliClient`` exposes (``search_issues``,
``get_comment_map``, ``get_issuelinks_map``, ``get_parent_map``) — returning them
**verbatim, with NO shape massaging**. This is the foundation the snapshot
contract test (story B), the verified-fake honesty harness (story C), and the
hermetic round-trip probe (story F) build on.

The fake is installed by the established codebase seam: patch
``fetcher._load_acli`` to return a stub *module* whose ``AcliClient(**kwargs)``
factory yields the fake (see :func:`install`). The fixtures are then consumed
ONLY by driving ``fetcher.fetch_snapshot`` / ``fetcher.compute_snapshot``, which
routes them through the production parent/comment/issuelinks enrichment merge —
exactly where bugs 0ee6 (nested ``comment`` vs flat ``comments``) and 3f04
(absent ``issuelinks``) lived. No test reads the fixtures and hand-reshapes them.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

# tests/integration/rebar_reconciler/jira_contract/_fakes.py -> parents[3] == tests/
FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "jira"


class FakeAcliClient:
    """Replays captured Jira fixtures via the production method signatures.

    Constructed exactly like the real ``AcliClient`` (``jira_url`` / ``user`` /
    ``api_token`` kwargs, all ignored) so the ``_load_acli`` stub-module factory
    can build it transparently. Every method returns a deep copy of the fixture
    so a consumer that mutates the merged snapshot cannot bleed into the next
    test — the SHAPE returned is the fixture's, untouched.
    """

    def __init__(self, fixture_dir: Path | None = None, **_kwargs: Any) -> None:
        d = fixture_dir or FIXTURE_DIR
        self._search: list[dict[str, Any]] = json.loads((d / "search.json").read_text())
        self._comment_map: dict[str, Any] = json.loads((d / "comment_map.json").read_text())
        self._issuelinks_map: dict[str, Any] = json.loads((d / "issuelinks_map.json").read_text())
        self._parent_map: dict[str, Any] = json.loads((d / "parent_map.json").read_text())

    # --- production AcliClient surface (signatures mirror the real client) ---

    def search_issues(
        self, jql: str, start_at: int = 0, max_results: int = 50
    ) -> list[dict[str, Any]]:
        """Return a page slice, mirroring the real client's pagination contract."""
        return copy.deepcopy(self._search[start_at : start_at + max_results])

    def get_comment_map(self, project: str, jql: str | None = None) -> dict[str, Any]:
        return copy.deepcopy(self._comment_map)

    def get_issuelinks_map(self, project: str, jql: str | None = None) -> dict[str, Any]:
        return copy.deepcopy(self._issuelinks_map)

    def get_parent_map(self, project: str, jql: str | None = None) -> dict[str, Any]:
        return copy.deepcopy(self._parent_map)


class _FakeAcliModule:
    """Stand-in for the ``rebar_reconciler.acli`` module.

    The fetcher builds its client via ``acli_mod.AcliClient(**kwargs)`` (it
    accepts no injected client). Patching ``fetcher._load_acli`` to return this
    module makes ``_build_snapshot`` construct the fake — the only seam that
    drives the fixtures through the production enrichment merge.
    """

    def __init__(self, fixture_dir: Path | None = None) -> None:
        self._fixture_dir = fixture_dir

    def AcliClient(self, **kwargs: Any) -> FakeAcliClient:  # noqa: N802 — mirrors real factory name
        return FakeAcliClient(fixture_dir=self._fixture_dir, **kwargs)


def install(monkeypatch: Any, fetcher_mod: Any, fixture_dir: Path | None = None) -> None:
    """Route ``fetcher_mod`` through the fake by patching ``_load_acli``.

    This is the production seam: after this call, ``fetcher.fetch_snapshot`` /
    ``fetcher.compute_snapshot`` construct a :class:`FakeAcliClient` and merge its
    fixtures via the real parent/comment/issuelinks enrichment path — no parallel
    serialization, no hand-reshaping.
    """
    monkeypatch.setattr(fetcher_mod, "_load_acli", lambda: _FakeAcliModule(fixture_dir))
