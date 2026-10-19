"""Every public library callable carries a complete parameter annotation (bossy-poetic-polliwog).

The MCP-derivation story derives tool input schemas from these signatures, and FastMCP maps an
unannotated parameter to ``{"type": "string"}``, so an unannotated parameter would advertise a
wrong schema. The facade modules use ``from __future__ import annotations``, so
``inspect.signature`` returns each annotation as its source STRING; these tests read those strings
and never evaluate them (several name ``TYPE_CHECKING``-only types such as ``dsse`` and
``FileImpactEntry``, which would raise ``NameError`` if evaluated).
"""

from __future__ import annotations

import inspect

import pytest

import rebar

REPO_ROOT_ANNOTATION = "str | os.PathLike[str] | None"
REPO_ROOT_FUNCTION_COUNT = 61


def _public_signatures(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, inspect.Signature]]:
    """Signatures of every public callable in ``rebar.__all__``, read from the REAL facade.

    ``tests/unit/conftest.py`` replaces ``rebar.append_session_log`` with a ``(*args, **kwargs)``
    no-op for every unit test; undoing this test's monkeypatch restores it so the census reads
    the real signature. Introspection only; nothing here writes the store."""
    monkeypatch.undo()
    out = []
    for name in rebar.__all__:
        obj = getattr(rebar, name)
        if inspect.isclass(obj) or not callable(obj):
            continue
        try:
            out.append((name, inspect.signature(obj)))
        except (TypeError, ValueError):  # builtins without an introspectable signature
            continue
    return out


def test_every_public_callable_parameter_is_annotated(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = [
        f"{name}.{param.name}"
        for name, sig in _public_signatures(monkeypatch)
        for param in sig.parameters.values()
        if param.annotation is inspect.Parameter.empty
    ]
    assert missing == [], f"{len(missing)} unannotated public parameter(s): {missing}"


def test_repo_root_annotation_is_uniform(monkeypatch: pytest.MonkeyPatch) -> None:
    spellings = {
        name: sig.parameters["repo_root"].annotation
        for name, sig in _public_signatures(monkeypatch)
        if "repo_root" in sig.parameters
    }
    assert len(spellings) == REPO_ROOT_FUNCTION_COUNT
    wrong = {name: ann for name, ann in spellings.items() if ann != REPO_ROOT_ANNOTATION}
    assert wrong == {}, f"repo_root spelled differently: {wrong}"
