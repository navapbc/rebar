"""Held-out edge/contract oracle for the config-ownership gate (RP-04 S7.1, ticket 29a9).

WITHHELD from the implementation subagent: it sees only the happy-path suite. This file
pins the shapes that separate a real seam/ownership classifier from one that fakes the
happy path — the aliased-callee forms, receiver-object aliasing, getattr-env, helper-shim
resolution + fail-closed, credential ownership, backend-reload, structural exception
rejection, the registry-completeness drift self-test, and REAL-TREE cleanliness.

Assertions target OBSERVABLE behavior only (error presence/absence, named substrings,
counts, exit codes) — never internal names or source text — so a behavior-preserving
refactor of the gate keeps them green.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.scripts

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_config_ownership.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_config_ownership", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _root(tmp_path: Path, body: str, relname: str = "belowseam.py") -> Path:
    root = tmp_path / "srcroot"
    target = root / relname
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# Aliased callee forms — an env read routed through a rename must still fire.  #
# --------------------------------------------------------------------------- #


def test_import_aliased_getenv_fires(gate, tmp_path):
    body = "from os import getenv as ge\n\n\ndef f():\n    return ge('SOME_KNOB')\n"
    errors = gate.check(_root(tmp_path, body))
    assert len(errors) == 1, errors
    assert "SOME_KNOB" in errors[0]


def test_module_aliased_environ_fires(gate, tmp_path):
    body = "import os as _o\n\n\ndef f():\n    return _o.environ.get('SOME_KNOB')\n"
    errors = gate.check(_root(tmp_path, body))
    assert len(errors) == 1, errors
    assert "SOME_KNOB" in errors[0]


def test_getattr_on_environ_is_category_four(gate, tmp_path):
    body = "import os\n\n\ndef f():\n    return getattr(os.environ, 'SOME_KNOB', None)\n"
    errors = gate.check(_root(tmp_path, body))
    assert len(errors) == 1, errors
    assert "SOME_KNOB" in errors[0]


def test_receiver_object_aliasing_of_environ_fires(gate, tmp_path):
    # `src = os.environ if ... else {}` then `src.get(NAME)` — the exact evasion the
    # plan-review R4 round flagged. The reader is os.environ under an alias.
    body = (
        "import os\n\n\n"
        "def f(flag):\n"
        "    src = os.environ if flag else {}\n"
        "    return src.get('SOME_KNOB')\n"
    )
    errors = gate.check(_root(tmp_path, body))
    assert len(errors) == 1, errors
    assert "SOME_KNOB" in errors[0]


# --------------------------------------------------------------------------- #
# Helper-shim resolution + fail-closed                                        #
# --------------------------------------------------------------------------- #


def test_known_env_helper_call_resolves_and_fires(gate, tmp_path):
    gen_path = _SCRIPT.parent / "gen_env_registry.py"
    gen_spec = importlib.util.spec_from_file_location("gen_env_registry_probe", gen_path)
    assert gen_spec is not None and gen_spec.loader is not None
    gen = importlib.util.module_from_spec(gen_spec)
    gen_spec.loader.exec_module(gen)

    helper = next(iter(gen.KNOWN_ENV_HELPERS))  # a real shim name, e.g. _int_env
    body = f"def f():\n    return {helper}('SOME_KNOB', 0)\n"
    errors = gate.check(_root(tmp_path, body))
    assert len(errors) == 1, errors
    assert "SOME_KNOB" in errors[0]


def test_shim_shaped_absent_helper_fails_closed(gate, tmp_path):
    # A shim-SHAPED callee absent from KNOWN_ENV_HELPERS, called with an env-name-shaped
    # literal, cannot silently bypass the scan (bug b00f).
    body = "def f():\n    return _mystery_env('SOME_KNOB')\n"
    errors = gate.check(_root(tmp_path, body))
    assert len(errors) == 1, errors
    assert "unresolved-env-read" in errors[0]
    assert "SOME_KNOB" in errors[0]


def test_dynamic_env_name_fails_closed(gate, tmp_path):
    body = "import os\n\n\ndef f(name):\n    return os.getenv(name)\n"
    errors = gate.check(_root(tmp_path, body))
    assert len(errors) == 1, errors
    assert "unresolved-env-read" in errors[0]


def test_shim_shaped_helper_with_nonenv_literal_is_quiet(gate, tmp_path):
    # narrowing that keeps the real tree quiet: lowercase / non-env-shaped arg → no fire.
    body = "def f():\n    return _load_env('config/defaults.toml')\n"
    assert gate.check(_root(tmp_path, body)) == []


# --------------------------------------------------------------------------- #
# Credential ownership (category 5)                                           #
# --------------------------------------------------------------------------- #


def test_credential_read_below_boundary_fires(gate, tmp_path):
    body = "import os\n\n\ndef f():\n    return os.getenv('JIRA_API_TOKEN')\n"
    errors = gate.check(_root(tmp_path, body))
    assert len(errors) == 1, errors
    assert "JIRA_API_TOKEN" in errors[0]
    assert "credential" in errors[0]


def test_credential_read_inside_a_provider_boundary_passes(gate, tmp_path):
    # Same read, but placed at an APPROVED provider-credential boundary path → allowed.
    rel = "_engine/rebar_reconciler/access_check.py"
    body = "import os\n\n\ndef f():\n    return os.getenv('JIRA_API_TOKEN')\n"
    assert gate.check(_root(tmp_path, body, relname=rel)) == []


# --------------------------------------------------------------------------- #
# Backend reload (category 6b) — select_backend only, register is too generic  #
# --------------------------------------------------------------------------- #


def test_select_backend_outside_reconciler_fires(gate, tmp_path):
    body = "from rebar import select_backend\n\n\ndef f():\n    return select_backend('jira')\n"
    errors = gate.check(_root(tmp_path, body))
    assert len(errors) == 1, errors
    assert "select_backend" in errors[0] or "backend" in errors[0]


def test_select_backend_inside_reconciler_passes(gate, tmp_path):
    rel = "_engine/rebar_reconciler/runtime.py"
    body = (
        "from ._backend_registry import select_backend\n\n\n"
        "def f():\n    return select_backend('jira')\n"
    )
    assert gate.check(_root(tmp_path, body, relname=rel)) == []


def test_bare_register_call_does_not_fire(gate, tmp_path):
    # `register` is a generic name (metrics/config/workflow registries) — must NOT fire.
    body = "from somewhere import register\n\n\ndef f():\n    return register('thing')\n"
    assert gate.check(_root(tmp_path, body)) == []


# --------------------------------------------------------------------------- #
# Configurable default (category 6a)                                          #
# --------------------------------------------------------------------------- #


def test_module_level_configurable_default_fires(gate, tmp_path):
    body = "import os\n\nTIMEOUT = os.getenv('SOME_KNOB')\n"
    errors = gate.check(_root(tmp_path, body))
    assert len(errors) == 1, errors
    assert "SOME_KNOB" in errors[0]


# --------------------------------------------------------------------------- #
# Composition-root seams own categories 1–4                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "seam",
    ["config.py", "_config_sources.py", "_config_schema.py", "_child_env.py", "model_classes.py"],
)
def test_env_read_inside_a_composition_root_passes(gate, tmp_path, seam):
    body = "import os\n\n\ndef f():\n    return os.getenv('SOME_KNOB')\n"
    assert gate.check(_root(tmp_path, body, relname=seam)) == []


# --------------------------------------------------------------------------- #
# LEGACY_EXCEPTIONS — structural validation + suppression                     #
# --------------------------------------------------------------------------- #


def test_legacy_exception_suppresses_the_named_read(gate, tmp_path, monkeypatch):
    body = "import os\n\n\ndef f():\n    return os.getenv('SOME_KNOB')\n"
    root = _root(tmp_path, body)
    assert gate.check(root) != []  # baseline: fires without an exception
    monkeypatch.setattr(
        gate,
        "LEGACY_EXCEPTIONS",
        [
            {
                "path": "belowseam.py",
                "symbol": "SOME_KNOB",
                "rationale": "legacy knob, ticket abcd-1234",
            }
        ],
    )
    assert gate.check(root) == []


def test_glob_metachar_in_exception_path_is_rejected(gate, tmp_path, monkeypatch):
    monkeypatch.setattr(
        gate,
        "LEGACY_EXCEPTIONS",
        [{"path": "src/**/*.py", "symbol": "X", "rationale": "broad"}],
    )
    errors = gate.check(_root(tmp_path, "x = 1\n"))
    assert any("*" in e or "glob" in e.lower() for e in errors), errors


def test_empty_rationale_exception_is_rejected(gate, tmp_path, monkeypatch):
    monkeypatch.setattr(
        gate,
        "LEGACY_EXCEPTIONS",
        [{"path": "belowseam.py", "symbol": "X", "rationale": ""}],
    )
    errors = gate.check(_root(tmp_path, "x = 1\n"))
    assert any("rationale" in e.lower() for e in errors), errors


# --------------------------------------------------------------------------- #
# Registry-completeness drift self-test (root-independent)                    #
# --------------------------------------------------------------------------- #


def test_unowned_credential_name_drifts_to_error(gate, tmp_path, monkeypatch):
    import rebar._child_env as ce

    patched = dict(ce._ADAPTER_SECRET_NAMES)
    patched["fake-adapter"] = frozenset({"FAKE_UNREAD_SECRET_XYZ"})
    monkeypatch.setattr(ce, "_ADAPTER_SECRET_NAMES", patched)
    errors = gate.check(_root(tmp_path, "x = 1\n"))
    assert any("FAKE_UNREAD_SECRET_XYZ" in e for e in errors), errors


# --------------------------------------------------------------------------- #
# Real-tree cleanliness — the enrolled gate must pass on rebar itself         #
# --------------------------------------------------------------------------- #


def test_real_source_tree_is_clean(gate):
    root = gate.REPO_ROOT / "src" / "rebar"
    assert gate.check(root) == [], "the enrolled tree must be clean via LEGACY_EXCEPTIONS"


def test_main_on_real_tree_returns_zero(gate):
    assert gate.main([]) == 0
