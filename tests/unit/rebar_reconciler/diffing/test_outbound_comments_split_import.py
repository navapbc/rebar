"""Regression guard: the outbound differ's per-concern comment split stays importable.

The comment-diff cluster was split out of ``outbound_differ.py`` into
``outbound_comments.py`` (commit 3d8be7fec); ``outbound_differ`` re-exports its public
names via ``from rebar_reconciler.outbound_comments import ...``. If that sibling module
is ever deleted/renamed without updating the importer, importing ``outbound_differ`` raises
``ModuleNotFoundError: No module named 'rebar_reconciler.outbound_comments'`` — the exact
signature reported in bug fuss-chapel-ulna (which reproduced only as a stale-worktree
artifact, never on current main). This pins the split so a *real* future removal fails
loudly and locally here, instead of resurfacing as a confusing collection error elsewhere.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
ENGINE_DIR = REPO_ROOT / "src" / "rebar" / "_engine"

# Names outbound_differ re-exports from the outbound_comments split; each must keep
# resolving on ``outbound_differ.<name>`` for the comment-diff seam and its callers.
_REEXPORTED = (
    "RECONCILER_MARKER",
    "_decorate_outbound_comment",
    "_diff_comments",
    "_map_comments_for_create",
    "_normalize_comment_body",
)


def test_outbound_comments_split_is_importable_via_differ() -> None:
    # The engine ships at <repo>/src/rebar/_engine and is imported as the
    # ``rebar_reconciler`` package root; ensure it is importable in isolation, not just
    # when a sibling conftest happens to have seeded sys.path.
    if str(ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(ENGINE_DIR))

    # This is the import that failed in the report — it transitively pulls
    # rebar_reconciler.outbound_comments. A missing split module raises here.
    od = importlib.import_module("rebar_reconciler.outbound_differ")

    missing = [name for name in _REEXPORTED if not hasattr(od, name)]
    assert not missing, (
        f"outbound_differ lost re-exported comment-split names {missing}; the "
        "rebar_reconciler.outbound_comments split may have been removed/renamed "
        "without updating outbound_differ's import"
    )
