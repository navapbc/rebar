"""Whitespace-only title rejection on the create/edit paths (story 5977).

Policy (committed): a title is rejected iff ``title.strip()`` is empty — so ``""``,
spaces, tabs, and newlines are all refused — while a title that merely has SURROUNDING
whitespace around real content (e.g. ``" hi "``) is ACCEPTED and stored **as-is**
(validation must never silently trim the stored value).

This module pins the core behavior on the library and CLI create/edit surfaces. The
full cross-facade corpus (byte-identity round-trip, NFC/NFD, the U+2192 exception,
>255 rejection, NUL) lives in ``test_title_corpus.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import rebar

WHITESPACE_ONLY = ["   ", "\t", "\n", "\r\n", " \t \n ", ""]


def _cli(*args: str, repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("title", WHITESPACE_ONLY)
def test_create_rejects_whitespace_only_title_library(rebar_repo: Path, title: str) -> None:
    with pytest.raises(rebar.RebarError) as ei:
        rebar.create_ticket("task", title, repo_root=str(rebar_repo))
    assert "non-empty" in str(ei.value).lower(), str(ei.value)


@pytest.mark.parametrize("title", WHITESPACE_ONLY)
def test_edit_rejects_whitespace_only_title_library(rebar_repo: Path, title: str) -> None:
    tid = rebar.create_ticket("task", "real title", repo_root=str(rebar_repo))
    with pytest.raises(rebar.RebarError) as ei:
        rebar.edit_ticket(tid, title=title, repo_root=str(rebar_repo))
    assert "non-empty" in str(ei.value).lower(), str(ei.value)
    # The stored title is untouched by the rejected edit.
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["title"] == "real title"


def test_create_rejects_whitespace_only_title_cli(rebar_repo: Path) -> None:
    cp = _cli("create", "task", "   ", repo=rebar_repo)
    assert cp.returncode != 0, cp.stdout
    assert "non-empty" in cp.stderr.lower(), cp.stderr


def test_surrounding_whitespace_with_content_is_accepted_and_stored_as_is(
    rebar_repo: Path,
) -> None:
    """A title with real content but surrounding whitespace is accepted and stored
    byte-identically — the rejection uses ``strip()`` only for the emptiness CHECK, it
    must NOT mutate the stored value."""
    tid = rebar.create_ticket("task", " hi ", repo_root=str(rebar_repo))
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["title"] == " hi "

    # And an edit to a surrounded-but-nonempty title is likewise preserved verbatim.
    rebar.edit_ticket(tid, title="\tkept\n", repo_root=str(rebar_repo))
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["title"] == "\tkept\n"
