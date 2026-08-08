"""Engine-support modules used in-process by the library/MCP read path.

These live here as real ``rebar._engine_support.*`` submodules (resolver, output,
reads, and their siblings) so the library never pollutes ``sys.path`` with generic
top-level names (ticket ``fare-rant-clasp``). History — the pre-collapse bash read
path and the retired ``_engine/`` compat shims: ``docs/bash-migration.md`` §5/§7.
"""
