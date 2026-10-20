"""The Jira reconciler, run as ``python -m rebar.reconciler``.

Its modules log under ``rebar.reconciler.*``, inside the ``rebar`` hierarchy, so the
``rebar`` root's NullHandler keeps importing the package quiet; the subprocess entry
point (``__main__.py``) installs the stderr handler. See ``rebar._logging``.
"""
