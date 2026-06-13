"""The unified write/sync core (Tier D, ``REBAR_WRITE_CORE``).

ONE lock (``lock``), the canonical locked committer (``event_append``), the
best-effort push (``push``), and cross-clone reconvergence (``sync``) — the
in-process replacement for the bash write path (``_flock_stage_commit`` /
``write_commit_event`` / ``_push_tickets_branch`` / ``_reconverge_tickets``).
"""
