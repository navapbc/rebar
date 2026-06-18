"""rebar import/export adapters (P1.2).

A lossy interop *projection* of the ticket store — NOT a backup (``git bundle
create tickets.bundle tickets`` already gives a lossless, full-audit-trail
backup). Two consumers:

* **Reporting / data-mining** — a flat, tool-friendly NDJSON export other systems
  parse (DuckDB / jq / pandas / BigQuery).
* **Clean rebar→rebar migration** — export a store, import into a different repo,
  optionally stripping external-tracker (Jira) associations so tickets re-map.

The export side lives in :mod:`export_ndjson` (streaming, one full ticket object
per line); ``--strip-external`` provider-neutral stripping in :mod:`_strip`. The
import side (:mod:`import_ndjson`) lands in a later sub-task.
"""

from __future__ import annotations

from .export_ndjson import EXPORT_SCHEMA_VERSION, export_tickets, iter_export_states

__all__ = ["export_tickets", "iter_export_states", "EXPORT_SCHEMA_VERSION"]
