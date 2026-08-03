"""Hardened conflict bug filing (ticket 4527-0cfa-d31a-4a08).

The reconciler's deferred conflict filer historically created one bug per
conflict occurrence per pass with no dedup, no accumulation cap, no
abort-if-empty guard, and no provenance. These tests pin the hardened
contract of the extracted ``conflict_bug_filing`` module:

- **dedup**: a stable per-pair tag; an open bug carrying that tag absorbs
  repeat filings for the same (local_id, jira_key) pair.
- **accumulation**: an absorbed repeat posts at most one marker comment
  per 24h window.
- **abort-if-empty**: hollow payloads (no identifiers, or empty
  title/description) are refused loudly instead of filed.
- **provenance**: creates carry ``--detected-by reconciler-conflict``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
MODULE_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "conflict_bug_filing.py"
)

NOW = 1_785_800_000


def _load_module():
    spec = importlib.util.spec_from_file_location("conflict_bug_filing_4527", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["conflict_bug_filing_4527"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


class FakeRunner:
    """argv-prefix tuple -> (rc, stdout, stderr); records every call."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        for prefix, resp in self.responses.items():
            if tuple(argv[1 : 1 + len(prefix)]) == prefix:
                return resp
        return (0, "", "")

    def by_verb(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if len(c) > 1 and c[1] == verb]


def _pending(**over):
    base = {
        "title": "[Reconciler conflict]: pair ('local-9', 'DIG-9') -> divergence",
        "description": "Reconciler detected a conflict on (local-9, DIG-9).",
        "parent_id": "",
        "local_id": "local-9",
        "jira_key": "DIG-9",
    }
    base.update(over)
    return base


def _cli(tmp_path: Path) -> Path:
    cli = tmp_path / "rebar-cli"
    cli.write_text("#!/bin/sh\n")
    return cli


def _show_with_comments(*bodies_and_ages):
    comments = [{"body": body, "timestamp": (NOW - age) * 10**9} for body, age in bodies_and_ages]
    return (0, json.dumps({"ticket_id": "abcd-1111-2222-3333", "comments": comments}), "")


# ---------------------------------------------------------------------------
# create path
# ---------------------------------------------------------------------------


def test_files_bug_with_detected_by_and_dedup_tag(mod, tmp_path):
    runner = FakeRunner(
        {
            ("list",): (0, "[]", ""),
            ("create",): (0, "Created ticket alias (aaaa-1111-2222-3333): title\n", ""),
        }
    )
    result = mod.file_conflict_bug_ticket(_cli(tmp_path), _pending(), runner=runner, now_epoch=NOW)
    assert "aaaa-1111-2222-3333" in result
    creates = runner.by_verb("create")
    assert len(creates) == 1
    argv = creates[0]
    assert argv[2] == "bug"
    assert "--detected-by" in argv
    assert argv[argv.index("--detected-by") + 1] == "reconciler-conflict"
    assert "--tags" in argv
    tag = argv[argv.index("--tags") + 1]
    assert tag == mod.conflict_dedup_tag("local-9", "DIG-9")
    assert tag.startswith("conflict-") and len(tag) == len("conflict-") + 12


def test_parent_id_forwarded_when_present(mod, tmp_path):
    runner = FakeRunner({("list",): (0, "[]", "")})
    mod.file_conflict_bug_ticket(
        _cli(tmp_path), _pending(parent_id="pppp-1111-2222-3333"), runner=runner, now_epoch=NOW
    )
    argv = runner.by_verb("create")[0]
    assert "--parent" in argv
    assert argv[argv.index("--parent") + 1] == "pppp-1111-2222-3333"


def test_distinct_conflicts_get_distinct_tags(mod):
    tag_a = mod.conflict_dedup_tag("local-9", "DIG-9")
    tag_b = mod.conflict_dedup_tag("local-9", "DIG-10")
    tag_c = mod.conflict_dedup_tag("local-10", "DIG-9")
    assert len({tag_a, tag_b, tag_c}) == 3


def test_create_failure_returns_empty(mod, tmp_path):
    runner = FakeRunner(
        {
            ("list",): (0, "[]", ""),
            ("create",): (1, "", "boom"),
        }
    )
    result = mod.file_conflict_bug_ticket(_cli(tmp_path), _pending(), runner=runner, now_epoch=NOW)
    assert result == ""


def test_missing_cli_returns_empty_without_calls(mod, tmp_path):
    runner = FakeRunner()
    result = mod.file_conflict_bug_ticket(
        tmp_path / "no-such-cli", _pending(), runner=runner, now_epoch=NOW
    )
    assert result == ""
    assert runner.calls == []


# ---------------------------------------------------------------------------
# dedup + accumulation
# ---------------------------------------------------------------------------


def test_dedup_same_conflict_absorbs_into_open_ticket(mod, tmp_path):
    listing = json.dumps([{"ticket_id": "abcd-1111-2222-3333"}])
    runner = FakeRunner(
        {
            ("list",): (0, listing, ""),
            ("show",): _show_with_comments(),
        }
    )
    result = mod.file_conflict_bug_ticket(_cli(tmp_path), _pending(), runner=runner, now_epoch=NOW)
    assert result == "abcd-1111-2222-3333"
    assert runner.by_verb("create") == []
    comments = runner.by_verb("comment")
    assert len(comments) == 1
    assert comments[0][2] == "abcd-1111-2222-3333"
    assert comments[0][3].startswith("RECONCILER_CONFLICT:")
    # the dedup search used the pair tag
    listing_call = runner.by_verb("list")[0]
    assert f"--has-tag={mod.conflict_dedup_tag('local-9', 'DIG-9')}" in listing_call


def test_accumulation_within_24h_skipped(mod, tmp_path):
    listing = json.dumps([{"ticket_id": "abcd-1111-2222-3333"}])
    runner = FakeRunner(
        {
            ("list",): (0, listing, ""),
            ("show",): _show_with_comments(
                ("RECONCILER_CONFLICT: still unresolved earlier today", 3 * 3600)
            ),
        }
    )
    result = mod.file_conflict_bug_ticket(_cli(tmp_path), _pending(), runner=runner, now_epoch=NOW)
    assert result == "abcd-1111-2222-3333"
    assert runner.by_verb("create") == []
    assert runner.by_verb("comment") == []


def test_accumulation_after_24h_comments_again(mod, tmp_path):
    listing = json.dumps([{"ticket_id": "abcd-1111-2222-3333"}])
    runner = FakeRunner(
        {
            ("list",): (0, listing, ""),
            ("show",): _show_with_comments(
                ("RECONCILER_CONFLICT: still unresolved yesterday", 25 * 3600),
                ("unrelated operator comment", 60),
            ),
        }
    )
    mod.file_conflict_bug_ticket(_cli(tmp_path), _pending(), runner=runner, now_epoch=NOW)
    assert runner.by_verb("create") == []
    assert len(runner.by_verb("comment")) == 1


def test_dedup_search_failure_falls_back_to_create(mod, tmp_path):
    """A broken dedup search must not silently swallow the conflict — file anyway."""
    runner = FakeRunner(
        {
            ("list",): (1, "", "list exploded"),
            ("create",): (0, "Created ticket alias (bbbb-1111-2222-3333): t\n", ""),
        }
    )
    result = mod.file_conflict_bug_ticket(_cli(tmp_path), _pending(), runner=runner, now_epoch=NOW)
    assert "bbbb-1111-2222-3333" in result


# ---------------------------------------------------------------------------
# abort-if-empty
# ---------------------------------------------------------------------------


def test_abort_when_both_identifiers_empty(mod, tmp_path, capsys):
    runner = FakeRunner()
    result = mod.file_conflict_bug_ticket(
        _cli(tmp_path), _pending(local_id="", jira_key=""), runner=runner, now_epoch=NOW
    )
    assert result == ""
    assert runner.calls == []
    assert "refus" in capsys.readouterr().err.lower()


def test_abort_when_title_empty(mod, tmp_path, capsys):
    runner = FakeRunner()
    result = mod.file_conflict_bug_ticket(
        _cli(tmp_path), _pending(title="   "), runner=runner, now_epoch=NOW
    )
    assert result == ""
    assert runner.calls == []
    assert capsys.readouterr().err


def test_abort_when_description_empty(mod, tmp_path):
    runner = FakeRunner()
    result = mod.file_conflict_bug_ticket(
        _cli(tmp_path), _pending(description=""), runner=runner, now_epoch=NOW
    )
    assert result == ""
    assert runner.calls == []


def test_single_identifier_is_sufficient(mod, tmp_path):
    """One-sided conflicts (only a local id, or only a Jira key) are real — file them."""
    runner = FakeRunner({("list",): (0, "[]", "")})
    mod.file_conflict_bug_ticket(
        _cli(tmp_path), _pending(jira_key=""), runner=runner, now_epoch=NOW
    )
    assert len(runner.by_verb("create")) == 1
