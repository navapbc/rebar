"""The rebar audit read layer (story 46f0).

A single documented read surface (:func:`rebar.audit.read.audit_trail`) that, for a ticket
id, returns its FULL retained review history plus the associated code reviews. Pure
addition — it composes existing best-effort sidecar readers and never mutates the store.
"""
