"""Held-out seam-clean + behavioral proof for RP-04 C3g (binding_lifecycle grace).

This is the FINAL config-ownership drain: it cuts the last below-seam ambient read —
``binding_lifecycle.py``'s ``RECONCILER_ABSENT_RETIRE_GRACE`` lookup (through the local
``_env_int`` shim) — to the owned accessor ``resolve_absent_retire_grace()`` exposed on
the PUBLIC ``rebar.config`` facade. With it gone, the ``LEGACY_EXCEPTIONS`` whitelist is
EMPTY and the whole-tree drain is complete.

Observable behavior and contracts only — never internal structure.

Seam-clean (the strong anti-fake):

1. ``LEGACY_EXCEPTIONS`` is now EMPTY (``[]``) — the last two rows are drained.
2. The config-ownership gate reports ZERO findings across the whole ``src/rebar`` tree.
3. No ``_env_int`` shim (and no ambient ``os.environ`` read) remains in
   ``binding_lifecycle.py`` — the read was cut, not marked.

Behavioral (the cut must be a pure re-source of the grace read; asserted through stable
entry points that survive it):

4. ``resolve_absent_retire_grace`` is importable from the PUBLIC ``rebar.config`` facade
   and keeps its contract: unset ⇒ default 3, a valid value is honored, malformed ⇒
   default, and a sub-minimum value clamps to 1.
5. END-TO-END: a real ``BindingStore`` retires a bound key at EXACTLY ``grace``
   consecutive confirmed-404 ``note_absent`` calls (driven the production way) — proving
   the accessor is wired into ``note_absent``, not merely present in the tree.
6. The ``_DEFAULT_ABSENT_RETIRE_GRACE`` constant is RETAINED and still agrees across its
   ``binding_store`` alias (a live cross-module consumer the cut must not break).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

# Bare-name imports: ``tests/scripts/conftest.py`` puts repo-root ``scripts/`` on sys.path.
import check_config_ownership as gate
import config_ownership_exceptions as exceptions
import pytest

_SRC = gate.REPO_ROOT / "src" / "rebar"
_REC = _SRC / "_engine" / "rebar_reconciler"
_BINDING_LIFECYCLE = _REC / "binding_lifecycle.py"


def _load_module(name: str, path: Path) -> ModuleType:
    """Path-load a reconciler module — ``rebar._engine`` is not an importable package,
    so its modules are loaded by file path (the convention across this test tree)."""
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Seam-clean structural properties
# ---------------------------------------------------------------------------


def test_legacy_exceptions_is_empty() -> None:
    assert exceptions.LEGACY_EXCEPTIONS == [], (
        "C3g is the final drain — the LEGACY_EXCEPTIONS whitelist must be EMPTY; "
        f"still present: {exceptions.LEGACY_EXCEPTIONS}"
    )


def test_gate_reports_no_findings_whole_tree() -> None:
    findings = gate.check(_SRC)
    assert findings == [], (
        "with the whitelist empty, the config-ownership gate must report zero findings "
        f"across the whole tree; got: {findings}"
    )


def test_no_env_int_shim_or_ambient_read_in_binding_lifecycle() -> None:
    text = _BINDING_LIFECYCLE.read_text(encoding="utf-8")
    assert "def _env_int" not in text, (
        "the dead _env_int shim must be removed from binding_lifecycle.py (cut, not kept)"
    )
    assert "os.environ" not in text, (
        "no ambient os.environ read may remain in binding_lifecycle.py after the cut"
    )


# ---------------------------------------------------------------------------
# Behavioral regressions the cut must preserve
# ---------------------------------------------------------------------------


def test_resolve_absent_retire_grace_via_public_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owned accessor is reachable from the PUBLIC ``rebar.config`` facade and keeps
    its byte-identical contract."""
    from rebar.config import resolve_absent_retire_grace

    monkeypatch.delenv("RECONCILER_ABSENT_RETIRE_GRACE", raising=False)
    assert resolve_absent_retire_grace() == 3, "unset ⇒ default 3"
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "5")
    assert resolve_absent_retire_grace() == 5, "a valid value is honored"
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "not-an-int")
    assert resolve_absent_retire_grace() == 3, "malformed ⇒ default"
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "0")
    assert resolve_absent_retire_grace() == 1, "a sub-minimum value clamps to 1"


def _binding_store_mod() -> ModuleType:
    return _load_module("binding_store_c3g", _REC / "binding_store.py")


def test_binding_retires_at_exactly_grace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """END-TO-END: with ``RECONCILER_ABSENT_RETIRE_GRACE=2`` a bound key survives the
    first confirmed-404 ``note_absent`` and retires on the SECOND — proving the accessor
    is wired into ``note_absent`` through the cut. A cut that froze the grace at the
    default 3, or dropped the read, would retire at the wrong count."""
    binding_store = _binding_store_mod()
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "2")

    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir(parents=True, exist_ok=True)
    store = binding_store.BindingStore(tracker)
    store.bind_confirm("local-1", "JIRA-1")
    store.save()

    store.note_absent("JIRA-1")
    assert not store.is_retired("JIRA-1"), "must NOT retire before grace consecutive 404s"
    store.note_absent("JIRA-1")
    assert store.is_retired("JIRA-1"), "must retire at exactly grace (2) consecutive 404s"


def test_default_grace_constant_retained_and_aliased() -> None:
    """The ``_DEFAULT_ABSENT_RETIRE_GRACE`` constant is kept (a plain constant, not an
    ambient read) and its ``binding_store`` alias still agrees — the cross-module
    consumer the cut must not break."""
    binding_lifecycle = _load_module("binding_lifecycle_c3g", _BINDING_LIFECYCLE)
    binding_store = _load_module("binding_store_c3g_const", _REC / "binding_store.py")

    assert binding_lifecycle._DEFAULT_ABSENT_RETIRE_GRACE == 3
    assert (
        binding_store._DEFAULT_ABSENT_RETIRE_GRACE == binding_lifecycle._DEFAULT_ABSENT_RETIRE_GRACE
    )
