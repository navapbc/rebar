# rebar_reconciler.adapters — vendor-adapter seam (ticket 44be / ambery-tweed-grosbeak)
#
# Backend-specific ("vendor") reconciler modules live under this sub-package, one
# directory per backend (``adapters/jira/`` today; ``adapters/<x>/`` for a future
# second backend). The reconciler's backend-neutral core — the differ / apply /
# dispatch / store machinery at the package root — targets the operations these
# vendor modules provide (issue CRUD, field mapping, the transport). See
# ``docs/adr/0035-reconciler-vendor-adapter-seam.md`` for the seam design and the
# phased-migration plan (Phase 1 relocates only the loader-safe, low-reference
# vendor modules; the rest are inventoried there for Phase 2).

# Importing each adapter's backend module registers its factory in the backend
# registry (rebar_reconciler._backend_registry) as an import side-effect, so
# select_backend() finds it after a lazy `import rebar_reconciler.adapters`.
from .jira import backend as _jira_backend  # noqa: F401,E402
