"""Engine-support modules used in-process by the library/MCP read path.

These were previously generic top-level engine modules (``ticket_resolver``,
``ticket_output``, ``ticket_reads``) imported by inserting the engine dir onto
``sys.path``. They now live here as real ``rebar._engine_support.*`` submodules so
the library never pollutes ``sys.path`` with generic names (ticket
``fare-rant-clasp``). The engine's bash dispatcher still reaches them under the
old top-level names via thin compat shims in ``rebar/_engine/`` (kept only until
the bash→Python strangler-fig ports retire those callers).
"""
