"""Access point for the reducer from the graph package.

Historically this loaded ``ticket-reducer.py`` via ``spec_from_file_location``
to cope with the hyphenated engine filename. Now that the reducer is a real
subpackage (``rebar.reducer``) it imports directly. ``reducer`` stays a module
object exposing ``reduce_ticket`` / ``reduce_all_tickets`` so existing patch
points (``_loader_module.reducer.reduce_all_tickets``) are unchanged.
"""

from __future__ import annotations

import rebar.reducer as reducer

reduce_ticket = reducer.reduce_ticket
reduce_all_tickets = reducer.reduce_all_tickets
