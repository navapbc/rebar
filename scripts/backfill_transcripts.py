#!/usr/bin/env python
"""Backfill env/integration-diagnosis signatures from Claude-Code transcripts.

This is the persistence path for the isolated Claude-transcript mining adapter
(ticket 538c). It globs ``*.jsonl`` transcripts under a directory, runs each
through the deterministic classifier in
``rebar.metrics.adapters.claude_transcripts`` (no LLM, no network), and returns
the concatenated classified records.

The core ``rebar.metrics`` package does **not** import this script or the
adapter — this script is the only caller of the adapter besides its tests.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from rebar.metrics.adapters.claude_transcripts import mine_transcript


def backfill(transcript_dir: str) -> list[dict]:
    """Mine every ``*.jsonl`` transcript under ``transcript_dir``.

    Globs ``*.jsonl`` files (sorted for determinism), runs each through
    :func:`rebar.metrics.adapters.claude_transcripts.mine_transcript`, and
    returns the concatenated list of classified records.
    """
    records: list[dict] = []
    for path in sorted(Path(transcript_dir).glob("*.jsonl")):
        records.extend(mine_transcript(str(path)))
    return records


def main() -> int:
    default_dir = os.path.expanduser("~/.claude/projects")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "transcript_dir",
        nargs="?",
        default=default_dir,
        help="directory of *.jsonl Claude Code transcripts",
    )
    args = parser.parse_args()

    records = backfill(args.transcript_dir)
    for record in records:
        print(json.dumps(record))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
