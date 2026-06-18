"""WS-A: the fs/repo cluster extraction from runner.py to llm/fs_tools.py.

Pins the structural contract of the extraction (no behavior change): the cluster
lives in fs_tools, runner re-uses it, and fs_tools stays import-light (no heavy
`agents`-extra import at module load — langchain is imported inside
_filesystem_tools)."""

from __future__ import annotations

import sys


def test_fs_tools_exposes_the_cluster() -> None:
    from rebar.llm import fs_tools

    for sym in (
        "_safe_path",
        "_git_tracked",
        "_discovery_filter",
        "_within_root",
        "_filesystem_tools",
        "_READ_MAX_LINES",
        "_NOISE_DIRS",
        "_NOISE_SUFFIXES",
    ):
        assert hasattr(fs_tools, sym), f"fs_tools missing extracted symbol {sym}"


def test_runner_uses_the_extracted_tools() -> None:
    from rebar.llm import fs_tools, runner

    assert runner._filesystem_tools is fs_tools._filesystem_tools


def test_importing_fs_tools_does_not_pull_langchain() -> None:
    # The optionality invariant: importing the module must not import the heavy
    # agents extra (langchain is lazily imported inside _filesystem_tools).
    for mod in [m for m in list(sys.modules) if m.startswith(("langchain", "langgraph"))]:
        del sys.modules[mod]
    import rebar.llm.fs_tools  # noqa: F401

    assert not any(m.startswith(("langchain", "langgraph")) for m in sys.modules), (
        "importing rebar.llm.fs_tools pulled in the agents extra"
    )
