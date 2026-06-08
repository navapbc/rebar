"""
ticket-llm-format.py
Backward-compatibility shim.

All logic has moved to ticket_reducer/llm_format.py.
This file re-exports the public API for callers that still import from here.
Do NOT delete until a grep audit confirms no additional callers remain.
"""

from ticket_reducer.llm_format import (  # noqa: F401
    KEY_MAP,
    OMIT_KEYS,
    ALWAYS_EMIT,
    COMMENT_KEY_MAP,
    COMMENT_OMIT,
    DEP_KEY_MAP,
    DEP_OMIT,
    shorten_comment,
    shorten_dep,
    to_llm,
)
