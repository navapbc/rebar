# ADR 0083: Reconciler vendor-adapter seam (Jira-neutral core + `adapters/<backend>/`)

> **Renumbered by story 0743:** previously ADR 0035 (a number shared by 3 ADRs — a collision); reassigned to 0083 to make ADR numbers unique. See [RENUMBERING.md](RENUMBERING.md).

- **Status:** Accepted (Phase 1 landed; Phase 2 in progress under epic `bbf1-82e1-cf9d-494a`,
  which pins the backend interface in §(d))
- **Amended by:** [`0055-jira-family-sub-seam.md`](0055-jira-family-sub-seam.md) — epic
  `e369-a449-4773-48fb` landed **Jira Data Center** as the first real second backend, ahead of the
  GitHub adapter this ADR reserved that role for. ADR 0055 adds a `adapters/jira_family/` layer
  *inside* the adapter half (two contracts, `RichTextCodec` + `UserIdentityModel`), records the
  shared-`jira` provenance decision, and **narrows** this ADR's proof-of-seam claim: a contract
  suite certifies a backend only as far as the port is complete and typed. Read 0055 alongside
  §(c), §(d), and Decision items 5–6 below.
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
   `conflict_resolver.py`, `health.py`, `baseline_shadow.py`,
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
    jira_family/              # Jira-family SHARED layer (ADR 0055) — added after this ADR
    jira_datacenter/          # the first real second backend, landed by epic e369 (ADR 0055)
    <backend-x>/              # any further backend (e.g. GitHub, epic be74)
      __init__.py
      …
```

The `adapters/<backend>/` directory **is** the seam: everything under it is one
backend's concrete implementation of the operations in (b); everything at the root is
backend-neutral. (One exception was added later: `adapters/jira_family/` is a *shared* layer
for one vendor family rather than a backend — see
[`0055-jira-family-sub-seam.md`](0055-jira-family-sub-seam.md) §(a).)

### (d) Adding a second backend (pinned interface)

Phase 2 (epic `bbf1-82e1-cf9d-494a`) pins the concrete backend interface that §(a)/§(b)
left as a prose sketch. Four decisions fix the design:

**1. rebar's local ticket is the canonical model — there is no separate `CanonicalTicket`.**
The seam speaks the local-field vocabulary (summary/description/priority/status/labels/
links/comments) directly; each adapter translates vendor⇄local with the mappers that already
exist for Jira (`outbound_fields.py`, `inbound_fields.py`/`inbound_translate.py`). We do not
keep a parallel schema in lock-step, and there is no redundant `rebar⇄canonical` hop — the
mapper *is* the vendor⇄local translation.

**2. Core owns diff/apply; adapters only read and enact.** The differ/apply/dispatch/store/
invariant machinery computes what must change and drives the operations in §(b); an adapter
never diffs — it reads the remote (transport + inbound map) and enacts a decided mutation
(outbound map + sanitize + transport). This keeps convergence logic single-sourced in the
neutral core.

> **Canonical-comparison corollary (the core diffs in LOCAL shape).** Because the local
> ticket is the canonical model, the core compares in local shape and the port translates
> only at the boundaries: the core canonicalizes the remote snapshot via `InboundMapper`
> BEFORE diffing (mirroring the inbound differ), produces a `changed` set keyed by LOCAL
> field names, and maps that back to vendor field shapes via `OutboundMapper` only at the
> emission boundary. A vendor shape therefore crosses the core solely as an opaque payload
> produced/consumed at a port call — a core differ imports nothing from `adapters.*` and
> names no raw vendor snapshot key. The arbitration **baseline** is likewise canonicalized
> at READ time (storage stays vendor-shaped at rest), and the `InboundMapper` is
> partial-tolerant so a stored `_BASELINE_FIELDS` subset maps only the keys it carries.
> Two vendor operations the field diff still needs are reached only through the port —
> `OutboundMapper.map_fields_to_remote` (field-name reconciliation + value/rich-text
> mapping of the changed subset) and `OutboundMapper.resolve_assignee` (the account
> resolver fast-path). **Delivery:** the outbound-UPDATE **FIELD** diff and the **BASELINE**
> read are canonicalized here (ticket `625b`, adding the `assignee_identity` /
> `reporter_identity` / `remote_parent_id` canonical `InboundMapper` keys); the **LINK**
> diff is canonicalized under sibling ticket `eefd`. Labels/comments/links remain read from
> the raw snapshot by their own capability-diff paths — only the field path is canonical.

**3. Role Protocols behind one `Backend` facade.** A backend is a `Backend` object exposing
five required role Protocols, each derived from the de-facto surface the core already calls:

| Role Protocol | Responsibility (from §(b)) | Today's Jira delegate |
|---|---|---|
| `TicketTransport` | create/read/update/transition/comment CRUD against the remote | `acli.AcliClient` |
| `OutboundMapper` | local ticket fields → vendor field/value shapes (+ rich text) | `outbound_fields._map_local_to_jira_fields` (+ `adf.fit_text_to_adf_limit`) |
| `InboundMapper` | vendor issue payload → local field shapes | `inbound_fields`/`inbound_translate` |
| `FieldSanitizer` | defend vendor hard limits (label/summary/comment/description) | `adapters/jira/jira_fields.py` + `comment_limits.py` |
| `IdentityConvention` | how the backend stores/reads the `rebar-id` back-pointer | new pure object (Jira: `rebar-id:<id>` label) |

**Scalar surface (ticket `97f2`).** Beyond the five role Protocols, `Backend` also pins three
scalar members so the reconciler core stops reaching into `adapters.jira`/`acli_subprocess`
for project scope and connection readiness, plus two vendor-neutral exception types:

| Member | Responsibility | Today's Jira delegate |
|---|---|---|
| `Backend.project` | write/create project scope, with the backend's create-time default applied | `resolve_jira_settings(project_default="DIG").project` |
| `Backend.query_project` | read/query project scope, WITHOUT any create-time default (fail-closed) | `resolve_jira_settings().project` |
| `Backend.assert_env_ready()` | fail fast when a connection essential is missing, before the transport is used | checks `JIRA_URL`/`JIRA_USER`/`JIRA_API_TOKEN` |
| `BackendEnvError` | neutral "connection essentials missing" error raised by `assert_env_ready()` (subclasses `RuntimeError`) | n/a |
| `BackendAssigneeNotFoundError` | neutral base for "assignee resolves to no assignable remote user" | Jira's `acli_subprocess.AssigneeNotFoundError` subclasses it |

Plus three **opt-in capability Protocols** a backend advertises only when it supports the
feature: `SupportsLinks`, `SupportsComments`, `SupportsIncremental`. Callers detect a
capability by an `isinstance`-guarded check against the backend (a backend that does not
implement `SupportsLinks` is never asked to sync links) — capability is observed via behavior,
not structural introspection.

> **AMENDED by [rebar:1de3-a19f-c4ea-4b28].** `SupportsIncremental` was removed as a
> zero-implementer, unwired capability: no adapter ever defined `search_incremental` and no
> `isinstance(backend, SupportsIncremental)` dispatch existed, so the promised "core uses an
> incremental fetch" behaviour was never real. There are now **two** opt-in capability
> Protocols (`SupportsLinks`, `SupportsComments`). An updated-since incremental fetch can be
> reintroduced from vendor support (Jira JQL `updated >`, GitHub `since=`, Linear `updatedAt`)
> if a measured bandwidth need later arises.

> **AMENDED by [rebar:5c21-f24a-f7f8-4a55].** The `isinstance`-guarded rule above is
> NECESSARY BUT NOT SUFFICIENT on Python 3.12+, and taking it literally reintroduces a silent
> soft failure. This is temporal decay, not an authoring error: the guidance was correct when
> written and a later interpreter changed underneath it. Since CPython 3.12 (gh-102433) a
> `@runtime_checkable` Protocol `isinstance` resolves members with `inspect.getattr_static`,
> which deliberately does NOT see attributes served by `__getattr__`. Measured on one proxy
> object and one Protocol: 3.11 → `isinstance` True (the check was plain `hasattr`); 3.12 →
> False. rebar's requires-python is `>=3.11` and CI runs 3.11/3.12/3.13, so an isinstance-only
> guard is correct on one supported interpreter and wrong on another.
>
> The consequence lands exactly where capability Protocols are meant to help. A transport that
> forwards dynamically to an inner client — a retry wrapper, an instrumentation shim, a
> recording proxy, and every `MagicMock` test double — HAS the capability yet fails
> `isinstance`. A caller following the unamended rule would skip the write and record the skip
> as DESIGNED, which is a real failure wearing an "intended" label.
>
> **The sanctioned shape is `isinstance` PRIMARY with a member-level `hasattr` fallback:**
> `if isinstance(client, protocol) or hasattr(client, member)`. `isinstance` stays primary so
> a backend's advertised capability remains the declared contract; the fallback closes the
> proxy hole. Check the specific `member` you are about to call rather than the whole
> Protocol — at a write dispatch site the question is "can this transport perform THIS call",
> not "does it also implement the read side". The reference implementation is
> `dispatch_apply_phases._capability_present` (task `a3fa-e8d4-f4aa-4b51`), which is also
> consistent with the `hasattr`-based capability checks already used in `fetcher.py`.
>
> A tree-wide sweep under Python 3.12 found no other affected site: the single executable
> isinstance-against-Protocol dispatch in `src/rebar` is `_capability_present` itself (already
> hardened), and the only other Protocol `isinstance` is `_TransportPortMeta.__instancecheck__`,
> whose `hasattr`-based body is proxy-safe by construction. The fallback is deliberately NOT
> centralised into a shared helper: with one dispatch expression in the tree there is no second
> caller to share it, so extraction would be premature.

**4. One new identity type `RemoteRef{vendor, instance, remote_id}`.**

> **AMENDED by [rebar:6a91-7429-e521-4a2e].** This section originally introduced `instance` as
> the thing that stops two deployments of one vendor colliding. That is true of the VALUE and
> false of the STORE. `inbound_translate._jira_key_to_local_id` derives the local ticket id from
> the Jira key alone (`"jira-" + jira_key.lower()`), so two deployments sharing a project key mint
> the SAME local id whatever `instance` holds — nothing consults it at mint time. `vendor` already
> separates Cloud from Data Center; `instance` distinguishes same-vendor deployments within the
> identity value only. `RemoteRef` is not persisted, and `Backend.remote_ref()` derives `instance`
> from the configured base URL at construction time.

This identity tuple
replaces the hardcoded `"jira"` provider literal in `apply_inbound_records.py` and the bare
`jira_key` threaded through the apply path. `IdentityConvention` formats a `RemoteRef` to the
backend's back-pointer label and parses it back, so provider identity is a typed value rather
than a string literal inlined at four core call sites.

**Selection.** The neutral core obtains its `Backend` from an in-tree registry keyed on
`config.reconciler.backend` (default `"jira"`); a second backend registers itself under
`adapters/<x>/` and is chosen by config. No core module imports `rebar_reconciler.adapters.
jira.*` once Phase 2 routes the differ/apply sites through the `Backend` port.

**Three leak-fixes fold Jira specifics back into the adapter.** As part of routing core through
the port, Jira-specific logic that leaked into backend-neutral core is single-sourced under
`adapters/jira/`: (i) ADF size-fitting and the lossy status/parent value rules; (ii) the
duplicated priority/status value-maps; (iii) the `outbound_links` link-relation constant.

**Proof-of-seam.** Phase 2 proves the interface with a backend-agnostic **contract test suite**
run against both `JiraBackend` (a thin delegation wrapper over today's Jira modules, zero
behavior change) and a test-only in-memory `FakeBackend`.

> **SUPERSEDED (see [`0055-jira-family-sub-seam.md`](0055-jira-family-sub-seam.md)).** This ADR
> originally said the first *real* second backend (a GitHub adapter) was out of scope here and
> tracked by epic `be74-7832-03a8-48ac`. Epic `e369-a449-4773-48fb` landed a **different** second
> backend first — **Jira Data Center**, `adapters/jira_datacenter/` — so a real second backend now
> exists ahead of `be74`, which still owns the GitHub adapter. ADR 0055 also narrows the
> proof-of-seam claim above: the contract suite certifies a backend only as far as the `Backend`
> port is **complete and typed**. `TicketTransport` declared six members while the core reached for
> twenty-one; a DC writing pass crashed at runtime while `isinstance`, the contract suite, and
> 1600+ unit tests were all green.

To add backend **X**, create `adapters/<x>/` implementing the five role Protocols (and any
capability Protocols X supports), register it under `config.reconciler.backend = "<x>"`, and
the neutral core drives it unchanged.

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
5. **Phase 2 (epic `bbf1-82e1-cf9d-494a`)** pins the backend interface per §(d) — the
   `Backend` facade, its five role Protocols + three opt-in capability Protocols, and the
   `RemoteRef` identity type — then routes the differ/apply sites through that port,
   single-sources the three leak-fixes under `adapters/jira/`, and relocates the remaining
   vendor modules per (c): the `acli*` cluster (~29-test surface — migrate all patch
   strings atomically), `adf.py` + `outbound_fields.py` + `comment_limits.py` (**must
   also update the file-location loader** to discover the new sub-package dir). A thin
   `JiraBackend` and a test-only `FakeBackend`, both exercised by one backend-agnostic
   contract suite, prove the seam.
   **AMENDED by [rebar:1de3-a19f-c4ea-4b28] —** the "three opt-in capability Protocols"
   count above is now **two**: `SupportsIncremental` was removed as a zero-implementer,
   unwired capability (see the §(d) amendment above).
6. **Standing up a concrete second backend is out of scope for this ADR.** This ADR (through
   Phase 2) establishes and proves the seam; building an adapter against it is a delivery
   epic's work — enabled by a one-line `config.reconciler.backend` switch once its
   `adapters/<x>/` package is registered.
   **AMENDED —** this item originally said "the first real second backend … is tracked
   separately by epic `be74-7832-03a8-48ac`". That is no longer true: epic
   `e369-a449-4773-48fb` landed **Jira Data Center** (`reconciler.backend = "jira-datacenter"`)
   as the first real second backend, ahead of the GitHub adapter `be74` was reserved for and
   still owns. See [`0055-jira-family-sub-seam.md`](0055-jira-family-sub-seam.md), which also
   records the `adapters/jira_family/` layer that second backend required.

## Consequences

- Phase 1 establishes the seam with a real (if small) relocation and the full suite
  green; it is a complete, defensible first step, not a speculative abstraction.
- The loader is **untouched** in Phase 1 (no location-pinned file moved), so dynamic
  loading cannot regress. Phase 2 owns the coupled loader + broad-test-surface work.
- Until Phase 2, core differ/apply modules still import `adapters/jira/` (and the
  root-level vendor modules) directly; the neutral-core boundary is *structural* now
  and becomes *enforced-by-interface* in Phase 2 — enforced by the pinned `Backend` port
  in §(d), which the core depends on instead of on `adapters/jira/` concretely.
- A second backend is added by implementing the §(d) role Protocols under `adapters/<x>/`
  and selecting it via `config.reconciler.backend` — no core rewrite, once Phase 2 routes
  the core through the interface. Standing up such a backend is a delivery epic's work, not
  this ADR's; the first one landed is **Jira Data Center** (epic `e369-a449-4773-48fb`, see
  [`0055-jira-family-sub-seam.md`](0055-jira-family-sub-seam.md)), and the GitHub adapter
  remains epic `be74-7832-03a8-48ac`.
