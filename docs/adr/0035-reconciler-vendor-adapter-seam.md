# ADR 0035: Reconciler vendor-adapter seam (Jira-neutral core + `adapters/<backend>/`)

- **Status:** Accepted (Phase 1 landed; Phase 2 tracked as a follow-up ticket)
- **Context:** Story *Reconciler vendor-adapter seam: ADR + sub-packaging for
  multi-backend* (`44be-2ae1-ba73-46da`, alias `ambery-tweed-grosbeak`; O5 + S5).
  A second reconciler backend (a non-Jira ticket system) is planned. Today the
  Jira/Atlassian assumption is threaded across ~24 of the ~58 flat modules in
  `src/rebar/_engine/rebar_reconciler/`, and there is no sub-package boundary that
  a backend could be swapped at. This ADR defines that boundary — the sub-package
  layout **is** the vendor-adapter seam — and records why the migration must be
  **phased** rather than a single 55-module move.

## Context

The reconciler is a flat namespace of ~58 sibling modules. Two facts constrain any
restructuring and are the reason this is design-first + phased:

1. **A file-location dynamic loader threads sibling `.py` files by name.**
   `_loader.lazy_load(key, filename)` and `__main__._load_sibling_keyed(dotted, filename)`
   (plus direct `importlib.util.spec_from_file_location(...)` sites in `differ.py`,
   `inbound_differ.py`, `outbound_fields.py`, `outbound_comments.py`, `applier.py`,
   `invariants.py`, `fetcher.py`, `pass_io.py`, `binding_store.py`,
   `inbound_translate.py`, `reconcile.py`, `run_differs.py`, `apply_base.py`,
   `reconcile_check.py`, `rebar_id_audit.py`, `_ref_lock.py`, `_advisory_lock.py`)
   load modules by **filename relative to `_PACKAGE_DIR = Path(__file__).parent`**.
   Every filename these sites name is **location-pinned**: physically moving such a
   file breaks the loader unless the loader is taught the new sub-package directory
   in the same change. The full location-pinned set (grep
   `lazy_load(|_load_sibling_keyed(|spec_from_file_location(`) is: `mode.py`,
   `_advisory_lock.py`, `_ref_lock.py`, `mutation.py`, `_errors.py`, `_concurrency.py`,
   `_loader.py`, `config.py`, `manifest_renderer.py`, `comment_limits.py`, `adf.py`,
   `alert_store.py`, `outbound_fields.py`, `differ.py`, `inbound_differ.py`,
   `outbound_differ.py`, `applier.py`, `invariants.py`, `invariant_sink.py`,
   `fetcher.py`, `run_differs.py`, `classify.py`, `binding_store.py`, `binding_walk.py`,
   `conflict_resolver.py`, `health.py`, `baseline_shadow.py`, `inbound_probe.py`,
   `local_label_intent.py`, `sync_logger.py`. Of these, `adf.py`, `outbound_fields.py`,
   and `comment_limits.py` are **Jira-coupled *and* location-pinned** — they cannot
   move until Phase 2 updates the loader.

2. **Tests patch reconciler internals module-qualified.** Many tests do
   `mock.patch("rebar_reconciler.<mod>.<attr>", ...)` or
   `patch.object(acli_mod.acli_subprocess, "_run_acli", ...)`. A patch string binds to
   the module at its canonical path; a package-`__init__` re-export shim does **not**
   fix this (the patch would rebind the shim, not the site the code actually calls).
   So any physically-moved module must have **exactly one binding site**, with **every**
   importer and **every** test patch string updated in the same change — and no
   re-export shim left at the old path. Modules with a broad patch/import surface are
   therefore deferred until their full surface can be migrated atomically.

### (a) Jira-coupling inventory

| Module | Jira/Atlassian coupling | Location-pinned? | Module-import / patch surface |
|--------|-------------------------|------------------|-------------------------------|
| `acli.py` | ACLI client core (re-export facade over the `acli_*` cluster) | no | ~29 test files |
| `acli_cli_ops.py` | Module-level ACLI CLI operations | no | via `acli` facade + a few |
| `acli_graph.py` | ACLI issue-link / graph mixin | no | via `acli` facade |
| `acli_rest.py` | ACLI REST fallbacks | no | small |
| `acli_subprocess.py` | ACLI subprocess transport (`_run_acli`, timeouts, `resolve_jira_settings`) | no | ~5 test files + `patch.object` sites |
| `adf.py` | Atlassian Document Format encode/limit-fit | **yes** (`lazy_load`) | ~19 refs |
| `jira_fields.py` | Jira field sanitizers + local↔Jira priority/status value maps | no | **3 internal importers, 0 test patches** |
| `outbound_fields.py` | Local→Jira outbound field mapping | **yes** (`spec_from_file_location`) | ~4 refs |
| `comment_limits.py` | Jira comment size limits | **yes** (`lazy_load`) | shared neutral-ish helper |

Differ / apply sites that reference the above (the Jira assumption leaking into the
core): `outbound_differ.py`, `inbound_differ.py`, `differ.py`, `classify.py`,
`reconcile_check.py`, `baseline_shadow.py`, `apply_inbound_records.py`,
`outbound_links.py`, `binding_walk.py`, `reconcile.py`. These call vendor field-mapping
/ sanitization / transport helpers directly and are the primary Phase-2 rewiring work
(they should depend on the backend *interface*, not on `adapters/jira/` concretely).

### (b) Vendor-neutral operation list (what a backend interface must provide)

A backend adapter exists to answer, for one external ticket system:

1. **Issue CRUD + transport** — create / read / update / transition / comment an
   external issue, and the transport that carries those calls (today: the ACLI
   subprocess + REST, `acli*`).
2. **Outbound field mapping** — map a local ticket's fields (summary, description,
   priority, status, labels, links, comments) to the backend's field/value shapes,
   including value maps (today: `outbound_fields.py` + `jira_fields.py`'s
   priority/status maps) and rich-text encoding (today: `adf.py`).
3. **Inbound field extraction** — the inverse: read the backend's issue payload back
   into local field shapes (today split across `inbound_*` + `adf.py` extraction).
4. **Field sanitization + limits** — defend against backend-specific hard limits
   (label length, comment size) and malformed input (today: `jira_fields.py`,
   `comment_limits.py`).
5. **Identity / label convention** — how the backend stores the `rebar-id` back-pointer
   (today: Jira labels; audited via `rebar_id_audit.py`).

The backend-neutral **core** — the differ / apply / dispatch / store / binding /
invariant machinery — orchestrates these operations and must not itself name Jira.

### (c) Target sub-package layout

```
rebar_reconciler/
  # ── backend-neutral core (stays at package root) ──
  reconcile.py  dispatch_one.py  batch_dispatch.py  typed_dispatch.py   # dispatch
  differ.py  inbound_differ.py  outbound_differ.py  run_differs.py      # differ
  applier.py  apply_*.py                                                # apply
  binding_store.py  binding_walk.py  alert_store.py  baseline_shadow.py # store
  invariants.py  invariant_sink.py  conflict_resolver.py  classify.py   # invariants
  fetcher.py  health.py  config.py  mode.py  mutation.py  _errors.py …   # loader-pinned neutral machinery
  adapters/
    __init__.py
    jira/
      __init__.py
      jira_fields.py          # ← relocated in Phase 1
      # Phase 2: acli*.py, adf.py, outbound_fields.py, comment_limits.py
    <backend-x>/              # future second backend
      __init__.py
      …
```

The `adapters/<backend>/` directory **is** the seam: everything under it is one
backend's concrete implementation of the operations in (b); everything at the root is
backend-neutral.

### (d) Adding a second backend (interface sketch)

To add backend **X**, create `adapters/<x>/` providing the operations in (b):

- a **transport/CRUD** module (create/read/update/transition/comment against X);
- **outbound** field mapping (local fields → X's field shapes + value maps + rich text);
- **inbound** field extraction (X's payload → local fields);
- **sanitization/limits** for X's constraints; and
- the **identity** convention (how X stores the `rebar-id` back-pointer).

The neutral core selects the adapter by config and drives it through those operations;
no core module should `import rebar_reconciler.adapters.jira.*` directly once Phase 2
routes the differ/apply sites through the interface.

## Decision

1. **The sub-package boundary is the vendor seam.** Backend-specific modules live under
   `adapters/<backend>/`; the differ/apply/dispatch/store/invariant core stays at the
   package root and is backend-neutral.
2. **The migration is PHASED, forced by the two constraints above.** Phase 1 (this
   change) moves **only** the loader-safe, low-reference vendor subset; broad-surface
   and location-pinned modules are deferred to Phase 2, inventoried here.
3. **Phase 1 moves `jira_fields.py` → `adapters/jira/jira_fields.py`** — the single
   cleanest candidate: not location-pinned (never dynamically loaded), 3 internal
   importers (`acli.py`, `acli_cli_ops.py`, `acli_graph.py`), and **zero** test patch
   strings. Its three importers are updated to
   `from rebar_reconciler.adapters.jira.jira_fields import …`; there is exactly one
   binding site and **no** re-export shim at the old path.
4. **No re-export shims.** Because tests patch module-qualified, a shim at the old path
   would create a patch-binding bug. A moved module has exactly one canonical path.
5. **Phase 2 (follow-up ticket, linked `discovered_from`)** relocates the remaining
   vendor modules per (c): the `acli*` cluster (~29-test surface — migrate all patch
   strings atomically), `adf.py` + `outbound_fields.py` + `comment_limits.py` (**must
   also update the file-location loader** to discover the new sub-package dir), then
   rewires the differ/apply sites to depend on the backend *interface* rather than on
   `adapters/jira/` concretely.

## Consequences

- Phase 1 establishes the seam with a real (if small) relocation and the full suite
  green; it is a complete, defensible first step, not a speculative abstraction.
- The loader is **untouched** in Phase 1 (no location-pinned file moved), so dynamic
  loading cannot regress. Phase 2 owns the coupled loader + broad-test-surface work.
- Until Phase 2, core differ/apply modules still import `adapters/jira/` (and the
  root-level vendor modules) directly; the neutral-core boundary is *structural* now
  and becomes *enforced-by-interface* in Phase 2.
- A second backend is added by implementing `adapters/<x>/` against the operations in
  (b) — no core rewrite, once Phase 2 routes the core through the interface.
