"""Python-surface env reads — REBAR_* only (DSO_* support removed).

The reconciler reads several env vars directly from ``os.environ`` via a local
``_rebar_env`` helper (apply_handlers.py / outbound_differ.py). This asserts the
resolution contract: ``REBAR_<NAME>`` is honored; the legacy ``DSO_<NAME>`` is
IGNORED; otherwise the default.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine"


def _load(modname: str, filename: str):
    key = f"rebar_reconciler.{modname}"
    spec = importlib.util.spec_from_file_location(key, _ENGINE / "rebar_reconciler" / filename)
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec so @dataclass resolution works on Python 3.14
    # (dataclasses looks up cls.__module__ in sys.modules during class body exec).
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "modname,filename",
    [
        ("apply_handlers", "apply_handlers.py"),
        ("outbound_differ", "outbound_differ.py"),
    ],
)
def test_rebar_env_reads_rebar_only(modname, filename, monkeypatch):
    mod = _load(modname, filename)
    monkeypatch.delenv("REBAR_WS1ALIAS", raising=False)
    monkeypatch.delenv("DSO_WS1ALIAS", raising=False)

    # Neither set → default.
    assert mod._rebar_env("WS1ALIAS", "fallback-default") == "fallback-default"

    # Legacy DSO_* is IGNORED (support removed) → still the default.
    monkeypatch.setenv("DSO_WS1ALIAS", "from-dso")
    assert mod._rebar_env("WS1ALIAS", "fallback-default") == "fallback-default"

    # REBAR_* is honored.
    monkeypatch.setenv("REBAR_WS1ALIAS", "from-rebar")
    assert mod._rebar_env("WS1ALIAS", "fallback-default") == "from-rebar"


def test_inbound_translate_event_meta_uses_shared_legacy_constants(monkeypatch):
    """Spec-loaded inbound_translate resolves the absolute ``rebar.reducer._version``
    import for its author/env defaults — no sibling reconciler-package context needed —
    and those defaults ARE the shared legacy-Jira constants (story e622)."""
    from rebar.reducer._version import LEGACY_JIRA_AUTHOR, LEGACY_JIRA_ENV_ID

    assert LEGACY_JIRA_AUTHOR == "reconciler"
    assert LEGACY_JIRA_ENV_ID == "reconciler"

    mod = _load("inbound_translate", "inbound_translate.py")
    monkeypatch.delenv("REBAR_AUTHOR", raising=False)
    monkeypatch.delenv("REBAR_ENV_ID", raising=False)
    _ts, _uuid, env_id, author = mod._event_meta()
    assert env_id == LEGACY_JIRA_ENV_ID
    assert author == LEGACY_JIRA_AUTHOR
