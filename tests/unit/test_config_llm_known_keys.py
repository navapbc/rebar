"""``_LLM_KNOWN_KEYS`` must mirror what the ``[llm]`` readers actually read.

The core config parser cannot import ``rebar.llm`` — that is the optional ``[agents]``
extra, and the stdlib core deliberately never touches the agents stack — so the list of
honoured ``[tool.rebar.llm]`` keys is duplicated in ``_config_sections``. A duplicated
list silently rots: a new reader lands and its key starts warning as unknown, or a
reader is deleted and a dead key stays accepted forever.

This test removes the rot by deriving the truth from the readers themselves. It
AST-walks them rather than importing, so it costs nothing at runtime and works whether
or not the extra is installed. Equality is exact in both directions on purpose — a
one-sided subset check would let either failure mode through.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from rebar._config_sections import _LLM_KNOWN_KEYS

pytestmark = pytest.mark.unit

_LLM_DIR = pathlib.Path(__file__).resolve().parents[2] / "src" / "rebar" / "llm"

# ``_llm_str(table, cli, env_name, file_key, default)`` and its int/float/source twins
# all take the config key as the 4th POSITIONAL argument.
_READER_FUNCS = frozenset({"_llm_str", "_llm_int", "_llm_float", "_llm_str_source"})
_FILE_KEY_ARG = 3

# Modules that read a sub-table straight off the ``[llm]`` table.
_GETTER_MODULES = ("config.py", "config_readers.py", "headers.py", "model_classes.py")


def _reader_keys() -> set[str]:
    """Keys passed as ``file_key`` to the ``_llm_*`` readers in ``config_readers.py``."""
    tree = ast.parse((_LLM_DIR / "config_readers.py").read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id not in _READER_FUNCS or len(node.args) <= _FILE_KEY_ARG:
            continue
        arg = node.args[_FILE_KEY_ARG]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.add(arg.value)
    return out


def _is_llm_table(recv: ast.expr) -> bool:
    """True for the two receivers that ARE the ``[llm]`` table: the local ``table`` name
    and a direct ``_read_llm_file_table(...)`` call."""
    if isinstance(recv, ast.Name) and recv.id == "table":
        return True
    return (
        isinstance(recv, ast.Call)
        and isinstance(recv.func, ast.Name)
        and recv.func.id == "_read_llm_file_table"
    )


def _sub_table_keys() -> set[str]:
    """Literal keys fetched off the ``[llm]`` table via ``.get("<key>")``."""
    out: set[str] = set()
    for mod in _GETTER_MODULES:
        tree = ast.parse((_LLM_DIR / mod).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "get" or not _is_llm_table(node.func.value):
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    out.add(value)
    return out


def _collected() -> set[str]:
    """Every top-level ``[llm]`` key any reader reads.

    Dotted names are dropped: ``evals.corpus_root`` is a SYNTHETIC flattened key built
    in ``_evals_corpus_root`` so the file and env layers can share one ``file_key``. It
    never appears in a real config file — the operator writes ``[llm.evals] corpus_root``
    — and its top-level parent ``evals`` is collected separately.
    """
    return {key for key in _reader_keys() | _sub_table_keys() if "." not in key}


def test_llm_known_keys_match_config_readers() -> None:
    collected = _collected()
    known = set(_LLM_KNOWN_KEYS)

    assert known == collected, (
        f"_LLM_KNOWN_KEYS is out of step with the readers.\n"
        f"  accepted but never read: {sorted(known - collected)}\n"
        f"  read but would warn as unknown: {sorted(collected - known)}"
    )


def test_the_extractor_actually_finds_both_reader_shapes() -> None:
    """Guards the test itself. Both AST shapes must contribute, or a refactor that
    renames one could empty its half and leave the equality check passing vacuously
    against a correspondingly shrunken list."""
    assert len(_reader_keys()) >= 10, _reader_keys()
    assert {"mcp_servers", "headers", "model_classes", "evals"} <= _sub_table_keys()


def test_known_keys_are_sorted_and_unique() -> None:
    """A duplicated entry would make the set comparison above hide it."""
    assert len(set(_LLM_KNOWN_KEYS)) == len(_LLM_KNOWN_KEYS)
    assert list(_LLM_KNOWN_KEYS) == sorted(_LLM_KNOWN_KEYS)
