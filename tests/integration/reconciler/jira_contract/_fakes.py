"""Replay scrubbed Jira fixtures through production fetch and enrichment. The fake
exposes the real client signatures, returns captured shapes without reshaping, and
isolates callers with deep copies.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

# tests/integration/reconciler/jira_contract/_fakes.py -> parents[3] == tests/
FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "jira"


class FakeAcliClient:
    """Match the real client constructor and method signatures. Return deep copies of
    captured payloads so one test cannot mutate another test's fixture state.
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


def install(monkeypatch: Any, fetcher_mod: Any, fixture_dir: Path | None = None) -> None:
    """Inject the fake transport through fetcher's production _load_acli seam.
    Production snapshot enrichment then merges the captured parent, comment, and
    link shapes without a parallel serialization path.
    """
    monkeypatch.setattr(fetcher_mod, "_load_acli", lambda: FakeAcliClient(fixture_dir))
