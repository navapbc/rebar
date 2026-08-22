"""One shared constructor for the single-turn structured LLM sub-call (story
``anthropoid-trophied-moose``).

Fourteen sites in ``src/rebar/llm`` used to hand-build the same request shape —
``mode="structured"`` + ``output_schema`` + ``execution_mode="single_turn"`` — and the four
lowering-only ceilings (:class:`SingleTurnBounds`) were declared at exactly one of them. An
omitted ceiling looks identical to a deliberately-inherited one, which is how bug
``leathery-druidic-nurseshark`` bounded ``judge_batch`` and left the identical ``judge_one``
block four lines above it unbounded.

:func:`RunRequest.for_structured` is now the only way to build that shape outside the owner
module, and it takes ``bounds`` as a REQUIRED argument, so the decision is always written down.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.runner import RunRequest
from rebar.llm.structured_run import SingleTurnBounds

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "rebar"
_OWNER = _SRC / "llm" / "runner.py"

# A legitimate second site declares itself with a REASON. A bare marker is not an excuse: the
# reason is what makes the exception reviewable rather than silently absent.
_MARKER = re.compile(r"#\s*single-turn-structured-ok:\s*(\S.*)$")


def _string(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _reason(lines: list[str], node: ast.Call) -> str | None:
    """The marker reason on any line of ``node``'s source range, or ``None``."""
    for raw in lines[node.lineno - 1 : (node.end_lineno or node.lineno)]:
        found = _MARKER.search(raw)
        if found and found.group(1).strip():
            return found.group(1).strip()
    return None


def _unmarked_hand_built_sites(root: pathlib.Path, owner: pathlib.Path) -> list[str]:
    """Every direct ``RunRequest(...)`` under ``root`` (minus ``owner``) that carries BOTH
    ``execution_mode="single_turn"`` and ``mode="structured"`` without a reasoned marker.

    The construct is that CONJUNCTION, never one of its halves: ``mode="structured"`` alone is
    the ordinary agentic review shape, and ``execution_mode="single_turn"`` alone also covers
    the ``mode="text"`` summarizer in ``plan_review/passes.py``.
    """
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path == owner:
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - src/rebar always parses
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "RunRequest":
                continue
            kwargs = {k.arg: k.value for k in node.keywords}
            if _string(kwargs.get("execution_mode")) != "single_turn":
                continue
            if _string(kwargs.get("mode")) != "structured":
                continue
            if _reason(lines, node) is not None:
                continue
            offenders.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return offenders


def _cfg() -> LLMConfig:
    return LLMConfig()


@pytest.mark.repo_policy
def test_the_single_turn_structured_request_is_built_only_by_for_structured() -> None:
    """The uniqueness guard: one constructor for the shape, everywhere else goes through it."""
    offenders = _unmarked_hand_built_sites(_SRC, _OWNER)
    assert offenders == [], (
        "build these through RunRequest.for_structured(..., bounds=...) so the bounding "
        "decision is explicit, or mark the line "
        f"'# single-turn-structured-ok: <reason>': {offenders}"
    )


def test_the_guard_catches_a_hand_built_site_and_a_reasoned_marker_excuses_it(
    tmp_path: pathlib.Path,
) -> None:
    """The guard is not a tautology, and the marker's REASON is what excuses a site."""
    body = (
        "RunRequest(\n"
        "    system_prompt='s',{marker}\n"
        "    mode='structured',\n"
        "    execution_mode='single_turn',\n"
        ")\n"
    )
    (tmp_path / "bare.py").write_text(body.format(marker=""), encoding="utf-8")
    (tmp_path / "reasoned.py").write_text(
        body.format(marker="  # single-turn-structured-ok: vendored"), encoding="utf-8"
    )
    (tmp_path / "unreasoned.py").write_text(
        body.format(marker="  # single-turn-structured-ok:"), encoding="utf-8"
    )
    (tmp_path / "agentic.py").write_text(
        "RunRequest(mode='structured', execution_mode='agentic')\n", encoding="utf-8"
    )
    (tmp_path / "owned.py").write_text(body.format(marker=""), encoding="utf-8")

    offenders = _unmarked_hand_built_sites(tmp_path, tmp_path / "owned.py")

    assert offenders == ["bare.py:1", "unreasoned.py:1"]


def test_for_structured_pins_the_single_turn_structured_pair() -> None:
    """``mode`` and ``execution_mode`` are no longer a caller's job to keep consistent."""
    req = RunRequest.for_structured(
        system_prompt="s",
        instructions="i",
        config=_cfg(),
        reviewers=["probe"],
        output_schema="overlap_verdict",
        bounds=RunRequest.INHERIT_POLICY,
    )

    assert (req.mode, req.execution_mode, req.output_schema) == (
        "structured",
        "single_turn",
        "overlap_verdict",
    )
    assert (req.system_prompt, req.instructions, req.reviewers) == ("s", "i", ["probe"])
    assert req.target == {}


def test_each_bound_reaches_its_own_request_field() -> None:
    """A bound must land on the field that enforces it — the four are not interchangeable."""
    req = RunRequest.for_structured(
        system_prompt="s",
        instructions="i",
        config=_cfg(),
        reviewers=["probe"],
        output_schema="overlap_verdict",
        bounds=SingleTurnBounds(
            output_tokens=1024, timeout_s=60, structured_retries=0, transport_attempts=1
        ),
        target={"kind": "ticket"},
    )

    assert req.output_token_limit == 1024
    assert req.request_timeout_limit_s == 60
    assert req.structured_retry_limit == 0
    assert req.transport_attempt_limit == 1
    assert req.target == {"kind": "ticket"}


def test_inherit_policy_leaves_every_ceiling_unset() -> None:
    """The named "inherit run-wide policy" value is exactly what an omitted seam did before."""
    req = RunRequest.for_structured(
        system_prompt="s",
        instructions="i",
        config=_cfg(),
        reviewers=["probe"],
        output_schema="overlap_verdict",
        bounds=RunRequest.INHERIT_POLICY,
    )

    assert req.output_token_limit is None
    assert req.request_timeout_limit_s is None
    assert req.structured_retry_limit is None
    assert req.transport_attempt_limit is None


def test_the_bounding_decision_cannot_be_inherited_by_omission() -> None:
    """No default on ``bounds``, and none on any :class:`SingleTurnBounds` field."""
    with pytest.raises(TypeError):
        RunRequest.for_structured(  # type: ignore[call-arg]
            system_prompt="s",
            instructions="i",
            config=_cfg(),
            reviewers=["probe"],
            output_schema="overlap_verdict",
        )
    with pytest.raises(TypeError):
        SingleTurnBounds(output_tokens=None, timeout_s=None)  # type: ignore[call-arg]
