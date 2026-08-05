"""Deterministic scoring/cap/cell logic for the structured-output measurement harness (a40f).

WHY THIS EXISTS. The live measurement harness
(``tests/external/test_structured_output_matrix.py``) makes real, billable model calls to
measure how the df3a schema-filtered selection parser (and the sentinel sibling's output-format
directive) move structured-output reliability across a matrix of providers, directive variants,
and production-shaped prompts. Everything about that harness that is DETERMINISTIC — how a raw
reply is scored (before/after parse success, layout class), the per-cell credential gate, and
the call-budget cap — lives HERE, as a pure, importable helper with NO network dependency, so it
is proven offline on committed golden fixtures and is never validated for the first time by a
paid live run. This mirrors ``tests/external/_live_llm.py``: an underscore helper shared by the
external harness and the ``tests/unit`` oracle, and NOT itself collected by pytest.

The scoring reuses rebar's own JSON enumeration (``rebar.llm.structured``) rather than
re-implementing brace matching, so "what parses" here is byte-for-byte what the production parser
sees.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from rebar.llm.structured import (
    _FENCE_RE,
    _all_json_objects,
    parse_structured,
    tolerant_parse,
    validate_to,
)

#: The four reply-layout classes the scorer distinguishes (see :func:`classify_layout`).
LAYOUT_CLASSES: tuple[str, ...] = ("prose", "fenced", "multi_object", "quoted_json")

#: The two output-format directive variants a matrix cell is measured under. ``current`` is the
#: prompt as shipped; ``sentinel`` appends the output-format directive line the sentinel sibling
#: (a separate story) refines.
DIRECTIVE_VARIANTS: tuple[str, ...] = ("current", "sentinel")

#: Default per-run ceiling on the number of live model calls the harness may make. A hard cap on
#: spend, enforced BEFORE any network object is constructed (see :func:`enforce_call_budget`).
DEFAULT_CALL_BUDGET: int = 300

#: The env var each key-authenticated provider's credential lives in. Bedrock is ABSENT by
#: design: it carries no key of its own and authenticates from the ambient AWS chain (mirrors
#: ``tests/external/_live_llm.py``).
PROVIDER_KEY_ENV: dict[str, str] = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


class CallBudgetExceeded(RuntimeError):
    """Raised when a planned live-call count exceeds the configured budget cap."""


# ── layout classification ─────────────────────────────────────────────────────────────────


def _object_spans(text: str) -> list[tuple[int, int, object]]:
    """Every top-level, non-overlapping balanced ``{…}`` object that PARSES as JSON, as
    ``(start, end_inclusive, value)`` triples in discovery order.

    Same string-aware brace state machine as ``rebar.llm.structured._all_json_objects`` — which
    yields the VALUES this harness counts — but also carries each object's span so the classifier
    can inspect the delimiters immediately surrounding it (inline quoting), which the value-only
    enumeration cannot express."""
    spans: list[tuple[int, int, object]] = []
    n = len(text)
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        closed_at = -1
        for i in range(start, n):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    closed_at = i
                    break
        if closed_at >= 0:
            try:
                value = json.loads(text[start : closed_at + 1])
            except json.JSONDecodeError:
                start = text.find("{", start + 1)
                continue
            spans.append((start, closed_at, value))
            start = text.find("{", closed_at + 1)
            continue
        start = text.find("{", start + 1)
    return spans


def _distinct_values(values: list[object]) -> list[object]:
    """Dedupe parsed objects by equal Python value, preserving first-seen order (dicts are
    unhashable, so this is a linear equality pass, not a ``set``)."""
    distinct: list[object] = []
    for value in values:
        if value not in distinct:
            distinct.append(value)
    return distinct


def _has_fenced_json(text: str) -> bool:
    """True when at least one ``` … ``` code fence's contents parse as JSON."""
    for match in _FENCE_RE.finditer(text):
        try:
            json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        return True
    return False


def _neighbours(text: str, start: int, end: int) -> tuple[str, str]:
    """The first non-whitespace characters immediately before ``start`` and after ``end``
    (inclusive), or the empty string when the object abuts a text boundary."""
    left = start - 1
    while left >= 0 and text[left].isspace():
        left -= 1
    right = end + 1
    while right < len(text) and text[right].isspace():
        right += 1
    before = text[left] if left >= 0 else ""
    after = text[right] if right < len(text) else ""
    return before, after


def _is_inline_quoted(text: str, start: int, end: int) -> bool:
    """True when the object spanning ``[start, end]`` is wrapped in inline ``backticks`` or
    straight double-quotes within prose (matched delimiters on both sides)."""
    before, after = _neighbours(text, start, end)
    return (before, after) in {("`", "`"), ('"', '"')}


def classify_layout(text: str) -> str:
    """Classify ``text`` into exactly one of :data:`LAYOUT_CLASSES`.

    Precedence, evaluated in order:

    1. ``multi_object`` — two or more DISTINCT top-level parseable JSON objects (deduped by
       equal Python value, so the same object is never double-counted).
    2. ``fenced`` — exactly one parseable object AND it appears inside a ``` … ``` code fence.
    3. ``quoted_json`` — exactly one parseable object AND it is wrapped in inline ``backticks``
       or straight double-quotes within prose (NOT a fenced block).
    4. ``prose`` — otherwise (a single bare object mid-prose, or no parseable object at all)."""
    spans = _object_spans(text)
    distinct = _distinct_values(_all_json_objects(text))
    if len(distinct) >= 2:
        return "multi_object"
    if len(distinct) == 1:
        if _has_fenced_json(text):
            return "fenced"
        start, end, _ = spans[0]
        if _is_inline_quoted(text, start, end):
            return "quoted_json"
    return "prose"


# ── reply scoring ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReplyScore:
    """The measured outcome for a single reply."""

    layout: str
    current_ok: bool
    new_ok: bool


def _current_parses(text: str, model_cls) -> bool:
    """The PRE-df3a behavior: the first-object tolerant parse feeds validation directly."""
    try:
        validate_to(model_cls, tolerant_parse(text, schema=model_cls))
    except Exception:  # noqa: BLE001 — any parse/validation failure is a "no", by design
        return False
    return True


def _new_parses(text: str, model_cls) -> bool:
    """The df3a behavior: schema-filtered candidate selection then validate."""
    try:
        parse_structured(text, model_cls)
    except Exception:  # noqa: BLE001 — any parse/validation failure is a "no", by design
        return False
    return True


def score_reply(text: str, model_cls) -> ReplyScore:
    """Score one raw reply: its layout class and whether the OLD and NEW parsers each recover a
    valid ``model_cls`` instance from it."""
    return ReplyScore(
        layout=classify_layout(text),
        current_ok=_current_parses(text, model_cls),
        new_ok=_new_parses(text, model_cls),
    )


@dataclass(frozen=True)
class ScoreTable:
    """The before/after aggregate over a set of replies."""

    n: int
    current_ok: int
    new_ok: int
    layout_counts: dict[str, int]


def score_replies(replies: list[str], model_cls) -> ScoreTable:
    """Aggregate :func:`score_reply` over ``replies`` into a :class:`ScoreTable`.

    ``layout_counts`` always carries EVERY :data:`LAYOUT_CLASSES` key, zero-filled."""
    layout_counts = {cls: 0 for cls in LAYOUT_CLASSES}
    current_ok = 0
    new_ok = 0
    for text in replies:
        score = score_reply(text, model_cls)
        layout_counts[score.layout] += 1
        current_ok += int(score.current_ok)
        new_ok += int(score.new_ok)
    return ScoreTable(
        n=len(replies),
        current_ok=current_ok,
        new_ok=new_ok,
        layout_counts=layout_counts,
    )


# ── call budget ───────────────────────────────────────────────────────────────────────────


def planned_call_count(n_cells: int, n_variants: int, n_prompts: int, n_repeats: int) -> int:
    """The number of live calls a full matrix sweep would make: the simple product of its
    dimensions (cells × directive-variants × prompts × repeats)."""
    return n_cells * n_variants * n_prompts * n_repeats


def enforce_call_budget(planned: int, cap: int = DEFAULT_CALL_BUDGET) -> None:
    """Raise :class:`CallBudgetExceeded` iff ``planned`` exceeds ``cap``.

    Callable BEFORE any network object is constructed — it is a pure integer comparison, so the
    harness can refuse to start an over-budget sweep without paying for a single call."""
    if planned > cap:
        raise CallBudgetExceeded(
            f"planned {planned} live calls exceeds the budget cap of {cap} "
            f"(raise REBAR_STRUCTURED_MATRIX_MAX_CALLS or shrink the matrix)"
        )


# ── matrix cells ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Cell:
    """One provider arm of the measurement matrix."""

    provider: str
    config_file: str


def load_cells(providers_dir) -> list[Cell]:
    """One :class:`Cell` per ``*.toml`` provider overlay in ``providers_dir`` (the ``README.md``
    is naturally ignored). The provider name is the toml filename stem. Accepts a ``str`` or
    ``Path``; cells are returned sorted by provider for deterministic ordering."""
    directory = Path(providers_dir)
    cells = [
        Cell(provider=path.stem, config_file=str(path)) for path in sorted(directory.glob("*.toml"))
    ]
    return cells


# ── per-cell credential gate ──────────────────────────────────────────────────────────────


def _aws_credentials_resolvable() -> bool:
    """True when boto3's own chain finds credentials (instance role / env / profile / OIDC).

    Mirrors ``tests/external/_live_llm.py._aws_credentials_resolvable``: credentials ONLY, never
    a region check, and ``False`` when boto3 is absent."""
    try:
        import boto3
    except ImportError:
        return False
    try:
        return boto3.session.Session().get_credentials() is not None
    except Exception:  # noqa: BLE001 — a broken/partial AWS config reads as "no credential"
        return False


def credential_status(provider: str, env=None, *, aws_probe=None) -> str:
    """``"measured"`` when ``provider``'s OWN credential is available, else ``"unmeasured"``.

    ``env`` defaults to :data:`os.environ`; ``aws_probe`` defaults to
    :func:`_aws_credentials_resolvable`. anthropic/openai are measured iff their key env var
    holds a non-empty string — a FOREIGN provider's key never counts. bedrock has no key of its
    own, so it is measured iff ``aws_probe()`` is truthy (the ambient AWS chain)."""
    if env is None:
        env = os.environ
    if provider == "bedrock":
        probe = aws_probe if aws_probe is not None else _aws_credentials_resolvable
        return "measured" if probe() else "unmeasured"
    env_name = PROVIDER_KEY_ENV.get(provider)
    value = env.get(env_name) if env_name else None
    return "measured" if isinstance(value, str) and value else "unmeasured"
