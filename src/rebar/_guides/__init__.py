"""Packaged author-facing prose guides read at runtime by ``rebar explain``.

These markdown files are the CANONICAL source (moved out of ``docs/`` so an installed rebar
can serve them from any working directory). ``rebar.llm.plan_review.registry.explain_guide``
reads them via :func:`importlib.resources.files` — the same in-package data pattern rebar uses
for ``criteria_routing.json``. The ``docs/`` copies are thin pointers back here.
"""
