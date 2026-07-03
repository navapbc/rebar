"""Prompt authoring + rendering (the ``rebar.llm`` prompt library).

Grouping-only subpackage (item toll-clock-tier): ``prompts``, ``prompt_library``,
and ``prompts_frontmatter`` moved here from the flat ``rebar.llm`` root. Import the
modules directly (``from rebar.llm.prompting import prompts``); no names are
re-exported at the package level, to preserve the deliberate lazy-import pattern
(``import rebar.llm`` stays light — the heavy prompt modules load on first use).
"""

from __future__ import annotations
