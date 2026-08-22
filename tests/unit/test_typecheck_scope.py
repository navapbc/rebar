"""CI guard: `make typecheck` must mypy `scripts/` too, not just `src/rebar`.

Ticket cc99 (follow-up to ae96): ae96 brought `scripts/` under `ruff check` / `ruff
format` — ``sources = src tests scripts`` in the Makefile — but the ``typecheck`` target
stayed ``mypy src/rebar``. Several files under ``scripts/`` ARE the CI gates behind the
Gerrit ``Verified`` vote (``.github/workflows/_build-and-test.yml`` invokes them
directly), so the gate implementations themselves were type-unchecked. Widening the
target surfaced 15 real errors across 6 files.

This is the guard for that scope. It fails if the ``typecheck`` target is ever
re-narrowed to ``src/rebar`` only, so the gap cannot silently reopen. It is the mypy
sibling of ``test_ci_actionlint_scope.py``, which guards actionlint's scope in ``lint``
the same way.
"""

from __future__ import annotations

import shlex
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _ROOT / "Makefile"


def _recipe(target: str, text: str) -> str:
    """Return the ``<target>:`` header plus its tab-indented recipe body."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{target}:"))
    body = [lines[start]]
    for ln in lines[start + 1 :]:
        if not ln.startswith("\t"):  # first non-recipe line ends the target
            break
        body.append(ln)
    return "\n".join(body)


def _mypy_args(recipe: str) -> list[str]:
    """The arguments passed to mypy by a recipe, across all its mypy invocations."""
    args: list[str] = []
    for line in recipe.splitlines():
        tokens = shlex.split(line.strip())
        if "mypy" in tokens:
            args.extend(tokens[tokens.index("mypy") + 1 :])
    return args


def _assert_scope_covers_scripts(recipe: str) -> None:
    """Fail unless the recipe hands mypy both `src/rebar` and `scripts`.

    Factored out so the negative test below can exercise the same assertion against a
    synthetic re-narrowed recipe — otherwise this guard could rot into a test that
    passes no matter what the Makefile says.
    """
    args = _mypy_args(recipe)
    assert args, "make typecheck no longer invokes mypy at all"
    missing = [
        path
        for path in ("src/rebar", "scripts")
        if not any(arg == path or arg.startswith(f"{path}/") for arg in args)
    ]
    assert not missing, (
        f"make typecheck does not type-check {missing} — mypy is called with {args!r}. "
        "scripts/ must stay in the typecheck scope (ticket cc99): several scripts ARE "
        "the CI gates behind the Verified vote."
    )


def test_typecheck_covers_src_and_scripts() -> None:
    """The real Makefile must type-check `src/rebar` AND `scripts`."""
    _assert_scope_covers_scripts(_recipe("typecheck", _MAKEFILE.read_text()))


def test_guard_rejects_a_renarrowed_recipe() -> None:
    """The guard must FAIL on a `src/rebar`-only recipe (proves it can detect a revert)."""
    renarrowed = _recipe("typecheck", "typecheck:  ## help\n\tmypy src/rebar\n\nother:\n")
    try:
        _assert_scope_covers_scripts(renarrowed)
    except AssertionError as exc:
        refusal: BaseException = exc
    else:  # pragma: no cover - only reached if the guard has rotted
        raise AssertionError(
            "the scope guard accepted a `mypy src/rebar`-only recipe — it would not "
            "catch a revert of ticket cc99"
        )
    assert "scripts" in str(refusal)
