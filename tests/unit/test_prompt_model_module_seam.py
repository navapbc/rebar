"""Module-size seam for the prompt library (story e881-06e4-ffb2-4c4b).

``src/rebar/llm/prompting/prompts.py`` sat at 789 lines against the 800-line hard cap
— eleven lines of headroom, so even adding a comment failed the CI module-size gate.
The file is split along the seam its own call graph already had: ``prompts`` keeps the
RESOLVER (which bytes are a prompt's bytes, the derived reviewer catalog over them, the
front-matter contract, variant overlays, reviewer selection — all filesystem-bound), and
the I/O-free half moves to the sibling ``prompt_model`` leaf: what a prompt IS.

The seam is real, not a line-count carve:

* the extracted set is a CLOSED sub-graph. ``Prompt``/``Reviewer`` are constructed by
  the resolver and construct nothing themselves; ``ReviewerError``/``PromptNotFound``
  are raised by it; ``EXECUTION_MODES`` is read by it; ``template_variables`` calls only
  ``_VAR``, ``_render_strict`` only ``template_variables`` + ``_VAR``,
  ``shared_plan_prefix`` only ``SHARED_STANCE_PREAMBLE``, ``split_volatile`` and
  ``strip_volatile_marker`` only ``VOLATILE_MARKER``, and ``prompt_content_hash`` only
  ``hashlib``. **No member calls anything in ``prompts``**, so every edge across the
  seam points one way, from the resolver down into the model;
* they share one property — none of them touches the filesystem or the catalog, against
  a resolver half that is *defined* by reading ``reviewers/*.md`` and
  ``.rebar/prompts/*.md``;
* nothing in the extracted set is a monkeypatch target. The resolver half is welded in
  place by two patches that must keep reaching their callers through the ``prompts``
  module namespace (``prompts._catalog_dir`` in ``test_prompt_index`` /
  ``test_prompt_authoring``, ``prompts._prompt_file`` in ``test_prompt_variants``), so
  moving THAT half would have silently broken late binding. Moving the model leaf
  instead changes no binding at all: the resolver still resolves every one of these
  names out of the ``prompts`` namespace, because that is where the re-export puts them.

What this file pins:

1. The model + text grammar are DEFINED in ``prompt_model``, not in ``prompts``.
2. ``prompts`` still exposes every moved name, bound to the SAME object — the
   back-compat re-export that keeps ``prompts.<symbol>`` attribute access and
   ``from rebar.llm.prompting.prompts import <symbol>`` call-sites working unchanged,
   exactly as the earlier front-matter split did (docs/architecture.md).
3. ``prompt_model`` imports nothing from ``prompts``: the leaf stays a leaf, so there is
   no import cycle and no name that used to late-bind through ``prompts`` stops doing so.
4. Both modules stay inside the size band the module-size policy sets: at least 100
   lines (no sliver files) and at least 100 lines of REAL headroom under the cap read
   from ``.github/module-size-limit.txt`` — landing back at the ceiling is the trap
   this story exists to clear.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from module_size_support import read_limit

pytestmark = pytest.mark.unit

_PROMPTING = Path(__file__).resolve().parents[2] / "src" / "rebar" / "llm" / "prompting"

#: The closed set extracted across the seam: the value types, the error vocabulary, the
#: closed execution-mode enum, the ``{{var}}`` template engine, the two authoring markers
#: and their readers, the single-sourced reviewing-stance preamble, and content identity.
_MOVED_TYPES = ("Prompt", "Reviewer", "ReviewerError", "PromptNotFound")
_MOVED_FUNCTIONS = (
    "template_variables",
    "_render_strict",
    "shared_plan_prefix",
    "split_volatile",
    "strip_volatile_marker",
    "prompt_content_hash",
)
_MOVED_CONSTANTS = (
    "EXECUTION_MODES",
    "_VAR",
    "_BASE_MARKER",
    "VOLATILE_MARKER",
    "SHARED_STANCE_PREAMBLE",
)

_MIN_LOC = 100
_MIN_HEADROOM = 100


@pytest.mark.parametrize("name", _MOVED_FUNCTIONS + _MOVED_TYPES)
def test_model_symbols_are_defined_in_the_extracted_module(name: str) -> None:
    """Each moved symbol's DEFINING module is the prompt-model leaf."""
    from rebar.llm.prompting import prompt_model

    obj = getattr(prompt_model, name)
    assert obj.__module__.endswith("prompt_model"), (
        f"{name} must be DEFINED in prompt_model, not re-exported into it; "
        f"found __module__={obj.__module__!r}"
    )


@pytest.mark.parametrize("name", _MOVED_FUNCTIONS + _MOVED_TYPES + _MOVED_CONSTANTS)
def test_prompts_still_exposes_every_moved_name(name: str) -> None:
    """Back-compat: ``prompts.<symbol>`` resolves to the very same object.

    Call-sites across ``src`` and ``tests`` reach these names both as
    ``prompts.<symbol>`` attributes and via ``from ...prompts import <symbol>``; the
    re-export is what keeps every one of them unmodified. Object identity matters for
    the two dataclasses in particular — a second class object would break ``isinstance``
    for anything that imported the other one.
    """
    from rebar.llm.prompting import prompt_model, prompts

    assert hasattr(prompts, name), f"prompts must keep re-exporting {name} after the split"
    assert getattr(prompts, name) is getattr(prompt_model, name)


def test_the_extracted_leaf_does_not_import_back_into_prompts() -> None:
    """``prompt_model`` is a LEAF — it imports nothing from ``prompts``.

    A back-edge would both create an import cycle and reintroduce the late-binding
    hazard the seam was chosen to avoid: a name the moved code reached through the
    ``prompts`` namespace would stop seeing patches applied to ``prompts``.
    """
    tree = ast.parse((_PROMPTING / "prompt_model.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    offenders = [m for m in imported if m.split(".")[-1] == "prompts"]
    assert not offenders, (
        f"prompt_model must not import from prompts (found {offenders}) — "
        "the extracted cluster is a leaf"
    )


@pytest.mark.parametrize("filename", ("prompts.py", "prompt_model.py"))
def test_both_sides_of_the_seam_sit_inside_the_size_band(filename: str) -> None:
    """No sliver file, and real headroom under the cap — measured the way CI measures."""
    cap = read_limit()
    loc = (_PROMPTING / filename).read_text(encoding="utf-8").count("\n")
    assert loc >= _MIN_LOC, (
        f"{filename} is {loc} lines — a split must not produce a sliver module (minimum {_MIN_LOC})"
    )
    assert loc <= cap - _MIN_HEADROOM, (
        f"{filename} is {loc} lines against a {cap}-line cap — the split must leave at "
        f"least {_MIN_HEADROOM} lines of headroom, not land back at the ceiling"
    )
