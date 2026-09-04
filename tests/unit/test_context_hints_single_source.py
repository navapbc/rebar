"""Guard: the provider context-length phrasing list has exactly ONE definition.

Story fcb7-e6e2-7244-42d2. The eight-phrase tuple that decides "does this provider error
look like a context-window overflow?" used to be copied byte-for-byte into
``rebar.llm.plan_review.sizing``. Two copies means a fix to one silently leaves the other
stale, and the two classifiers then disagree about the same wire error.

The guard matches the CONJUNCTION of all eight phrases as *distinct string literals* in one
module — never a single common token such as ``"context"``, which appears innocently all over
the tree. Prose that merely quotes the phrases inside one docstring (``run_failure.py``,
``enrich.py``) is one literal, not eight, so it is left alone; so is
``workflow.completion_failures``, whose token-exhaustion classifier is a deliberately
DIFFERENT list.

A module that genuinely needs its own copy opts out with a line comment
``# context-hints-ok: <reason>`` — the reason is mandatory.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from rebar.llm import failure
from rebar.llm.plan_review import sizing

SRC_ROOT = Path(failure.__file__).resolve().parents[2]
OWNER = SRC_ROOT / "rebar" / "llm" / "failure.py"

#: The conjunction the guard keys on. Deliberately spelled out here rather than imported
#: from the owner: importing it would make the guard follow a rename instead of noticing it.
CONTEXT_LEN_PHRASES = frozenset(
    {
        "context",
        "too many tokens",
        "maximum context",
        "context_length",
        "prompt is too long",
        "input length",
        "exceeds the maximum",
        "token limit",
    }
)

_ESCAPE_RE = re.compile(r"#[ \t]*context-hints-ok:[ \t]*(?P<reason>\S.*)")


def _string_literals(source: str) -> set[str]:
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _escape_reason(source: str) -> str | None:
    """The opt-out reason, or ``None`` when the file carries no *reasoned* marker.

    ``_ESCAPE_RE`` is the single place the "a reason is mandatory" rule lives: its
    ``\\S`` is what refuses a bare ``# context-hints-ok:``.
    """
    match = _ESCAPE_RE.search(source)
    return match.group("reason") if match else None


def definitions_under(root: Path) -> list[Path]:
    """Every ``*.py`` under ``root`` that spells out the whole phrase conjunction."""
    found: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "too many tokens" not in source:  # cheap reject before parsing
            continue
        if not CONTEXT_LEN_PHRASES <= _string_literals(source):
            continue
        if _escape_reason(source) is not None:
            continue
        found.append(path)
    return found


# ── The guard itself ──────────────────────────────────────────────────────────


def test_the_phrase_list_is_defined_exactly_once_under_src():
    assert definitions_under(SRC_ROOT) == [OWNER]


# ── The guard is proven to FLAG, not merely to pass ───────────────────────────


def _write_copy(directory: Path, name: str, *, trailer: str = "") -> Path:
    body = ",\n    ".join(repr(phrase) for phrase in sorted(CONTEXT_LEN_PHRASES))
    path = directory / name
    path.write_text(f"HINTS = (\n    {body},\n){trailer}\n", encoding="utf-8")
    return path


def test_a_second_copy_is_flagged(tmp_path: Path):
    copy = _write_copy(tmp_path, "sneaky.py")
    assert definitions_under(tmp_path) == [copy]


def test_a_partial_copy_is_not_flagged(tmp_path: Path):
    """Seven of the eight phrases is a different list, not a copy — the guard needs the
    whole conjunction, so it cannot fire on an unrelated module that happens to say
    "context"."""
    partial = sorted(CONTEXT_LEN_PHRASES - {"token limit"})
    body = ",\n    ".join(repr(phrase) for phrase in partial)
    (tmp_path / "partial.py").write_text(f"HINTS = (\n    {body},\n)\n", encoding="utf-8")
    assert definitions_under(tmp_path) == []


def test_prose_quoting_the_phrases_is_not_flagged(tmp_path: Path):
    """One docstring naming all eight is ONE literal, not eight — docs stay legal."""
    prose = " / ".join(sorted(CONTEXT_LEN_PHRASES))
    (tmp_path / "doc.py").write_text(f'"""Matches: {prose}."""\n', encoding="utf-8")
    assert definitions_under(tmp_path) == []


def test_the_escape_marker_requires_a_reason(tmp_path: Path):
    excused = _write_copy(tmp_path, "excused.py", trailer="  # context-hints-ok: vendored")
    bare = _write_copy(tmp_path, "bare.py", trailer="  # context-hints-ok:")
    assert definitions_under(tmp_path) == [bare]
    assert excused.exists()


# ── Behaviour: the surviving predicate reads the owner's list at call time ────


def test_sizing_predicate_follows_the_owner_module(monkeypatch: pytest.MonkeyPatch):
    """Patching the OWNER's tuple changes what ``sizing`` recognises. A private copy in
    ``sizing`` would keep answering from its own list and this would fail."""
    monkeypatch.setattr(failure, "_CONTEXT_LEN_HINTS", ("flibbertigibbet",))

    assert sizing.is_context_limit_error(Exception("a flibbertigibbet occurred")) is True
    assert sizing.is_context_limit_error(Exception("prompt is too long")) is False


def test_sizing_still_exposes_a_patchable_predicate():
    """``orchestrator`` binds this by value at import time and a prerequisites test
    monkeypatches it by name, so it must stay a real attribute of ``sizing``."""

    assert callable(sizing.is_context_limit_error)
    assert sizing.is_context_limit_error is sizing.is_context_limit_error


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("the request exceeds the maximum context length", True),
        ("prompt is too long: 300000 tokens", True),
        ("the model refused to answer", False),
        ("connection reset by peer", False),
    ],
)
def test_the_predicate_keeps_its_verdicts(message: str, expected: bool):
    assert sizing.is_context_limit_error(Exception(message)) is expected
