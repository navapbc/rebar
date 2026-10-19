"""Keep ``docs/env-vars.md`` complete for every ``_llm_*`` environment resolver.

The registry generator's AST scan recognizes only helpers and argument positions in
``KNOWN_ENV_HELPERS``. These tests ensure new resolvers cannot silently disappear
from generated documentation, as ``_llm_float`` once did.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# The `_llm_*` env-reading helpers live in
# `config_readers.py` (extracted from `config.py` for module-size headroom, ticket 02b7); this AST
# probe follows them so it keeps finding the same function definitions it always has.
_CONFIG_PY = REPO_ROOT / "src" / "rebar" / "llm" / "config_readers.py"

# Read through `_llm_float`, and therefore invisible to the generator before b00f.
_FLOAT_VARS = ("REBAR_LLM_TEMPERATURE",)


def _gen_env_registry():
    """Load the generator by path — it lives in `scripts/`, which is not an importable package.
    Mirrors `tests/unit/test_llm_config_pointer.py:251`."""
    path = REPO_ROOT / "scripts" / "gen_env_registry.py"
    spec = importlib.util.spec_from_file_location("gen_env_registry_b00f", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _llm_helpers_with_env_arg() -> dict[str, int]:
    """Every `_llm_*` function defined in `config.py` that actually TAKES an env-var name, mapped to
    the 0-indexed position of that parameter.

    Keyed off the `env_name` parameter rather than the `_llm_` prefix, because the prefix alone
    can over-match: an `_llm_*` coercion helper that never touches the environment must not be
    required to be registered.
    """
    module = ast.parse(_CONFIG_PY.read_text(encoding="utf-8"))
    found: dict[str, int] = {}
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_llm_"):
            names = [a.arg for a in node.args.args]
            if "env_name" in names:
                found[node.name] = names.index("env_name")
    return found


def test_the_llm_float_vars_are_visible_to_the_generator() -> None:
    """The scan must SEE the `_llm_float` reads. Fails before b00f: the helper is not in
    `KNOWN_ENV_HELPERS`, so the scanner walks straight past every call to it."""
    gen = _gen_env_registry()
    reads, _dynamic = gen.scan(gen.DEFAULT_SCAN_ROOT)
    missing = [v for v in _FLOAT_VARS if v not in reads]
    assert missing == [], f"not visible to the generator: {missing}"


def test_the_committed_registry_documents_the_llm_float_vars() -> None:
    """A visible read plus a stale committed doc is still an undocumented variable — the doc is the
    artifact operators actually read, so assert on the committed file, not just the scan."""
    doc = (REPO_ROOT / "docs" / "env-vars.md").read_text(encoding="utf-8")
    missing = [v for v in _FLOAT_VARS if f"`{v}`" not in doc]
    assert missing == [], f"absent from the committed docs/env-vars.md: {missing}"


def test_every_env_reading_llm_helper_is_registered_at_the_right_position() -> None:
    """Register every ``_llm_*`` resolver at the ``env_name`` parameter position."""
    gen = _gen_env_registry()
    registered = gen.KNOWN_ENV_HELPERS
    expected = _llm_helpers_with_env_arg()
    assert expected, "found no _llm_* helper taking env_name — the AST probe itself has broken"

    unregistered = sorted(name for name in expected if name not in registered)
    assert unregistered == [], (
        f"{unregistered} read an env var but are absent from KNOWN_ENV_HELPERS, so every variable "
        "they read is silently missing from docs/env-vars.md"
    )

    wrong_position = {
        name: (registered[name], idx) for name, idx in expected.items() if registered[name] != idx
    }
    assert wrong_position == {}, f"registered index != actual env_name position: {wrong_position}"


def test_no_registered_llm_helper_lacks_an_env_name_parameter() -> None:
    """The negative control that stops the guard above from degenerating into 'register everything
    beginning with _llm_': every registered `_llm_*` helper defined here must actually take an
    `env_name`, because registering one that reads no environment variable would make the
    scanner mis-file whatever sits at its argument index. (The original control, an enum
    coercion helper, was retired with the overlap detector, ADR 0086.)"""
    gen = _gen_env_registry()
    module = ast.parse(_CONFIG_PY.read_text(encoding="utf-8"))
    defined = {
        n.name for n in module.body if isinstance(n, ast.FunctionDef) and n.name.startswith("_llm_")
    }
    registered_here = {name for name in gen.KNOWN_ENV_HELPERS if name in defined}
    assert registered_here, "no registered _llm_* helper found here — the probe itself has broken"
    assert registered_here <= set(_llm_helpers_with_env_arg()), (
        f"registered helpers without an env_name parameter: "
        f"{sorted(registered_here - set(_llm_helpers_with_env_arg()))}"
    )


def test_dropping_a_used_helpers_row_fails_loudly_instead_of_shrinking(tmp_path) -> None:
    """Require a scan with a missing used-helper row to fail and name that helper."""
    gen = _gen_env_registry()
    baseline, _dynamic = gen.scan(gen.DEFAULT_SCAN_ROOT)
    assert all(v in baseline for v in _FLOAT_VARS), "precondition: the row resolves them today"

    del gen.KNOWN_ENV_HELPERS["_llm_float"]
    try:
        reads, _dyn = gen.scan(gen.DEFAULT_SCAN_ROOT)
    except RuntimeError as exc:
        refusal: BaseException = exc
    else:
        lost = sorted(set(baseline) - set(reads))
        raise AssertionError(
            f"the scan returned silently after losing {len(lost)} variable(s): {lost} -- "
            "a missing row for a live helper must fail loudly, not shrink the registry"
        )
    assert "_llm_float" in str(refusal), "the refusal must name the helper whose row is missing"
