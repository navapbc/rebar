# Janitor tools scratch

## 9ce8 reconciler do_not_move

Comment triage for ticket `9ce8-8ff1-0832-4825` (reconciler subtree). Every block below LOOKED
relocatable but is LOAD-BEARING — a measured external-API fact that cannot be re-derived from
code, an anti-refactor warning, a test-introspection dependency, or a lint/type pragma. Each was
deliberately KEPT VERBATIM (disposition DO_NOT_MOVE). `path:line` is approximate (post-edit).

### Measured external-API facts (Jira / ACLI behaviour that code cannot re-derive)

- `adapters/jira_datacenter/_hierarchy.py:136-140` — "do not 'simplify' that extra round trip
  away as redundant: on DC 8.17.1 the `fields.parent` write answers **HTTP 204 and is silently
  ignored**". Version-specific measured quirk + explicit anti-refactor warning. VERBATIM.
- `adapters/jira_datacenter/_hierarchy.py:180` — "Bug 1a9f-50c0-e7a5-4fda: DC answers this write
  with 204 and ignores it, so the write…". Measured status-code fact with incident id.
- `adapters/jira_datacenter/_hierarchy.py:205` — user-facing error text stating the same 204
  silent-ignore fact; changing it changes an observable message.
- `adapters/jira_datacenter/retry.py:153-160` — "READ FROM `exc.response.headers`, **NOT**
  `exc.headers` — verified against pycontribs/jira 3.10.5". Reading the wrong attribute
  type-checks and silently never finds `Retry-After`. Anti-refactor + measured library fact.
- `adapters/jira_datacenter/retry.py:37-66` — the labelled PROVENANCE of the three rate-limit
  numbers (each marked CONFIRMED / UNVERIFIED against Jira 8.17.1 / 8.6+ / Atlassian docs). The
  labels ARE the load-bearing content: they record exactly which numbers are measured vs
  assumed, and why the retry keys off header PRESENCE. VERBATIM.
- `adapters/jira/acli_graph.py:583-590` — "Bug 3b86: ACLI's `--out`/`--in` are INVERTED relative
  to the naive reading. Empirically (live-validated 2026-07-16): `link create --out X --in Y
  --type Blocks` creates 'Y blocks X'". Measured CLI-flag inversion with live-validation date.
- `adapters/jira/acli_cli_ops.py:418`, `adapters/jira/acli.py:690`,
  `adapters/jira/acli_graph.py:523,535,568` — "Probe-validated: returns 204 on success."
  Measured success-status facts per operation.
- `adapters/jira_datacenter/transitions.py` (docstring, "**Status is not an editable Jira
  field.**") — `PUT …/issue/{key}` with `fields.status` is rejected; a state is reachable ONLY
  via `POST …/transitions`. Measured REST asymmetry; the whole module's reason to exist.
- `adapters/jira_datacenter/_base.py:133-147` — "Jira DC silently truncates `maxResults` above
  `jira.search.views.default.max`". Measured server-side pagination quirk driving the stall
  probe.
- `fetcher.py:90-105` — `_ACLI_CEILING`/`_DONE_RECENT_CAP` provenance: "Raised from 1,000 to
  1,200 in bug f6cc after empirical confirmation that the DIG working set has 1,050 active +
  1,120 Done issues (probe 2026-05-26)". Measured working-set sizes; changing the constant
  needs a re-probe.
- `fetcher.py:357,377` + `_base.py` — "JRACLOUD-94632 silent truncation" and the DC
  `maxResults` truncation notes. Measured upstream-bug references.
- `dispatch_one.py:74-116` — retryable/non-retryable HTTP matrix (5xx/429 retry; 4xx-except-429
  fail-fast; 429 honours integer `Retry-After` off `exc.headers`; hierarchy-400 semantics).
  Measured status-class contract; the retry policy depends on each number.
- `dispatch_one.py:544-574` — "rejected with HTTP 400 carrying a misleading 'same project'"
  hierarchy-rejection handling. Measured misleading-message quirk.
- `binding_walk.py` / `classify.py` / `outbound_differ.py` 404-semantics blocks
  (CONFIRMED_404, consecutive-404 grace, "404 is 'gone' OR MOVED to another project and
  re-keyed", ADR 0028 §2). Measured deletion-vs-move ambiguity; load-bearing for retirement.
- `apply_outbound.py:209` — `status_code in (404, 410, 403)` idempotent-delete set. Measured
  status set.

### Anti-refactor warnings / structural invariants (splitting or "tidying" would break)

- `adapters/jira_datacenter/retry.py` (docstring) — "`TlsVerificationError` moves WITH its
  factory … splitting the two would make this module import `transport` while `transport`
  imports `_with_connection_retry` from here — a circular import. The class and its factory are
  one unit." Prevents a real import cycle.
- `_backend.py` `BackendPaginationStallError` (docstring) — "Lives in core beside the other
  `Backend*` errors … core must never import `adapters/`, so an adapter-local error would be
  unnameable at exactly the boundary that absorbs it." Placement invariant. (The recurring
  RATIONALE + the three incidents deac/9263/cabc were RELOCATED to ADR 0062; the placement
  invariant stays here.)
- `adapters/jira/acli_graph.py:41-48` `RunawayPaginationError` — "Base class (bug 9a46): …
  derives from `BackendPaginationStallError` … while this error sat outside that hierarchy the
  re-raise clauses missed it and a Cloud cursor stall was swallowed". MRO invariant with
  incident id; deriving from `RuntimeError` directly re-opens bug 9a46. VERBATIM.
- `outbound_differ.py:172-176` — "Collapsed under bug 7c26 … this module sits at the LOCKED
  module-size cap, so wiring the move-aware absence path had to buy its lines back from real
  duplication instead of from comments." Explains why the code is a duplication-collapse; a
  future editor "un-collapsing" it re-breaks the cap. Proximate design history.
- `reconcile.py` `persist` / `cap-0` / `mode cap` comments (lines ~216-705) — these describe the
  BEHAVIORAL write-cap (dry-run/reconcile-check are cap-0 → no persist), NOT the module-size
  cap. KEPT; not cap-prose to collapse.
- `adapters/jira_datacenter/_hierarchy.py` `_resolve_epic_link_field_id` (docstring) —
  "`getattr`/`setattr` on `_epic_link_field_id` … a transport built via `__new__` (some
  pagination tests) skips `__init__`'s assignment entirely, so a bare read would raise."
  Explains a non-obvious getattr guard tied to a test construction path.

### Test-introspection / re-export contracts (a test reads the name/facade through this module)

- `adapters/jira_datacenter/transport.py:107-118` — re-export facade note: "`test_jira_dc_config_
  settings.py` reaches for `TlsVerificationError` and `_with_connection_retry` through this
  module and must pass UNEDITED." Names a test that binds to the re-export.
- `adapters/jira_datacenter/retry.py` (docstring, re-export note) — same
  `test_jira_dc_config_settings.py` dependency via `transport`.
- `outbound_differ.py:44-83` — "(split for module size…)" import blocks each naming the test
  suite that pins the re-exported symbol (`test_identity_264f_resolve.py` pins
  `_bootstrap_account_id_via_user_search`; comment/label/link suites likewise). The cap
  parenthetical is incidental; the test-pin facts are load-bearing — block KEPT intact.
- `adapters/jira_datacenter/transport.py:12-19` (docstring) — "asserted by
  `tests/_jira_shape_contract.py`, the SAME shape contract that holds the Cloud transport
  honest". Names the contract test enforcing the unwrap boundary.

### Lint / type-checker pragmas (removing any breaks `make lint`/`make typecheck`, uncaught by tests)

- ALL `# noqa: …` (e.g. `# noqa: F401` on the re-export imports in `outbound_differ.py`,
  `_backend_registry.py:import rebar_reconciler.adapters  # noqa: F401`, `# noqa: BLE001` on the
  fail-open `except Exception` handlers in `fetcher.py`/`_hierarchy.py`), ALL `# type: ignore`,
  and ALL `# ruff:` directives across the subtree — **110 pragma lines total** (grep count).
  DO_NOT_MOVE, retained verbatim in place.

## Notes on dispositions applied (not do_not_move, recorded for provenance)

- RELOCATE→citation (ADR 0058, pre-existing): the six `RELOCATED VERBATIM out of transport.py`
  headers (`_links _base _hierarchy _people retry _issues`), retry.py's 789-line cap paragraph,
  transitions.py's relocation paragraph, outbound_labels.py's cap clause.
- RELOCATE→citation (ADR 0062, newly created): `_backend.py`
  `BackendPaginationStallError` rationale + incidents deac/9263/cabc.
- CORRECT-IN-PLACE (verified-stale): `_backend_registry.py` "STUB: … bodies to be implemented"
  (FALSE — `select_backend` fully implemented, imported by 12 modules / 38 sites); `_backend.py`
  "routing … is S4, config-driven selection is S3" (both landed); `adapters/jira/backend.py`
  "No core call site is rewired here (that is S4)" (rewired via `select_backend`).
