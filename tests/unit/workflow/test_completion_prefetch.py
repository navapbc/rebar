"""Held-out oracle for the completion prefetch feature (story a9dd).

Offline only — no network, no live LLM. Pins the pure ranking/skeleton/clamp/ceiling
arithmetic, the opt-in ``assemble_context`` no-op contract, the successor prompt-inheritance of
the trusted prefetch paragraph, and the Phase-0 usage_log distinct-fetch / overlap additions.
"""

from __future__ import annotations

import pytest

from rebar.llm import operations, usage_log
from rebar.llm.workflow import completion_banking as cb
from rebar.llm.workflow import completion_prefetch as pf

pytestmark = pytest.mark.unit


# ── rank_paths ──────────────────────────────────────────────────────────────────────
def test_rank_paths_close_path_returns_own_only_deduped() -> None:
    own = ["a.py", "b.py", "a.py"]
    ranked = pf.rank_paths(own, {"z.py": 99}, graph=False)
    assert ranked == ["a.py", "b.py"]  # subtree map ignored on close path


def test_rank_paths_graph_orders_own_first_then_freq_desc_tie_path_asc() -> None:
    own = ["own1.py", "own2.py"]
    subtree_freq = {"own1.py": 5, "hi.py": 3, "mid.py": 2, "also.py": 2}
    ranked = pf.rank_paths(own, subtree_freq, graph=True)
    # own first (given order), then remaining by desc freq, ties by path asc; no dupes.
    assert ranked == ["own1.py", "own2.py", "hi.py", "also.py", "mid.py"]
    assert len(ranked) == len(set(ranked))


# ── skeleton_compress ───────────────────────────────────────────────────────────────
def test_skeleton_compress_keeps_signatures_elides_bodies() -> None:
    text = (
        "import os\n"
        "\n"
        "@decorator\n"
        "def foo(x):\n"
        '    """Docstring."""\n'
        "    a = 1\n"
        "    b = 2\n"
        "    return a + b\n"
        "\n"
        "class Bar:\n"
        "    async def baz(self):\n"
        "        do_stuff()\n"
    )
    out = pf.skeleton_compress(text)
    assert "@decorator" in out
    assert "def foo(x):" in out
    assert '"""Docstring."""' in out
    assert "class Bar:" in out
    assert "async def baz(self):" in out
    assert "a = 1" not in out
    assert "do_stuff()" not in out
    assert "lines elided; call read_file" in out


# ── clamp_and_format ────────────────────────────────────────────────────────────────
def test_clamp_and_format_full_mode_wraps_and_manifest() -> None:
    bodies = {"a.py": "print(1)\n", "b.py": "print(2)\n"}
    section, manifest = pf.clamp_and_format(
        ["a.py", "b.py"], bodies, token_budget=10_000, per_file_char_cap=12_000
    )
    assert section.startswith(f"<{pf.PREFETCH_TAG}>\n")
    assert section.rstrip().endswith(f"</{pf.PREFETCH_TAG}>")
    assert "PRE-LOAD MANIFEST:" in section
    assert "- a.py: full" in section
    assert "--- a.py (full) ---" in section
    assert "--- b.py (full) ---" in section
    assert manifest == [{"path": "a.py", "mode": "full"}, {"path": "b.py", "mode": "full"}]


def test_clamp_and_format_skeleton_on_size_and_linecount() -> None:
    big_body = "def f():\n" + ("    x = 1\n" * 20_000)  # exceeds char cap
    many_lines = "def g():\n" + ("    y = 2\n" * (pf.SKELETON_LINE_THRESHOLD + 10))
    bodies = {"big.py": big_body, "lines.py": many_lines}
    _section, manifest = pf.clamp_and_format(
        ["big.py", "lines.py"], bodies, token_budget=1_000_000, per_file_char_cap=12_000
    )
    assert manifest == [
        {"path": "big.py", "mode": "skeleton"},
        {"path": "lines.py", "mode": "skeleton"},
    ]


def test_clamp_and_format_drops_lowest_ranked_over_budget() -> None:
    body = "x" * 4000  # ~1000 tokens each
    bodies = {"a.py": body, "b.py": body, "c.py": body}
    # budget only admits ~2 files; lowest-ranked (c.py) is dropped, never added.
    _section, manifest = pf.clamp_and_format(
        ["a.py", "b.py", "c.py"], bodies, token_budget=2200, per_file_char_cap=99_999
    )
    paths = [m["path"] for m in manifest]
    assert "a.py" in paths
    assert "c.py" not in paths


def test_clamp_and_format_skips_paths_absent_from_bodies() -> None:
    bodies = {"present.py": "code\n"}
    _section, manifest = pf.clamp_and_format(
        ["missing.py", "present.py"], bodies, token_budget=10_000, per_file_char_cap=12_000
    )
    assert manifest == [{"path": "present.py", "mode": "full"}]


# ── test_glob_for_module ────────────────────────────────────────────────────────────
def test_test_glob_for_module() -> None:
    assert pf.test_glob_for_module("src/rebar/llm/foo.py") == "tests/**/test_foo*.py"
    assert pf.test_glob_for_module("README.md") == ""
    assert pf.test_glob_for_module("Makefile") == ""


# ── fit_within_ceiling ──────────────────────────────────────────────────────────────
def _pin_ceiling(monkeypatch, value: int) -> None:
    monkeypatch.setattr(
        "rebar.llm.workflow.completion_recovery.physical_context_ceiling",
        lambda model: value,
    )


def test_fit_within_ceiling_fits_unchanged(monkeypatch) -> None:
    _pin_ceiling(monkeypatch, 1000)
    base = "b" * 100
    section = "s" * 100
    assert pf.fit_within_ceiling(base, section, None) == section


def test_fit_within_ceiling_trims_and_marks(monkeypatch) -> None:
    _pin_ceiling(monkeypatch, 500)
    base = "b" * 100
    section = "<prefetched_file_contents>\n" + ("x" * 2000)
    out = pf.fit_within_ceiling(base, section, None)
    assert len(base) + len(out) <= 500
    assert out.endswith("... <prefetch truncated to fit context ceiling> ...")


def test_fit_within_ceiling_prefers_block_boundary(monkeypatch) -> None:
    _pin_ceiling(monkeypatch, 200)
    base = ""
    section = "head\n--- a.py (full) ---\nAAAA\n--- b.py (full) ---\n" + ("B" * 500)
    out = pf.fit_within_ceiling(base, section, None)
    assert len(out) <= 200
    assert "truncated to fit context ceiling" in out


def test_fit_within_ceiling_empty_when_base_fills(monkeypatch) -> None:
    _pin_ceiling(monkeypatch, 100)
    base = "b" * 100
    assert pf.fit_within_ceiling(base, "section", None) == ""


# ── assemble_context opt-in no-op ───────────────────────────────────────────────────
def _fake_ticket(monkeypatch) -> None:
    def fake_show(ticket_id, *, repo_root=None, **kw):
        return {
            "ticket_id": ticket_id,
            "title": "T",
            "ticket_type": "task",
            "status": "open",
            "priority": 2,
            "description": "desc body",
            "comments": [],
        }

    monkeypatch.setattr("rebar._reads.show_ticket", fake_show)
    monkeypatch.setattr("rebar._reads.list_tickets", lambda **kw: [])


def test_assemble_context_prefetch_none_is_byte_identical(monkeypatch) -> None:
    _fake_ticket(monkeypatch)
    without, ids1 = operations.assemble_context("T1", graph=False, repo_root=".")
    with_none, ids2 = operations.assemble_context("T1", graph=False, repo_root=".", prefetch=None)
    assert without == with_none
    assert ids1 == ids2


def test_assemble_context_with_spec_appends_section(monkeypatch) -> None:
    _fake_ticket(monkeypatch)
    section = (
        f"<{pf.PREFETCH_TAG}>\nPRE-LOAD MANIFEST:\n- x.py: full\n\n"
        f"--- x.py (full) ---\nX\n</{pf.PREFETCH_TAG}>"
    )
    monkeypatch.setattr(
        "rebar.llm.workflow.completion_prefetch.assemble_prefetch",
        lambda spec, *, repo_root: (section, [{"path": "x.py", "mode": "full"}]),
    )
    spec = pf.PrefetchSpec(ticket_id="T1", graph=False)
    base, _ = operations.assemble_context("T1", graph=False, repo_root=".")
    ctx, _ids = operations.assemble_context("T1", graph=False, repo_root=".", prefetch=spec)
    assert ctx == base + "\n\n" + section
    assert f"<{pf.PREFETCH_TAG}>" in ctx


def test_assemble_context_empty_section_appends_nothing(monkeypatch) -> None:
    _fake_ticket(monkeypatch)
    monkeypatch.setattr(
        "rebar.llm.workflow.completion_prefetch.assemble_prefetch",
        lambda spec, *, repo_root: ("", []),
    )
    spec = pf.PrefetchSpec(ticket_id="T1", graph=False)
    base, _ = operations.assemble_context("T1", graph=False, repo_root=".")
    ctx, _ = operations.assemble_context("T1", graph=False, repo_root=".", prefetch=spec)
    assert ctx == base


# ── successor path carries the prefetch ─────────────────────────────────────────────
def test_successor_instructions_embeds_prefetch_in_context() -> None:
    section = (
        f"<{pf.PREFETCH_TAG}>\nPRE-LOAD MANIFEST:\n- a.py: full\n\n"
        f"--- a.py (full) ---\ncode\n</{pf.PREFETCH_TAG}>"
    )
    ticket_context = "Ticket body\n\n" + section
    out = cb.successor_instructions(
        "T1",
        ticket_context,
        batch=["do the thing"],
        id_by_text={"do the thing": "c00-abcd1234"},
        banked={},
    )
    assert section in out
    assert f"<{pf.PREFETCH_TAG}>" in out


# ── Phase 0: fetch_target ────────────────────────────────────────────────────────────
def test_fetch_target_dict_args() -> None:
    assert usage_log.fetch_target("read_file", {"path": "src/x.py", "line_start": 3}) == "src/x.py"
    assert usage_log.fetch_target("search_files", {"query": "needle"}) == "needle"
    assert usage_log.fetch_target("search_files", {"pattern": "regexp"}) == "regexp"
    assert usage_log.fetch_target("list_directory", {"path": "src/"}) == "src/"
    assert usage_log.fetch_target("record_criterion_verdict", {"path": "x"}) is None


def test_fetch_target_json_string_args() -> None:
    assert usage_log.fetch_target("read_file", '{"path": "a/b.py"}') == "a/b.py"
    assert usage_log.fetch_target("read_file", "not json") is None
    assert usage_log.fetch_target("read_file", 42) is None
    assert usage_log.fetch_target("read_file", {"nope": 1}) is None


# ── Phase 0: run_shape distinct_fetches ─────────────────────────────────────────────
class _ToolCallPart:
    def __init__(self, tool_name, args):
        self.tool_name = tool_name
        self.args = args


class _Usage:
    input_tokens = output_tokens = cache_read_tokens = cache_write_tokens = 0


class _ModelResponse:
    finish_reason = "stop"
    usage = _Usage()

    def __init__(self, parts):
        self.parts = parts


# The reducer discriminates by ``type(part).__name__`` — alias to the expected names.
_ToolCallPart.__name__ = "ToolCallPart"
_ModelResponse.__name__ = "ModelResponse"


def test_run_shape_collects_distinct_fetches_order_preserved() -> None:
    messages = [
        _ModelResponse(
            [
                _ToolCallPart("read_file", {"path": "a.py"}),
                _ToolCallPart("read_file", {"path": "a.py"}),  # dup → collapsed
                _ToolCallPart("search_files", {"query": "q"}),
                _ToolCallPart("read_file", {"path": "b.py"}),
                _ToolCallPart("record_criterion_verdict", {"x": 1}),  # non-fetch → skipped
            ]
        )
    ]
    shape = usage_log.run_shape(messages, request_limit=10, tool_calls_limit=20)
    assert shape["distinct_fetches"] == [
        {"tool": "read_file", "target": "a.py"},
        {"tool": "search_files", "target": "q"},
        {"tool": "read_file", "target": "b.py"},
    ]
    assert "distinct_fetches" in usage_log._SHAPE_FIELDS


# ── Phase 0: fetch_overlap ──────────────────────────────────────────────────────────
def test_fetch_overlap_exact_and_suffix() -> None:
    fetches = [
        {"tool": "read_file", "target": "src/rebar/llm/foo.py"},
        {"tool": "read_file", "target": "./bar.py"},
        {"tool": "read_file", "target": "unrelated.py"},
        {"tool": "search_files", "target": "ignored_query"},  # not path-bearing
    ]
    file_impact = ["src/rebar/llm/foo.py", "pkg/bar.py"]
    result = usage_log.fetch_overlap(fetches, file_impact)
    assert result["total"] == 3  # only read_file entries
    assert result["covered"] == 2  # foo.py exact, bar.py suffix
    assert result["fraction"] == round(2 / 3, 4)


def test_fetch_overlap_zero_total() -> None:
    assert usage_log.fetch_overlap([], ["a.py"]) == {
        "covered": 0,
        "total": 0,
        "fraction": 0.0,
    }


# ── system prompt inheritance of the trusted prefetch paragraph ──────────────────────
def test_prefetch_paragraph_before_volatile_and_inherited_by_successor() -> None:
    from pathlib import Path

    md = Path("src/rebar/llm/reviewers/completion_verifier.md").read_text()
    before_volatile = md.split("<!--volatile-->", 1)[0]
    assert "prefetched_file_contents" in before_volatile
    assert "PRE-LOAD MANIFEST" in before_volatile

    successor = cb.successor_system_prompt(None)
    assert "prefetched_file_contents" in successor
