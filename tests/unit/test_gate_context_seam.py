"""The gate read-root/session domain moves out of `llm/config.py` (ticket b300).

`config.py` was at 793 LOC against the 800 hard cap and is the most-imported near-cap module in the
repo (fan-in 93), while ticket d23e must add a `REBAR_LLM_MODEL` deprecation alias to it — a change
measured at 10-16 lines against 7 available. The relieving cut is NOT the declarative field table
ADR 0056 speculated about (that would break the env-registry drift gate, which only records
STRING-LITERAL env names); it is the ~219 lines of gate read-root / snapshot-session context, which
is a DIFFERENT CONCERN that merely lived in the same file, is INERT (no growth since 2026-07-09),
and already has a consumer proving the seam at `llm/gate_source.py:35`.

THE LOAD-BEARING CONSTRAINT: the moved names must stay reachable through `rebar.llm.config`, and
NO consumer under `src/` may be repointed at `gate_context`. Thirteen monkeypatch targets in the
suite name `rebar.llm.config.<moved name>`, and they keep working ONLY because every consumer
resolves the name at call time out of `config`'s module globals (a function-level `from
rebar.llm.config import ...`, e.g. `plan_review/attest.py:123`). Repointing a consumer to the new
module is the silent-break mode: the patch still applies to `config`, the consumer no longer reads
it, and the test passes while asserting nothing. That is why this file tests the RE-EXPORT and the
NON-REPOINTING as contracts rather than trusting the relocation."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _tree_scan import parsed_python_files

REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = REPO_ROOT / "src" / "rebar" / "llm" / "config.py"
_GATE_CONTEXT = REPO_ROOT / "src" / "rebar" / "llm" / "gate_context.py"
_SRC = REPO_ROOT / "src" / "rebar"
# `config_readers.py` is `config.py`'s own composition-root sibling (ticket 02b7 moved the
# env/file-reader helpers there to clear the module-size cap). It calls `current_code_root()`
# directly for `_read_llm_file_table`'s ambient-discovery fallback; that call is never a
# monkeypatch target (no test patches `rebar.llm.gate_context.current_code_root` — it's a
# ContextVar-backed read, exercised via `use_code_root`/`gate_read_root`, not mocked), so
# giving it its own import does not reproduce the silent-break mode this guard exists for.
_CONFIG_READERS = REPO_ROOT / "src" / "rebar" / "llm" / "config_readers.py"

# The nine public names plus the three ContextVars that carry the domain's state.
_MOVED_PUBLIC = (
    "current_code_root",
    "resolve_code_root",
    "current_tickets_root",
    "current_code_sha",
    "in_gate_session",
    "gate_session",
    "assert_gated",
    "use_code_root",
    "use_tickets_root",
)
_MOVED_PRIVATE = ("_active_code_root", "_active_tickets_root", "_in_gate_session")

# AGENTS.md: never create a file under 100 LOC by splitting. This is the ONLY size bound this file
# asserts — the upper ceiling belongs to `.github/module-size-limit.txt` alone, enforced by the CI
# module-size gate and mirrored in-process by test_module_size_contract.py.
_FLOOR = 100


def _loc(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


# ══ HAPPY PATH ════════════════════════════════════════════════════════════════════════


def test_the_extracted_module_exists_and_is_a_reasonable_size() -> None:
    """A new module must clear AGENTS.md's 100-LOC anti-fragmentation floor — splitting a file into
    a tiny fragment trades one problem for two."""
    assert _GATE_CONTEXT.exists(), "src/rebar/llm/gate_context.py was not created"
    loc = _loc(_GATE_CONTEXT)
    assert loc >= _FLOOR, (
        f"gate_context.py is {loc} LOC; splitting must not create a file under {_FLOOR} LOC"
    )


def test_the_audited_override_warning_names_the_module_that_holds_the_code(caplog) -> None:
    """`assert_gated` hardcoded `logging.getLogger("rebar.llm.config")`. The warning it emits is
    security-relevant — its own text calls the REBAR_GATE_ALLOW_UNGATED override "audited" — so
    attributing it to a module that no longer contains the code sends whoever greps the logs to the
    wrong file. Verified safe to re-point: that literal had exactly one occurrence repo-wide and no
    test filtered on it."""
    from rebar.llm import gate_context

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REBAR_GATE_ALLOW_UNGATED", "1")
        with caplog.at_level("WARNING"):
            gate_context.assert_gated("probe context")

    records = [r for r in caplog.records if "OUTSIDE a snapshot gate session" in r.getMessage()]
    assert records, "the override warning was not emitted"
    assert all(r.name == "rebar.llm.gate_context" for r in records), (
        f"warning attributed to {[r.name for r in records]}, not rebar.llm.gate_context"
    )


# ══ HELD OUT ══════════════════════════════════════════════════════════════════════════


def test_moved_names_are_no_longer_reachable_through_config() -> None:
    """ADR 0111: gate-context state has one canonical binding in ``rebar.llm.gate_context``."""
    from rebar.llm import config as llm_config

    leaked = [n for n in (*_MOVED_PUBLIC, *_MOVED_PRIVATE) if hasattr(llm_config, n)]
    assert leaked == [], f"still reachable as rebar.llm.config.<name>: {leaked}"


def test_src_consumers_import_gate_context_from_the_canonical_module() -> None:
    """No source consumer may keep importing gate-context symbols through ``llm.config``."""
    offenders: list[str] = []
    for module in parsed_python_files(_SRC):
        if module.path in (_GATE_CONTEXT, _CONFIG, _CONFIG_READERS):
            continue
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("llm.config"):
                names = {a.name for a in node.names}
                if names.intersection((*_MOVED_PUBLIC, *_MOVED_PRIVATE)):
                    offenders.append(f"{module.path.relative_to(REPO_ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
                if (
                    isinstance(node.value.value, ast.Name)
                    and node.value.value.id == "config"
                    and node.value.attr in {"llm"}
                    and node.attr in _MOVED_PUBLIC
                ):
                    offenders.append(f"{module.path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == [], (
        f"these still route gate-context symbols through rebar.llm.config: {offenders}"
    )


def test_gate_context_is_a_leaf_and_does_not_import_config() -> None:
    """One-way dependency. `config` imports `gate_context`, never the reverse — `model_classes.py`'s
    docstring already forbids config importing back into its dependents, and a cycle here would be
    an import-time failure rather than a lint nit."""
    tree = ast.parse(_GATE_CONTEXT.read_text(encoding="utf-8"))
    cycles = [
        f"line {n.lineno}"
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("llm.config")
    ]
    assert cycles == [], f"gate_context imports llm.config (cycle): {cycles}"


def test_a_monkeypatch_on_gate_context_still_reaches_a_real_consumer(monkeypatch) -> None:
    """Behavioural proof that consumers resolve the canonical gate-context module."""
    from rebar.llm import gate_context

    monkeypatch.setattr(gate_context, "current_code_sha", lambda: "deadbeef")
    from rebar.llm.gate_context import current_code_sha as resolved_at_call_time

    assert resolved_at_call_time() == "deadbeef"


def test_the_seam_headroom_bounds_are_per_file() -> None:
    """The coordination hazard between this ticket and b5fe. The d8ef seam test asserted both
    `workflow_ops.py` and `config.py` against ONE shared constant; tightening it for config.py would
    also tighten it for workflow_ops.py, which stays ~794 until b5fe lands, so the shared form makes
    whichever ticket lands first go red on the OTHER ticket's file — and the failure names the wrong
    file. The bounds must be per file."""
    seam = (REPO_ROOT / "tests" / "unit" / "test_plan_review_workflow_ops_seam.py").read_text()
    assert "for path in (_WORKFLOW_OPS, _LLM_CONFIG)" not in seam, (
        "the shared-bound loop is still present; split it into per-file bounds before tightening"
    )
