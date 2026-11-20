"""Readers of atomically republished files ride out a Windows replace (bug ruby-unwomanish-willet).

While ``MoveFileEx`` replaces a file, Windows refuses an open or stat of it with
``ERROR_ACCESS_DENIED``. ``fsutil.retry_contended`` retries that refusal on Windows only;
``open_published`` and ``published_is_file`` are the read-side entry points. The Windows
branch is forced here through the module's existing switch, so these run on every host.
"""

from __future__ import annotations

import ast
import builtins
import os
from pathlib import Path

import pytest
from _repo_root import REPO_ROOT

from rebar._store import fsutil

pytestmark = pytest.mark.unit


def _refuse_then(real, refusals: int):
    calls = {"n": 0}

    def fake(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= refusals:
            raise PermissionError(13, "Access is denied")
        return real(*args, **kwargs)

    return fake, calls


@pytest.fixture
def windows_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fsutil, "_RETRY_CONTENDED_REPLACE", True)
    monkeypatch.setattr(fsutil, "_REPLACE_RETRY_FIRST_SLEEP_S", 0.001)


def test_open_published_rides_out_a_transient_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_branch: None
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"ok": true}', encoding="utf-8")
    fake, calls = _refuse_then(builtins.open, refusals=2)
    monkeypatch.setattr(builtins, "open", fake)

    with fsutil.open_published(target) as fh:
        assert fh.read() == '{"ok": true}'
    assert calls["n"] == 3


def test_open_published_reads_bytes_in_binary_mode(tmp_path: Path, windows_branch: None) -> None:
    target = tmp_path / "blob"
    target.write_bytes(b"\x00\xff")
    with fsutil.open_published(target, "rb") as fh:
        assert fh.read() == b"\x00\xff"


def test_a_refusal_that_outlasts_the_deadline_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_branch: None
) -> None:
    target = tmp_path / "state.json"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(fsutil, "_REPLACE_RETRY_DEADLINE_S", 0.05)
    fake, calls = _refuse_then(builtins.open, refusals=10**6)
    monkeypatch.setattr(builtins, "open", fake)

    with pytest.raises(PermissionError):
        fsutil.open_published(target)
    assert calls["n"] > 1


def test_a_missing_file_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_branch: None
) -> None:
    calls = {"n": 0}
    real = builtins.open

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting)
    with pytest.raises(FileNotFoundError):
        fsutil.open_published(tmp_path / "absent.json")
    assert calls["n"] == 1


def test_posix_branch_raises_the_first_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fsutil, "_RETRY_CONTENDED_REPLACE", False)
    target = tmp_path / "state.json"
    target.write_text("x", encoding="utf-8")
    fake, calls = _refuse_then(builtins.open, refusals=1)
    monkeypatch.setattr(builtins, "open", fake)

    with pytest.raises(PermissionError):
        fsutil.open_published(target)
    assert calls["n"] == 1


def test_published_is_file_retries_a_refused_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_branch: None
) -> None:
    target = tmp_path / "rebar.toml"
    target.write_text("", encoding="utf-8")
    fake, calls = _refuse_then(os.stat, refusals=2)
    monkeypatch.setattr(os, "stat", fake)

    assert fsutil.published_is_file(target) is True
    assert calls["n"] == 3


def test_published_is_file_is_false_for_absent_paths_and_directories(
    tmp_path: Path, windows_branch: None
) -> None:
    assert fsutil.published_is_file(tmp_path / "absent") is False
    assert fsutil.published_is_file(tmp_path / "absent" / "under") is False
    assert fsutil.published_is_file(tmp_path) is False


def test_replace_with_retry_still_retries_a_contended_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_branch: None
) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_text("new", encoding="utf-8")
    fake, calls = _refuse_then(os.replace, refusals=1)
    monkeypatch.setattr(os, "replace", fake)

    fsutil.replace_with_retry(src, dst)
    assert dst.read_text(encoding="utf-8") == "new"
    assert calls["n"] == 2


# Every reader the ticket found exposed to a refusal, with the helper it must use.
_READERS: dict[str, tuple[str, ...]] = {
    "src/rebar/_store/compat.py": ("_local_store_epoch", "_analyze"),
    "src/rebar/_config_sources.py": ("_parse_toml", "_discover_project_config", "raw_layers"),
    "src/rebar/_cli/__init__.py": ("_suggest_mapping_write",),
    "src/rebar/llm/evals/eval.py": ("load_eval_spec",),
    "src/rebar/llm/evals/provider_parity.py": ("eligible_cases",),
    "src/rebar/reconciler/projects_store.py": ("load_mapping",),
    "src/rebar/reconciler/binding_repository.py": ("retired_entries", "_load", "_load_retired"),
    "src/rebar/_engine_support/bridge_fsck.py": ("_read_integrity_store",),
    "src/rebar/llm/workflow/completion_banking.py": ("_read",),
    "src/rebar/_commands/session_log.py": ("_read_pointer",),
    "src/rebar/_commands/bridge_repair.py": ("_retired_keys",),
    "src/rebar/_snapshot/cache.py": ("byte_total",),
    "src/rebar/reconciler/get_rotation.py": ("load",),
    "src/rebar/llm/gate_runs.py": ("read_gate_run",),
    "src/rebar/llm/prompting/prompts.py": ("get_prompt", "_prompt_file", "prompt_override_drift"),
    "src/rebar/_commands/identity.py": ("_read_pointer_id",),
    "src/rebar/_store/push_state.py": ("read_status",),
    "src/rebar/_store/ensures.py": ("applied_ids", "_read_hinted"),
    "src/rebar/_commands/scratch.py": ("_get",),
    "src/rebar/_ids.py": ("_parse_binding_reverse",),
    "src/rebar/_engine_support/bridge_fsck_drift.py": ("_read_json",),
    "src/rebar/reconciler/alert_store.py": ("is_deduped",),
    "src/rebar/reconciler/last_pass.py": ("_read_matching_detail",),
    "src/rebar/reconciler/peer_confirmations.py": ("_load",),
    "src/rebar/reconciler/impossible_links.py": ("_load",),
    "src/rebar/llm/plan_review/checkpoints.py": ("load_checkpoint",),
    "src/rebar/llm/workflow/completion_verdict_cache.py": ("load_valid_pass_entries",),
    "src/rebar/_store/store_usability.py": ("_carries_store_structure",),
}
_HELPERS = {"open_published", "published_is_file", "retry_contended"}
_UNRETRIED_CHECKS = {"exists", "is_file", "isfile"}


def _functions(path: Path, names: tuple[str, ...]) -> dict[str, ast.AST]:
    found: dict[str, ast.AST] = {}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            found.setdefault(node.name, node)
    return found


def _called(fn: ast.AST) -> set[str]:
    """Names called in ``fn``. A check on ``.git`` is the repo-boundary probe, not a read of
    a published file, so it is left out."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and any(
                isinstance(c, ast.Constant) and c.value == ".git" for c in ast.walk(f.value)
            ):
                continue
            names.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return names


@pytest.mark.parametrize(
    ("module", "function"),
    [(m, f) for m, fns in _READERS.items() for f in fns],
)
def test_each_exposed_reader_uses_a_retried_read(module: str, function: str) -> None:
    found = _functions(REPO_ROOT / module, (function,))
    assert function in found, f"{module}: no function {function}; update this list"
    called = _called(found[function])
    assert called & _HELPERS, f"{module}:{function} reads a published file without a retry"
    assert not called & _UNRETRIED_CHECKS, (
        f"{module}:{function} checks existence with an unretried stat; let the retried "
        "open raise FileNotFoundError instead"
    )
