"""``rebar_reconciler.adapters.jira_datacenter`` — the Data Center backend (story J6,
epic e369).

Built on ``pycontribs/jira`` (the maintained Python Jira client), confined to the
opt-in ``[jira-datacenter]`` extra. Every module here imports ``jira`` LAZILY —
inside the function that needs it, never at module top — so ``import rebar``
(and this package's own import) stays dependency-free, matching the engine's
``dependencies = []`` contract. See ``transport.py`` for the transport,
``settings.py`` for the typed-config resolution, and ``backend.py`` for the
registered :class:`~rebar_reconciler._backend.Backend` factory.

Does NOT import anything from ``adapters/jira/`` (Cloud's ACLI-based transport,
live-validated and must not be disturbed) — only the Jira-family SHARED layer
(``adapters/jira_family``).

**Organising principle for the transport (ticket 465d): one module per
capability, aligned to the Protocols in ``_backend.py``** —
``_base.py`` (shared substrate), ``_issues.py`` (``TicketTransport``),
``_links.py`` (``SupportsLinks`` + ``SupportsComments``, co-located: each
alone is too small to clear the module-size policy's floor), ``_hierarchy.py``
(parent/Epic Link), ``_people.py`` (identity + properties), and
``transport.py`` itself as the thin composition root. A new transport
operation's home is decided by *which capability it is*, not by which file
currently has headroom — see ``transport.py``'s module docstring for the
detailed map.
"""

from __future__ import annotations
