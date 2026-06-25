# rebar_reconciler package
#
# This package is imported top-level as ``rebar_reconciler`` (run via
# ``python -m rebar_reconciler`` with ``_engine`` on sys.path), so its modules'
# loggers — ``logging.getLogger(__name__)`` — resolve under the **sibling** root
# ``rebar_reconciler.*``, NOT under ``rebar``. A NullHandler on the ``rebar`` root
# therefore does not cover them. Attach one here so importing the reconciler stays
# quiet by default; the subprocess ``main`` (``__main__.py``) installs the stderr
# handler. See ``rebar._logging`` for the convention.
import logging

logging.getLogger("rebar_reconciler").addHandler(logging.NullHandler())
