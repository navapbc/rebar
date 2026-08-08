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

## 4fc1 commands/CLI/engine_support/io do_not_move

Load-bearing blocks in `src/rebar/_commands/**`, `src/rebar/_cli/**`,
`src/rebar/_engine_support/**`, `src/rebar/_io/**` retained verbatim during the 4fc1
comment triage (path — one-line why):

### Lint / type-checker pragmas (removing any breaks `make lint`/`make typecheck`, uncaught by tests)

- ALL `# noqa: …` across the four subtrees — **83 lines total** (grep count), dominated by
  `# noqa: BLE001` on fail-open/fail-closed `except Exception` handlers (transition_close.py,
  close_precheck.py, verify_opcert.py, verify_authorship.py, identity.py, txn.py, delete.py,
  metrics.py, gates.py, reads.py, next_batch.py, bridge_fsck.py, import_ndjson.py, …),
  `# noqa: F401` re-export seams (transition_close.py:34, composer.py:631, transition.py:601,
  claim import, fsck.py:35, _llm_commands.py:21), `# noqa: E402` deferred-import cycles
  (composer.py:631, transition.py:601), the `# noqa: T201` CLI-`print` allowances in
  `_io/_cli.py` (8 lines), and `# noqa: SIM115` in export_ndjson.py:151. DO_NOT_MOVE, verbatim.
- `# pragma: no cover` — `_cli/_workflow_commands.py:301` (guards a broken template). DO_NOT_MOVE.
- `# tickets-boundary-ok` — `_engine_support/bridge_fsck.py:580` (boundary-lint pragma). DO_NOT_MOVE.

### Test-pinned user-facing output strings (changing them breaks golden/contract tests)

- `_commands/fsck_recover.py` `_USAGE` (`ticket-fsck-recover.sh …`) — printed to stderr on
  `--help`/usage error; the string IS the CLI contract, not stale narration. DO_NOT_MOVE.
- `_commands/compact.py:50` `Usage: ticket-compact.sh <ticket_id> …` — printed usage. DO_NOT_MOVE.
- `_commands/scratch.py:190` `"reason":"Usage: ticket-scratch-clear.sh …"` — emitted JSON
  envelope. DO_NOT_MOVE.
- `_commands/__init__.py` `main()` `Usage: ticket-commands.py <command> …` — printed usage.
  DO_NOT_MOVE.

### Measured external-API facts / live invariants (retained in place, lightly de-narrated only)

- `_commands/txn.py` **Byte-parity contract** paragraph — the canonical-serialization invariant
  (`rebar._store.canonical.canonical_str`, sorted keys, compact separators, `ensure_ascii=False`,
  byte-identical to every live writer) + the "do NOT split the commit out" anti-refactor warning.
  DO_NOT_MOVE (current invariant).
- `_engine_support/field_reads.py` output-contract bullets (spaced vs compact `json.dumps`,
  silent `[]` on miss, exact error strings + streams + exit codes) — measured external-API facts.
  Kept; only the "byte-parity with the dispatcher" framing was reworded to "byte-pinned by tests".
- `_engine_support/reads.py` the `/tmp/.ticket-sync-<md5>` throttle-marker description — documents
  CURRENT runtime behavior (the code builds that exact path). Kept; only the dead `ticket-sync.sh`/
  `_ensure_initialized` citations were corrected to `rebar._store.sync`.
- `_commands/leaf.py` module docstring "option-looking token / surplus positional is a loud usage
  error; `--` ends option parsing" — live arg-parsing invariant. Kept (CORRECT-IN-PLACE elsewhere).

### Notes on dispositions applied (provenance, not do_not_move)

- RELOCATE→citation (`docs/bash-migration.md` §7, new subsection "Post-cutover: where 'byte-parity'
  lives now"): the ~16 module-docstring bash-port narrations across the four subtrees.
- CORRECT-IN-PLACE (verified-stale): `_commands/txn.py` (dead `_engine/ticket_txn.py` shim "until
  E7"); `_engine_support/reads.py` (false engine-dir location reason); `_commands/leaf.py`
  (tag/untag/archive "tracked as child tickets" — all three defined in-file); `_engine_support/
  __init__.py` (non-existent `rebar/_engine/` compat shims); `_io/__init__.py` (import side "lands
  in a later sub-task" — already imported 6 lines above); `_cli/_help.py` (docstring truncated
  mid-clause by commit 3a53e202a7, completed); `_commands/init.py` (stale test path
  `tests/interfaces/test_e4_init.py` → `tests/interfaces/store/test_e4_init.py`).
- DELETE (referenced-artifact-gone): the dead byte-parity test pins
  `tests/interfaces/test_e4_fsck.py` and `tests/interfaces/test_e4_fsck_recover.py` (no such files
  exist anywhere in the tree) removed from fsck.py / fsck_recover.py module docstrings.
- CODE (behavior-preserving): `_engine_support/reads_cli.py` — extracted `_reject_unknown_option`
  and called it from the six unknown-option arms (show/list/session-logs/deps/ready/search),
  removing the repeated explanatory comment; observable output unchanged (verified by the CLI tests).

## 4b94 llm core — do_not_move

Comment triage for ticket `4b94` (llm/ core subtree, EXCLUDING `plan_review/` and `workflow/`).
Every item below was deliberately KEPT VERBATIM (disposition DO_NOT_MOVE) — a lint/type pragma,
a test-introspected docstring, a measured external-API fact, an anti-refactor / rejected-alternative
warning, or a load-bearing live-field decision. `path:line` is approximate (post-edit).

### Live-field decision (NOT dead code — do not delete)

- `runner.py` `RunRequest.extra_tools` field + its two use sites (`if req.extra_tools:` /
  `tools = [*tools, *req.extra_tools]`) — KEPT. The field is LIVE: set by
  `llm/workflow/completion_recovery.py:469,665` (the token-recovery record tool, story 2948),
  threaded through `workflow/runs.py`, and asserted by
  `tests/unit/workflow/test_completion_banking.py:491` ("the record tool is still wired"). The
  ticket's premise that it "is always None in practice" is FALSE; only the STALE comment was
  corrected in place, the field and call sites stay.

### Measured external-API / provider facts (cannot be re-derived from code)

- `capabilities.py` `_MODEL_ID_CAPABILITY_OVERRIDES` MEASURED matrix (~295-316) — "MEASURED
  (ticket 2932, real AWS us-east-2)" temperature-deprecation 400s, and the ticket-1903 boto3
  converse matrix (account 896586841071 / us-east-1). Real-AWS-account measured facts; VERBATIM.
- `capabilities.py` `native_output_with_thinking` provenance (~337-346) — MEASURED run E1
  (outputConfig json_schema + extended thinking wire-legal, no 400; sonnet-4-6 adaptive,
  haiku-4-5 budget 2048) and "the bare alias 400s at request validation". Measured; VERBATIM.
- `capabilities.py` `_is_claude` boto3-import note (~283-292) — measured import-cost reason
  (BedrockModelProfile drags botocore onto the always-run path). VERBATIM.
- `providers.py` deprecated-alias / pydantic-ai 1.107.1 `infer_provider` notes and the many
  measured `providers.py:line` behavioural facts in the retained body. Left as-is except the
  module docstring, which duplicated ADR 0059 §1 and was collapsed to a citation.
- Numerous measured 400/status-code facts across `failure.py`, `agent_call.py`,
  `structured_run.py`, `structured.py` (grammar-compilation-timeout server-side 400 after ~185s,
  Bedrock ValidationException classes). Out of primary scope / measured — untouched.

### Anti-refactor / rejected-alternative warnings (keep VERBATIM)

- `capabilities.py` `_REBAR_OVERRIDES` rejected-alternative (~268-281) — the flag-only rule
  `supports_json_schema_output and not supports_thinking` "was tried and REJECTED: it breaks
  gemini … and groq …. Do not reintroduce it." VERBATIM.
- `capabilities.py` `_MODEL_ID_CAPABILITY_OVERRIDES` "SEPARATE table … not a widening" +
  "must contain no prefix-match call, an attested S2 criterion" structural invariant. VERBATIM.

### Lint / type pragmas (never move)

- `capabilities.py` `# noqa: BLE001` on the `WebSearchTool()` isinstance probe and on `_is_claude`
  paths; all other `# noqa` / `# type: ignore` across the subtree. VERBATIM.

### Test-introspected / assertion-anchored docstrings

- `structured.py` `output_mode` docstring and `capabilities.py` override notes referenced by
  `tests/unit/test_structured.py` (the PromptedOutput-vs-NativeOutput assertions). Corrected the
  stale blanket-400 claim only where it CONTRADICTED current runtime; the measured/asserted
  content stays.
