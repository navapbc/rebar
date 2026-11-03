"""Tests patch a Starlette ``app.state`` attribute through its backing dict, never ``setattr``.

Starlette's ``State`` keeps every attribute in a private ``_state`` dict and routes
``__getattr__``/``__setattr__``/``__delattr__`` through it, so ``vars(app.state)`` never holds
``config``. pytest 9.2.0 (pytest-dev/pytest#14962) made ``MonkeyPatch.setattr`` record an
instance's old value from ``target.__dict__``; for a ``State`` that value is always "absent", so
teardown runs ``delattr(app.state, "config")`` and deletes the review-bot module's real config.
Every later test on that worker then saw ``'State' object has no attribute 'config'``
(bug b48a-0ffe-4f9c-4b28). A bare ``app.state.config = ...`` leaks the other way: it replaces a
shared app's config and never restores it.

``monkeypatch.setitem(<app>.state._state, name, value)`` reads and restores the dict entry
directly, so it is correct on every pytest version. This module scans ``tests/`` for the two
unsafe forms and proves the sanctioned form restores both a present and an absent entry.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TESTS_ROOT = Path(__file__).resolve().parents[1]
SANCTIONED = "monkeypatch.setitem(<app>.state._state, <name>, <value>)"


def _is_state(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "state"


def _is_app_state(node: ast.expr) -> bool:
    if not (isinstance(node, ast.Attribute) and node.attr == "state"):
        return False
    owner = node.value
    return (isinstance(owner, ast.Attribute) and owner.attr == "app") or (
        isinstance(owner, ast.Name) and owner.id == "app"
    )


def _violations(source: str, rel: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
            and node.args
            and _is_state(node.args[0])
        ):
            found.append(f"{rel}:{node.lineno}: setattr on a State: {ast.unparse(node)[:100]}")
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and _is_app_state(target.value):
                found.append(f"{rel}:{node.lineno}: unrestored write: {ast.unparse(target)}")
    return found


def _scan_tree(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent).as_posix()
        found.extend(_violations(path.read_text(encoding="utf-8"), rel))
    return found


def test_no_test_patches_app_state_through_setattr_or_bare_assignment() -> None:
    found = _scan_tree(TESTS_ROOT)
    assert not found, (
        "Patch app.state through its backing dict so teardown restores it on every pytest "
        f"version: {SANCTIONED}.\n" + "\n".join(found)
    )


@pytest.mark.parametrize(
    "snippet",
    [
        'monkeypatch.setattr(appmod.app.state, "config", cfg)',
        'monkeypatch.setattr(appmod.app.state, "config", cfg, raising=False)',
        'mp.setattr(request.state, "user", u)',
        "app_module.app.state.config = cfg",
        "app.state.queue = q",
    ],
)
def test_the_scan_flags_each_unsafe_form(snippet: str) -> None:
    assert _violations(snippet, "x.py"), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        'monkeypatch.setitem(appmod.app.state._state, "config", cfg)',
        'monkeypatch.delattr(appmod.app.state, "queue", raising=False)',
        'monkeypatch.setattr(appmod, "_worker", fake)',
        "request.state.user = u",
        "cfg = appmod.app.state.config",
    ],
)
def test_the_scan_allows_safe_forms(snippet: str) -> None:
    assert not _violations(snippet, "x.py"), snippet


def test_the_sanctioned_form_restores_a_present_entry_and_removes_an_absent_one() -> None:
    # starlette is always installed: the dev extra requires mcp, which depends on it.
    from starlette.datastructures import State

    real = object()
    state = State({"config": real})
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(state._state, "config", object())
        mp.setitem(state._state, "queue", object())
        assert state.config is not real
    assert state.config is real
    assert not hasattr(state, "queue")
