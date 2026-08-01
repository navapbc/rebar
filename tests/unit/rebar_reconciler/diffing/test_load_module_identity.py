"""The two invariants of the diffing suites' cache-first ``_load_module``
(bug 9f0b-3d48-b935-428b).

WHAT 9f0b WAS. The by-path loader used ``sys.modules.setdefault(name, mod)`` and then
``exec_module``'d the freshly built module regardless. When another registrant had already
claimed ``name``, ``setdefault`` KEPT the old object while the caller received the new one —
so two distinct class objects for the same source file existed at once and ``isinstance``
answered False across them. It surfaced on macOS CI and not on Linux purely because
collection order differs, which is the signature of an order-dependent cache bug.

The repair (commit 7e3c50242d, "9f0b: unify by-path test module identity") made the loader
cache-FIRST: return the module already registered under ``name`` when its ``__file__``
matches the requested path, and otherwise build, register and execute a fresh one.

WHY THIS FILE EXISTS. That repair shipped with NO test of either half. The close gate was
right to refuse the ticket: the fix was real, but nothing pinned it, so a future edit could
restore ``setdefault`` — or drop the ``__file__`` guard — and every suite would go green.
The two halves pull in OPPOSITE directions and so are asserted separately:

  * SAME name + SAME path must return the SAME object, or one source file yields two class
    objects and ``isinstance`` starts answering False (the original defect);
  * SAME name + DIFFERENT path must NOT return the cached module, or the loader silently
    hands back the wrong file's code (the defect the naive "just cache on name" fix would
    have introduced in its place).

Asserted against the REAL helper, loaded out of the diffing suite by path rather than
re-implemented here — a restated copy would keep passing while the original rotted, which is
the same reasoning `test_user_guide_commands.py` uses for parsing the guide at runtime.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

# The three diffing suites carry byte-identical copies of the helper; test_differ.py is the
# canonical one. Reading it from disk means this test tracks the shipped code, not a copy.
_DIFFER_TEST = Path(__file__).resolve().parent / "test_differ.py"


@pytest.fixture
def restore_sys_modules() -> Iterator[None]:
    """Undo every ``sys.modules`` key this test adds.

    Mandatory, not hygiene theatre: this file's whole subject is a module-cache leak across
    test files (the 4cc1 leakage class 9f0b belongs to). A test that proved the invariant
    while itself leaking would be causing the bug it documents.
    """
    before = dict(sys.modules)
    try:
        yield
    finally:
        for name in set(sys.modules) - set(before):
            del sys.modules[name]
        sys.modules.update(before)


@pytest.fixture
def load_module(restore_sys_modules: None) -> Callable[[str, Path], ModuleType]:
    """The REAL ``_load_module`` from the diffing suite, loaded by path.

    Registered under a probe-specific name so importing the suite here cannot collide with
    pytest's own import of it during a normal run.
    """
    spec = importlib.util.spec_from_file_location("_j9f0b_probe_test_differ", _DIFFER_TEST)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_j9f0b_probe_test_differ"] = mod
    spec.loader.exec_module(mod)
    helper = getattr(mod, "_load_module", None)
    assert callable(helper), (
        f"{_DIFFER_TEST.name} no longer defines `_load_module`; 9f0b's invariants are pinned "
        "against that helper, so this test must be repointed rather than deleted"
    )
    return helper  # type: ignore[no-any-return]


def _write_module(tmp_path: Path, name: str, marker: str) -> Path:
    path = tmp_path / name
    path.write_text(
        f"class Probe:\n    pass\nMARKER = {marker!r}\n",
        encoding="utf-8",
    )
    return path


def test_same_name_and_path_returns_the_identical_module_object(
    load_module: Callable[[str, Path], ModuleType], tmp_path: Path
) -> None:
    """ONE object per (name, path) — the invariant whose absence WAS bug 9f0b.

    Asserting on the CLASS as well as the module is the part that matters. Two module objects
    holding equal-but-distinct ``Probe`` classes is exactly the state that made ``isinstance``
    return False for an object built by "the same" code, and a module-level ``is`` check alone
    would not have caught the original ``setdefault`` mismatch.
    """
    source = _write_module(tmp_path, "probe_same.py", "first")

    first = load_module("j9f0b_probe_same", source)
    second = load_module("j9f0b_probe_same", source)

    assert first is second, (
        "the loader built a SECOND module object for the same name+path; one source file now "
        "yields two distinct class objects and isinstance will answer False across them — "
        "this is bug 9f0b exactly"
    )
    assert first.Probe is second.Probe, (
        "the modules compare identical but their classes do not, which is the failure mode "
        "9f0b actually manifested as (order-dependent isinstance failures)"
    )
    assert sys.modules["j9f0b_probe_same"] is first, (
        "the object handed to the caller is not the one left in sys.modules — the "
        "setdefault-vs-exec_module mismatch that caused 9f0b"
    )


def test_same_name_but_a_different_path_does_not_reuse_the_cached_module(
    load_module: Callable[[str, Path], ModuleType], tmp_path: Path
) -> None:
    """The ``__file__`` guard: caching on the NAME alone would serve the wrong file's code.

    This is the counterweight to the test above. Fixing 9f0b by simply returning whatever sits
    under ``name`` would make the loader hand back a module loaded from a DIFFERENT source —
    silently, and with no error anywhere. Both properties have to hold at once, so both are
    pinned; a change that satisfies only one turns the other red.
    """
    first_source = _write_module(tmp_path, "probe_a.py", "first")
    second_source = _write_module(tmp_path, "probe_b.py", "second")

    first = load_module("j9f0b_probe_shared_name", first_source)
    assert first.MARKER == "first"

    second = load_module("j9f0b_probe_shared_name", second_source)

    assert second.MARKER == "second", (
        f"the loader returned the CACHED module for a different path: MARKER is "
        f"{second.MARKER!r}, expected 'second'. Callers asking for {second_source.name} would "
        f"silently receive {first_source.name}'s code."
    )
    assert second is not first, "a different path must produce a different module object"
    assert (second.__file__ or "") == str(second_source), (
        f"the returned module's __file__ is {second.__file__!r}, which does not name the "
        f"requested path {str(second_source)!r}"
    )
    # THIS is the assertion that reproduces 9f0b itself, and it belongs HERE rather than in
    # the same-path test above. The original defect needed the name to be ALREADY occupied by
    # a different-path module: only then does `sys.modules.setdefault(name, mod)` keep the OLD
    # object while `exec_module` runs — and returns — the NEW one, leaving the caller holding a
    # module that is not the one registered under its own name. That is the two-class-objects
    # state that made isinstance answer False. The same-path test cannot catch it, because
    # cache-first short-circuits before ever reaching the registration line (verified: with
    # `setdefault` restored, that test still passes).
    assert sys.modules["j9f0b_probe_shared_name"] is second, (
        "the loader returned a module that is NOT the one registered under its name — "
        "`sys.modules[name]` still holds the previously-registered module. This is bug 9f0b "
        "exactly: a self-referential import inside the module resolves to the OTHER object, "
        "so one source file yields two class objects and isinstance answers False."
    )
