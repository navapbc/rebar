"""Eval harness for the ``rebar.llm`` prompt/criterion library.

Grouping-only subpackage (item toll-clock-tier): ``eval``, ``eval_scorers``, and
``eval_solver`` moved here from the flat ``rebar.llm`` root. The packaged eval-spec
YAMLs stay at ``rebar.llm.eval_specs`` (a data dir, not moved). Import the modules
directly (``from rebar.llm.evals import eval``); no names are re-exported at the
package level, to preserve the lazy-import pattern (``import rebar.llm`` stays light).
"""

from __future__ import annotations
