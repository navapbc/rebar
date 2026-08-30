"""Single definitions for the token-identical helpers (story 7931-faee-53ba-47b1).

Two helpers exist as byte- or AST-identical twins across modules that never share code:

* ``_ticket_id`` — AST-identical in ``llm/workflow/steps.py`` and
  ``llm/plan_review/decide_ops.py``;
* ``_positive_int`` — three identical argparse converters (``rebar_reconciler/request.py``,
  ``_cli/_parsers/advanced/bridge.py``, ``_cli/_parsers/advanced/reconcile.py``).

(The diagnostic-only ``_shadow``/``emit_shadow_snapshot`` twin this file used to guard was
retired once ticket ec44 migrated its last two callers — ``_mcp_llm`` and ``_lib_ops`` — onto
the authoritative ``compose_and_bind_llm_config`` / ``compose_and_bind_operation_snapshot``
bindings; see ``tests/unit/test_operation_snapshot.py`` and
``tests/interfaces/contracts/test_llm_config_authoritative_binding.py`` for its successor
coverage.)

These helpers carry no distinctive atom to scan for, so the guard is the definition COUNT —
branch 4 of the parent epic's guard rule. The behavioural tests below exist because a
definition count alone would be satisfied by a helper that forwards the WRONG arguments.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _tree_scan import parsed_python_files

import rebar

_SRC = Path(rebar.__file__).resolve().parent


def _definitions(name: str) -> list[str]:
    """Every top-level or nested ``def <name>`` under ``src/rebar``, as path:line."""
    found: list[str] = []
    for module in parsed_python_files(_SRC):
        for node in ast.walk(module.tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
                found.append(f"{module.path.relative_to(_SRC.parent)}:{node.lineno}")
    return found


# ======================================================================================
# HELD OUT
# ======================================================================================
def test_each_helper_is_defined_exactly_once() -> None:
    """The guard. A count, not a token scan: these helpers have no distinctive atom, which is
    branch 4 of the parent epic's guard rule. A fourth copy pasted into a new module must fail
    here even though no test happens to execute it."""
    for name in ("_ticket_id",):
        assert len(_definitions(name)) == 1, f"{name}: {_definitions(name)}"


def test_the_argparse_positive_int_is_defined_once_and_its_namesake_survives() -> None:
    """``_positive_int`` is subtler: the THREE argparse converters collapse to one, but
    ``_config_resolvers._positive_int`` is a NAMESAKE, not a twin — it takes ``(raw, default)``
    and RETURNS the default instead of raising, so merging it would turn a rejected CLI value
    into a silent default. It must survive, which is why this is a count of 2, not 1."""
    defs = _definitions("_positive_int")
    assert len(defs) == 2, defs
    assert any(d.startswith("rebar/_config_resolvers.py") for d in defs), defs


def test_the_two_ticket_id_call_sites_share_one_object() -> None:
    from rebar.llm.plan_review import decide_ops
    from rebar.llm.workflow import steps

    assert steps._ticket_id is decide_ops._ticket_id


def test_no_surface_still_imports_the_retired_shadow_helper() -> None:
    """Ticket ec44 migrated the last two shadow callers (``_mcp_llm``, ``_lib_ops``) onto
    the authoritative bindings and deleted ``_shadow``/``emit_shadow_snapshot`` entirely —
    no module may still import or expose it."""
    from rebar import _lib_ops, _mcp_llm, _mcp_reads, _mcp_writes, _operation_config

    for module in (_mcp_reads, _mcp_writes, _mcp_llm, _lib_ops, _operation_config):
        assert not hasattr(module, "_shadow")
        assert not hasattr(module, "emit_shadow_snapshot")


def test_the_argparse_converter_rejects_non_positive_values() -> None:
    """The consolidated converter must still RAISE (argparse turns this into a usage error),
    which is exactly what distinguishes it from the ``_config_resolvers`` namesake."""
    import argparse

    # NOT imported from rebar_reconciler.request: `src/rebar/_engine/` has no
    # `__init__.py`, so those modules are loaded by path (spec_from_file_location) and
    # imported FLAT as `rebar_reconciler.*` — see tests/unit/rebar_reconciler/conftest.py.
    # The consolidated owner is an ordinary module, which is exactly why it can serve all
    # three call sites; the reconciler copy's disappearance is proven by the AST count above.
    from rebar._cli._parsers._common import _positive_int

    assert _positive_int("3") == 3
    for bad in ("0", "-1", "x", ""):
        with pytest.raises((argparse.ArgumentTypeError, ValueError)):
            _positive_int(bad)


def test_the_config_namesake_still_returns_its_default_rather_than_raising() -> None:
    """The negative control that makes the consolidation safe: proving the namesake keeps its
    OPPOSITE contract is what stops a future reader merging it into the converter."""
    from rebar._config_resolvers import _positive_int as cfg_positive_int

    assert cfg_positive_int(None, 7) == 7
    assert cfg_positive_int("not-a-number", 7) == 7
    assert cfg_positive_int("0", 7) == 7
    assert cfg_positive_int("12", 7) == 12


def test_ticket_id_prefers_the_explicit_input_then_falls_back_to_the_target() -> None:
    """The consolidated helper's actual CONTRACT, not just its uniqueness. A step that silently
    acts on the WRONG ticket is worse than one that refuses, so the resolution order and the
    refusal are the behaviour worth pinning — a definition count would be satisfied by a single
    copy that resolved incorrectly."""
    import types

    from rebar.llm.workflow.step_contracts import _ticket_id

    def ctx(inputs, target):
        return types.SimpleNamespace(inputs=inputs, target_ticket=target, step_id="s1")

    # explicit input wins over the run target
    assert _ticket_id(ctx({"ticket_id": "aaaa-1111"}, "bbbb-2222")) == "aaaa-1111"
    # absent or empty input falls back to the target
    assert _ticket_id(ctx({}, "bbbb-2222")) == "bbbb-2222"
    assert _ticket_id(ctx({"ticket_id": ""}, "bbbb-2222")) == "bbbb-2222"
    # a non-str id is coerced, so downstream string handling cannot break on it
    assert _ticket_id(ctx({"ticket_id": 1234}, None)) == "1234"


def test_ticket_id_refuses_rather_than_guessing_when_no_ticket_is_available() -> None:
    """With neither an input nor a target there is no correct answer, so the helper must RAISE.
    The message names the step and both ways to supply one — a step failing here should tell the
    author what to write, not just that something was missing."""
    import types

    from rebar.llm.workflow.step_contracts import _ticket_id

    ctx = types.SimpleNamespace(inputs={}, target_ticket=None, step_id="verify-step")
    with pytest.raises(ValueError) as excinfo:
        _ticket_id(ctx)
    msg = str(excinfo.value)
    assert "verify-step" in msg
    assert "ticket_id" in msg and "target ticket" in msg
