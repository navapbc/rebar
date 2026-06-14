"""Tests for rebar_reconciler._errors."""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
ERRORS_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_errors.py"
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"

# The 9 canonical applier leaf names as of draft-2/draft-6.
# This set is the single source of truth for drift detection — if a leaf is
# added, renamed, or removed, this set and the docstring contract MUST be
# updated together.
_CANONICAL_9_LEAVES = frozenset(
    {
        "outbound_create",
        "outbound_update",
        "outbound_delete",
        "outbound_probe",
        "outbound_conflict",
        "inbound_create",
        "inbound_update",
        "inbound_clean_label",
        "inbound_repair_property",
    }
)


def _load_errors():
    spec = importlib.util.spec_from_file_location("_errors", ERRORS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_applier():
    # Register under the canonical module name so that Python's dataclass
    # machinery (which looks up cls.__module__ in sys.modules) can resolve
    # the annotation strings for ApplyResult's slots=True dataclass.
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("applier", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def errs():
    return _load_errors()


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


def test_direction_mismatch_is_exception_subclass(errs):
    assert issubclass(errs.DirectionMismatchError, Exception)


def test_unknown_action_is_exception_subclass(errs):
    assert issubclass(errs.UnknownActionError, Exception)


def test_status_mapping_is_exception_subclass(errs):
    assert issubclass(errs.StatusMappingError, Exception)


def test_str_preserves_message(errs):
    for cls in (
        errs.DirectionMismatchError,
        errs.UnknownActionError,
        errs.StatusMappingError,
    ):
        e = cls("boom")
        assert str(e) == "boom"


# ---------------------------------------------------------------------------
# New tests for RebarIdLabelWriteError and _AUTHORIZED_REBAR_ID_LABEL_WRITERS
# ---------------------------------------------------------------------------


def test_rebar_id_label_write_error_is_exception_subclass(errs):
    """RebarIdLabelWriteError must be importable from _errors and subclass Exception."""
    assert issubclass(errs.RebarIdLabelWriteError, Exception)


def test_rebar_id_label_write_error_str_preserves_message(errs):
    """RebarIdLabelWriteError must carry the message string through str()."""
    e = errs.RebarIdLabelWriteError("unauthorized write attempt")
    assert "unauthorized write attempt" in str(e)


def test_authorized_writers_frozenset_value(applier):
    """_AUTHORIZED_REBAR_ID_LABEL_WRITERS must equal exactly the three authorized leaves."""
    assert applier._AUTHORIZED_REBAR_ID_LABEL_WRITERS == frozenset(
        {"inbound_clean_label", "outbound_create", "inbound_create"}
    )


def test_authorized_writers_is_frozenset(applier):
    """_AUTHORIZED_REBAR_ID_LABEL_WRITERS must be a frozenset (immutable, hashable)."""
    assert isinstance(applier._AUTHORIZED_REBAR_ID_LABEL_WRITERS, frozenset)


def test_authorized_writers_docstring_documents_full_contract(applier):
    """The _AUTHORIZED_REBAR_ID_LABEL_WRITERS docstring must document the full 9-leaf contract.

    Asserts that the docstring (stored as __doc__ on the constant's hosting
    module-level object, or in the module docstring — the convention is to
    attach it as a comment-style docstring via a dedicated sentinel) mentions:
      - All 9 applier leaf names
      - 'conflict_resolver' (per-element provenance skip requirement)
      - 'inbound_repair_property' (property field, NOT label)

    Because Python frozenset constants cannot carry __doc__, the contract text
    MUST appear in _AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC (a string constant)
    OR in the module-level docstring of applier.py.
    """
    # Collect candidate docstring sources: module docstring + dedicated doc constant.
    candidates = []
    if applier.__doc__:
        candidates.append(applier.__doc__)
    doc_attr = getattr(applier, "_AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC", None)
    if doc_attr:
        candidates.append(str(doc_attr))

    # At least one source must exist.
    assert candidates, (
        "No docstring source found: applier.__doc__ is None and "
        "_AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC is not defined"
    )

    full_text = "\n".join(candidates)

    required_names = _CANONICAL_9_LEAVES | {
        "conflict_resolver",
        "inbound_repair_property",
    }
    missing = [name for name in sorted(required_names) if name not in full_text]
    assert not missing, (
        f"Contract docstring is missing these names: {missing}\n"
        f"Full text searched:\n{full_text[:500]}"
    )
