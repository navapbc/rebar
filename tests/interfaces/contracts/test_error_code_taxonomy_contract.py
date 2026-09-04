"""Epic 3405-c0b7-0436-4b46 prevention guardrail — the exhaustive ``exception -> code`` contract
for the ``LLMError`` family, plus vocabulary-closure over ``KNOWN_ERROR_CODES``.

This is the epic's AC6 guardrail: it freezes the RECONCILED error-code taxonomy so a future
change cannot silently re-misclassify an ``LLMError`` subclass or emit an unregistered code.

Reconciled taxonomy (by exception TYPE, precedence high→low):
  * ``WorkflowNotFoundError``                                  -> ``not_found``      (dbca)
  * ``WorkflowParse/Validation/Version/UnknownStepError``      -> ``invalid_input``  (dbca)
  * ``LLMUnavailableError`` (incl. ``LLMConfigError`` + prompt subtree)  -> ``llm_unavailable``
  * ``LLMRunnerError`` subtree (input-rejected / budget / context-window /
    tool-loop / output-defect)                                -> ``command_failed`` (f75f)
  * bare ``WorkflowError`` base                                -> ``llm_unavailable`` (dbca)
  * bare ``LLMError``, ``FindingsError``, ``EvalError``,
    ``SnapshotError``, ``WorkflowAssetsUnavailableError``       -> ``command_failed`` (ce6b)
  * ``ExpressionError``                                        -> ``invalid_input``  (73d8)

The map is asserted to be EXHAUSTIVE: the set of live ``LLMError`` subclasses must equal the
keys below, so adding a new subclass FAILS this test until its intended code is recorded here —
that forced decision is the prevention this guardrail exists to provide. It must AGREE with, not
contradict, ``tests/interfaces/contracts/test_workflow_error_codes_dbca.py``.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import rebar.llm
import rebar.llm.errors as llm_errors
from rebar._errors import KNOWN_ERROR_CODES, error_code_for

pytestmark = pytest.mark.unit


def _materialize_llm_error_tree() -> None:
    """Import every ``rebar.llm`` submodule so the ``LLMError`` subclass tree is FULLY and
    DETERMINISTICALLY registered. ``type.__subclasses__()`` only sees classes whose defining
    module has been imported, and the family is spread across many modules (findings, evals,
    prompting, workflow.snapshot/executor/prompt_authoring, …). Without this, the exhaustive
    assertions below would depend on incidental import order. Best-effort: a submodule that
    cannot import in this environment (e.g. an optional-extra edge) is skipped, not fatal."""
    for mod in pkgutil.walk_packages(rebar.llm.__path__, "rebar.llm."):
        try:
            importlib.import_module(mod.name)
        except Exception:  # noqa: BLE001 — an unimportable optional submodule must not break the census
            pass


_materialize_llm_error_tree()


#: Every live ``LLMError`` family class → the vocabulary code ``error_code_for`` must assign it,
#: encoding the reconciled taxonomy. Keyed by class name so the set-equality assertion below
#: forces a conscious decision when the hierarchy grows.
EXPECTED_CODE: dict[str, str] = {
    # availability faults — the only members that keep ``llm_unavailable`` by their own type
    "LLMError": "command_failed",
    # admission refusals: the gate never started, so neither a runner failure nor an outage
    "GateCongestedError": "gate_congested",
    "GateScratchUnavailableError": "gate_scratch_unavailable",
    "LLMUnavailableError": "llm_unavailable",
    "LLMConfigError": "llm_unavailable",
    "PromptError": "llm_unavailable",
    "PromptVersionError": "llm_unavailable",
    "PromptNotFound": "llm_unavailable",
    "PromptWriteError": "llm_unavailable",
    "LibraryWriteError": "llm_unavailable",
    "InvalidPromptIdError": "llm_unavailable",
    "PromptExistsError": "llm_unavailable",
    "ReviewerError": "llm_unavailable",
    # bare LLMError-family leaves with no availability/runner/workflow role → command_failed
    "FindingsError": "command_failed",
    "EvalError": "command_failed",
    # runner subtree — NOT an availability fault (f75f): honest broad ``command_failed``
    "LLMRunnerError": "command_failed",
    "LLMBudgetExhaustedError": "command_failed",
    "ContextWindowExceededError": "command_failed",
    "LLMInputRejectedError": "command_failed",
    "RunawayToolLoopError": "command_failed",
    "StructuredOutputError": "command_failed",
    "UnretryableOutputError": "command_failed",
    "CompletionRecoveryError": "command_failed",
    # workflow caller-input subtree — dbca's finer, accurate codes (do NOT revert)
    "WorkflowError": "llm_unavailable",  # bare base: an EXECUTE-time LLM outage stays availability
    "WorkflowNotFoundError": "not_found",
    "WorkflowUnknownStepError": "invalid_input",
    "WorkflowParseError": "invalid_input",
    "WorkflowValidationError": "invalid_input",
    "WorkflowVersionError": "invalid_input",
    "WorkflowAssetsUnavailableError": "command_failed",
    "SnapshotError": "command_failed",
    "ExpressionError": "invalid_input",
}


def _all_subclasses(cls: type) -> set[type]:
    out: set[type] = set()
    for sub in cls.__subclasses__():
        out.add(sub)
        out |= _all_subclasses(sub)
    return out


def _family() -> dict[str, type]:
    fam = {llm_errors.LLMError.__name__: llm_errors.LLMError}
    for sub in _all_subclasses(llm_errors.LLMError):
        fam[sub.__name__] = sub
    return fam


def test_llm_error_family_is_exhaustively_mapped() -> None:
    """Every live ``LLMError`` subclass is accounted for — no more, no less. A new subclass added
    without recording its intended code here fails this assertion, forcing the taxonomy decision
    that the epic exists to guarantee."""
    assert set(_family()) == set(EXPECTED_CODE)


def test_error_code_for_matches_reconciled_taxonomy_by_type() -> None:
    """``error_code_for`` classifies every family member to its reconciled code, discriminating
    purely by exception TYPE (a synthesized bare instance carries no ``error_code`` attribute, so
    only the type-dispatch branches can fire)."""
    for name, cls in _family().items():
        inst = cls.__new__(cls)  # type: ignore[misc] — type-dispatch only, no __init__ state
        assert error_code_for(inst) == EXPECTED_CODE[name], name


def test_reconciled_codes_are_registered_vocabulary() -> None:
    """Vocabulary-closure: every code the taxonomy assigns is a member of ``KNOWN_ERROR_CODES``,
    and the specific ``pinned_ticket_read_failed`` code (ticket 0aee — emitted by the pinned
    ticket-view read path) is registered too."""
    assert set(EXPECTED_CODE.values()) <= set(KNOWN_ERROR_CODES)
    assert "pinned_ticket_read_failed" in KNOWN_ERROR_CODES


def _returned_code_literals() -> set[str]:
    """Every string constant returned anywhere inside ``error_code_for`` and its ``_*_code``
    helpers, harvested from the source AST — the exhaustive set of codes the central classifier
    can emit."""
    src = Path(llm_errors.__file__).parent.parent / "_errors.py"
    tree = ast.parse(src.read_text())
    targets = {"error_code_for", "_workflow_caller_code", "_llm_error_code"}
    codes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in targets:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Constant):
                    if isinstance(inner.value.value, str):
                        codes.add(inner.value.value)
    return codes


def test_classifier_never_returns_an_unregistered_code() -> None:
    """Prevention: every code literal ``error_code_for`` (and its helpers) can return is in
    ``KNOWN_ERROR_CODES``. A future ``return "new_code"`` added without registering it fails
    here — the vocabulary contract can never silently drift open."""
    returned = _returned_code_literals()
    assert returned, "expected to harvest at least one returned code literal"
    unregistered = returned - set(KNOWN_ERROR_CODES)
    assert not unregistered, f"error_code_for returns unregistered codes: {sorted(unregistered)}"
