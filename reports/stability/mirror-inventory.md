# Representation-mirror inventory — 2026-09-01 (sweep of `src/rebar`, docs, and scripts)

Ticket `yogic-soapstone-takin` (`f9b9-83ad-931c-4833`), under epic `wide-wimpy-insect`
(Track I — reduce defect introduction).

A **representation mirror** is one canonical fact with **two masters** that can drift
apart. Representation drift is the dominant sub-mechanism of the product-logic defect
category in the R2S1 mechanism read, so an un-inventoried mirror is an un-inventoried
defect source. This is the inventory; the fixes land as the follow-up tickets listed per
finding, not in this task.

## Headline

- **77 candidate sets examined** across five areas; **14 genuine mirrors**; **5 already
  drifted** at the time of the sweep.
- **3 rated high severity**: one risks silent permanent data loss (F1), one is drifted
  today in a document that advertises a gate it does not have (F12), and one leaves a
  known silent-degradation class open for two of three gates when the validator that
  prevents it already exists and is one line away (F13).
- The single most reliable discriminator found: **a comment claiming "single-sourced" or
  "kept in lockstep" is a marker for an *unenforced* mirror.** All four such claims in the
  tree are false (F1, F3 ×2, F12).
- Equal counts hide drift. F12's doc table and its registry both have 78 rows and
  *different members* — one addition and one stale row cancelling out. Count comparison
  alone would have missed it; only a set difference found it.

## Verdict vocabulary

| verdict | meaning |
|---|---|
| GENUINE MIRROR | same fact, two masters, nothing enforces agreement |
| DISTINCT CONCEPT | looks duplicated, is semantically a different predicate |
| ALREADY GATED | a generator or test enforces agreement (named) |

The DISTINCT CONCEPT bar is the one bug `e63c` drew when it unified the terminal-status
set: a *subset defined for one predicate* is not a mirror of the set it draws from. That
distinction rejected 9 candidate families here (see "Rejected").

## The 14 genuine mirrors

### F1 — Reducer event-type dispatch has two masters · HIGH · not yet drifted

| master | location |
|---|---|
| `_EVENT_HANDLERS` (19 keys) | `src/rebar/reducer/_replay.py:93` |
| `KNOWN_EVENT_TYPES` (19 values) | `src/rebar/reducer/_version.py:86` |

Fact: the set of event types the reducer dispatches. Values: `ARCHIVED BRIDGE_ALERT
COMMENT COMMITS CREATE EDIT FILE_IMPACT KEY_ADD KEY_REVOKE LINK REVERT SIGNATURE SNAPSHOT
STATUS TAG_DELTA UNLINK VERIFY_COMMANDS WORKFLOW_RUN WORKFLOW_STEP`. Symmetric difference
is empty today.

Why it is the worst one: `_replay.py:195` gates on `KNOWN_EVENT_TYPES` *before* handler
lookup, and `_commands/compact_plan.py:131` makes known types eligible for SNAPSHOT squash
and file retirement. A type present in `KNOWN_EVENT_TYPES` but absent from
`_EVENT_HANDLERS` is therefore folded into nothing and then deleted — **silent permanent
data loss**, not a loud failure.

Phantom gate: `tests/interfaces/contracts/test_event_schema_forward_compat.py:16` claims to
pin the parity; its body (`:90-97`) asserts 3 memberships. `grep "EVENT_HANDLERS" tests/`
returns nothing.

Fix: **generate-from-canonical** — `KNOWN_EVENT_TYPES = frozenset(_EVENT_HANDLERS)`.
Precedent in-tree at `_capabilities.py:203`.

### F12 — `docs/exit-codes.md` per-command table vs `ROUTES` · HIGH · **ALREADY DRIFTED**

| master | location |
|---|---|
| `ROUTES` (78 live routes) | `src/rebar/_cli/_registry.py` |
| hand-maintained per-command table (78 rows) | `docs/exit-codes.md` |

Drifted **in both directions**: live route with no row — `bridge-status`; row for a
non-route — `review` (`docs/exit-codes.md:184`), which is not even `retired=True` but
absent from `ROUTES` entirely.

The aggravating factor is the false gate claim. `docs/exit-codes.md:5-7` states the file is
"the single source of truth ... pinned by `tests/interfaces/lifecycle/test_exit_codes.py`,
which fails if the codes drift." The entire assertion touching the file (`:236-243`) is:

```python
assert "`11`" in text and "block-but-retryable" in text.lower()
```

A reader has positive reason to trust a document that nothing checks, and exit codes are
load-bearing for agents driving the CLI.

Fix: **add-census-assertion-to-existing-test** — set-difference the table's rows against
live `ROUTES` inside `test_exit_codes.py`.

### F13 — Code-review gate step ids duplicated in Python, unvalidated · HIGH · not yet drifted

| master | location |
|---|---|
| `- id: verify` / `- id: decide` | `src/rebar/llm/workflow/gates/code-review.yaml:174,206` |
| `_STEP_VERIFY` / `_STEP_DECIDE` | `src/rebar/llm/code_review/finalize.py:28-29` (used `:205,211,217,300`) |

`finalize.py:15,27` states the coupling outright: "a rename of those there must be mirrored
here." A validator for exactly this hazard already exists — `_validate_gate_step_ids` — and
has **one** call site: `gate_dispatch.py:154`, `gate_name="plan-review"`.
`plan_review_recovery.py:31-36,84-87` records why it was written: a step-id rename "would
otherwise make the recovery lookups silently return `None` and degrade a recoverable run to
INDETERMINATE." Renaming `verify` or `decide` reproduces precisely that, unguarded. Neither
`code-review.yaml` nor `completion-verification.yaml` is validated.

Fix: **add-parity-check** — call the existing helper at the code-review dispatch site. This
is also the portable pattern (runtime validation, no CI provider needed) that F1, F3 and
F11 should follow.

### F5 — MCP output models re-type canonical enums; the gate is enum-blind · HIGH · **ALREADY DRIFTED**

Distinct from sibling story `cream-capitate-snake` (tool↔facade); this is models↔schemas.
`src/rebar/_mcp_models.py:1-24` declares itself a pydantic-only leaf with no `rebar.*`
edges, so every enum **must** be hand-copied.

| copy | canonical |
|---|---|
| `_mcp_models.py:61-68` `PinStatus` (6) | `llm/plan_review/pin_health.py:21` |
| `_mcp_models.py:54` `TargetPinStatus` (4) | `pin_health.py:29` (third copy as dict keys at `:193`) |
| `_mcp_models.py:122` `FileImpactScope` | `types.py:22` |
| `_mcp_models.py:284-295,298,336` | `schemas/bridge_{run,status,access_check}.schema.json` |

Two gaps in `schemas/check_mcp_models.py`: `missing_declarations` (`:51-57`) is a
**property-name** difference and never reads `enum`; and `MODEL_SCHEMAS` (`:22-34`) covers
11 of 34 `*Out` models. Running the gate's own predicate over the unregistered ones shows
`TicketStateOut` missing 23 properties, `NextBatchOut` 11, `GateResultOut` 5,
`CreateResultOut` 1. Both `PlanReviewHealth*Out` — which carry the `pin_status` copies —
are absent entirely. This is the published MCP wire contract.

Fix: **add-parity-check** — compare `enum` values, and derive/assert `MODEL_SCHEMAS`
completeness rather than hand-listing it.

### F3 — Plan-review ticket-type exemption: six sites, two complementary sets, zero enforcement · HIGH · code aligned, comments drifted

| set | locations |
|---|---|
| exempt `("bug","session_log","code_review","identity")` | `_commands/gates.py:45`, `_commands/composer.py:47`, `llm/plan_review/orchestrator.py:560` |
| complement `("task","story","epic")` | `llm/plan_review/claimability.py:38`, `_commands/transition_close.py:46`, `_commands/gates.py:215` |

`gates.py:42-44` claims "Single-sourced here so … cannot drift" — false. `composer.py:42-46`
admits "Kept in lockstep with". `claimability.py:40-41` prose names 2 of the 4 exempt types.
The only test is `assert "bug" in …` (`test_bug_blast_radius_escalation_ad0d.py:199`).

Consequence: an eighth ticket type lands in neither set — neither gated nor exempted, which
decides whether the claim gate runs at all.

Fix: **add-parity-check** — assert `set(get_args(TicketType)) == exempt | reviewed`, with all
six sites referencing one constant.

### F4 — Blocking/non-blocking relation partition · MEDIUM · **ALREADY DRIFTED (prose)**

| master | location |
|---|---|
| `_BLOCKING_RELATIONS = {"blocks","depends_on"}` | `src/rebar/graph/_relations.py:17` |
| hardcoded complement (5 relations) | `src/rebar/graph/_graph.py:210` |
| hardcoded positive `("blocks","depends_on")` | `src/rebar/graph/_links.py:279` |

Both docstrings list only **four** non-blocking relations, omitting `caused_by` —
`_graph.py:194-196` (two lines above a five-element code list) and `_links.py:277-279`.
Consequence of a future miss: a new non-blocking relation falls to the `else:` branch, is
cycle-checked as `blocks`, and a valid link is rejected with `CyclicDependencyError`.

Fix: **generate-from-canonical** — `return relation not in _BLOCKING_RELATIONS`.

### F10 — Three doc enumerations already drifted from their code masters · MEDIUM · **ALREADY DRIFTED**

| doc | master | drift |
|---|---|---|
| `docs/reuse-surface.md:132-141` (8 rows) | `schemas/verify_signature_result.schema.json:17` (9 verdicts) | `key_not_valid_at_era` missing (it *is* documented at `docs/identity.md:138,147`, so stale, not intentional) |
| `docs/oss-comparison-and-remediation.md:110-111` | `types.py:21/23/24` | statuses missing `idea`; types missing `session_log`/`code_review`/`identity`; says "six relations" — there are seven |
| `docs/user-guide.md:147` | `types.py:21` | 5 of 7 statuses; `archived`/`deleted` missing though the same file discusses them at `:560,568-571` |

Fix: **add-parity-check** (docs↔schema census). Agent-facing guidance that is already wrong.

### F2 — Op-cert kinds have three masters · MEDIUM · not yet drifted

`signing.py:115` `OPCERT_KINDS` (exported in `__all__:76`) vs `opcert_service/jobs.py:38`
`VALID_KINDS` vs `_commands/remote_cert.py:24` `_VALID_KINDS` (+ a fourth prose copy in
`_USAGE:23`). Neither consumer imports the exported name; both gate client request
validation. Low churn (ADR 0049 fixes the count at two), but a miss silently rejects a
legitimate kind. Fix: **generate-from-canonical**.

### F6 — `TicketStatus` re-declared in the reducer · MEDIUM · not yet drifted

`types.py:21` / `schemas/common.schema.json#/$defs/ticket_status` vs
`reducer/_processors.py:33` `_KNOWN_TICKET_STATUSES` (same 7). `_processors.py:565` raises
`ValueError(f"unknown ticket status in snapshot: {status!r}")`, so it *asserts*
completeness; ungated. A new status makes snapshots unreadable. Fix:
**generate-from-canonical** — `frozenset(get_args(TicketStatus))`.

### F7 — `TicketType` re-declared on the create path · MEDIUM · not yet drifted

`types.py:23` vs `_commands/composer.py:40` `_TYPES` (+ prose copy at `_USAGE:55`).
`composer.py` is the outlier: `_engine/rebar_reconciler/inbound_fields.py:119` already
derives it as `frozenset(get_args(TicketType))`. A new type is rejected at create. Fix:
**generate-from-canonical** — copy the sibling's derivation.

### F8 — `CANONICAL_RELATIONS` gated to docs but not to the schema · LOW · not yet drifted

`types.py:24` / `common.schema.json#/$defs/relation` vs `graph/_links.py:20` (same 7).
`tests/interfaces/contracts/test_event_schema_relations.py:23` pins it to
`docs/event-schema.md`; nothing pins it to the schema or `types.Relation`. Fix:
**add-parity-check** — extend the existing test with the schema arm.

### F9 — Config precedence layers have two masters · LOW · not yet drifted

`config.py:326` `LAYER_ORDER = ("default","user","project","env","cli")` vs
`_operation_config.py:51` `_SOURCE_KINDS` (same 5). `_operation_config.py:128` validates a
provenance label whose domain `config.py:337` documents as "label ∈ LAYER_ORDER". Fix:
**generate-from-canonical** — `frozenset(config.LAYER_ORDER)`; ordering is the tuple's only
additional fact.

### F11 — `_CROSS_SESSION_WARN_COMMANDS` verb spellings unvalidated · LOW · not yet drifted

`_cli/_execute.py:22`, 14 hardcoded verb spellings consumed at `:74`, against `ROUTES`. All
14 verified live today.

Scope correction made during the sweep: *which* verbs warn is a **DISTINCT CONCEPT** (a
predicate subset, the same rule applied to `_NOT_CLAIMABLE_STATUSES`); the mirror is only
the **spellings**, which must match route names with nothing enforcing it. It is the one CLI
policy subset not folded into `derive_policy_sets` (`_registry.py:680`). Fix:
**add-route-flag** — `warn_cross_session` on `Route`.

### F14 — The 17-verb "every mutating verb" list has three masters · LOW · not yet drifted

`_cli/_registry.py:680` derived `_CONFIRM_SCOPE` vs `scripts/gen_cli_reference.py:43-46`
`EDITORIAL_PREAMBLE` prose (emitted into `docs/cli-reference.md:8`) vs `docs/user-guide.md:27`.
All three at 17/17 today. `_check_mutation_verbs()` (`gen_cli_reference.py:194-205`) compares
the `MUTATION_VERBS` **dict keys** to `_CONFIRM_SCOPE`, not the prose; `lint_editorial`'s route
rule fires only on `rebar <verb>` forms and the preamble uses bare backticks;
`docs/user-guide.md` is ungated. Fix: **add-parity-check** — extend `_check_mutation_verbs`
to both prose copies.

## Rejected — DISTINCT CONCEPT, not mirrors

Recorded because a false positive here is expensive: proposing a "fix" that collapses two
genuinely different predicates would introduce a defect rather than remove one.

| candidate | why it is not a mirror |
|---|---|
| `_store/event_prepare.py:35` `EVENT_TYPES` | write allow-list; a deliberate superset, delta documented as `_NON_REPLAY_KNOWN_TYPES` |
| `llm/code_review/bugfix_size_gate.py:63-82` `_COMPUTE_VALIDITY_VERDICTS` | deliberate 13-value subset with written rationale (`:57-61`) |
| `graph/_ready.py:27`, `reducer/_state.py:20`, `classify.py:43`, `claimability.py:34` | four different predicates over `TicketStatus`; the `e63c` distinction — `_TERMINAL_LOCAL_STATUSES` differs from `TERMINAL_STATUSES` by `closed` (store-terminal vs sync-terminal) |
| `_commands/close_disposition.py:46/54/63/74` | four documented predicates over `CloseClass`; `close_precheck._NON_COMPLETION_BUG_CLASSES` is an alias object, not a copy |
| gate-run statuses | one producer (`llm/gate_runs.py:127-165`); `_mcp_models.py:414` types it `str` with values in a comment — prose, not a master |
| `_errors.py:48`, `_capabilities.py`, `_deprecations._KINDS` | single master each |
| `scripts/check_raw_git_writes.py:122` `MUTATION_VERBS` | git subcommands; name collision only |
| Makefile vs CI script enumeration | the 5 direct CI steps are redundant re-invocations; `make lint` also runs and is asserted at `tests/unit/test_ci_workflow_parity.py:72` |
| `rebar explain` guides registry | an unregistered `_guides/*.md` is inert, so no canonical fact drifts |

## Already gated — excluded from the findings

| fact | mechanism |
|---|---|
| config keys ↔ docs | `scripts/gen_config_reference.py` |
| `REBAR_*` env vars ↔ docs | `scripts/gen_env_registry.py` |
| library public surface | `scripts/gen_api_surface.py` + `tests/unit/api_surface_baseline.json` (symbol names only — does **not** gate prose in `docs/reuse-surface.md`, hence F10) |
| MCP tool ↔ library facade | sibling story `cream-capitate-snake` (`8ce5-b870-601d-4715`) — referenced, not re-filed |
| `types.py` ↔ schemas | `python -m rebar.schemas.gen_types`, pinned by `test_public_types.py:25-27` |
| `CloseClass` | `test_administrative_close_dispositions.py:105-115` — **the exemplar**: parameterized reach-every-consumer plus `tuple(enum) == txn.CLOSE_CLASSES` including order |
| `CreationChannel` | `test_creation_channel_vocabulary.py` |
| schema registry ↔ disk | `test_schema_coverage.py:7-9`, bidirectional |
| plan-review workflow step ids | `_validate_gate_step_ids` at `gate_dispatch.py:154` — runtime parity, no CI provider needed |
| CLI help artifacts, `docs/cli-reference.md` | `gen_cli_help.py --check`, `gen_cli_reference.py` |
| `DECLARED_EXTRAS` ↔ `pyproject.toml` | `test_cli_registry_full_extra.py:108-117`, bidirectional |
| `server.json` ↔ `MCP_ENV_VARS` | `check_server_manifest.py` |
| deploy paths | `check_deploy_manifest.py` |

Caveat carried forward: `tests/unit/test_ci_workflow_parity.py` is itself a **partial** gate —
one-directional membership only, and nothing ties `_SHARED_GATE_SIGNATURES` (`:66`) /
`_DOCUMENTATION_GATE_SIGNATURES` (`:84`) back to the scripts on disk. `gen_api_surface.py`,
`gen_config_reference.py` and `check_orphaned_load.py` are in neither dict (they are driven
only via `importlib` from pytest modules), so they run in CI but are invisible to that census.

## Method — repeatable

Run from the repo root with the worktree venv first on `PATH`
(`env PATH="$PWD/.venv/bin:$PATH" …`). In zsh, quote every `--include` glob.

```sh
# Area 1 — enumerations duplicated across modules
grep -rn "frozenset(" src/rebar --include="*.py"
grep -rn "Literal\[" src/rebar --include="*.py"
grep -rn "Enum)"     src/rebar --include="*.py"
python -c "import json;d=json.load(open('src/rebar/schemas/common.schema.json'));\
[print(k,v['enum']) for k,v in d['\$defs'].items() if 'enum' in v]"
grep -rn "get_args(\|frozenset(_" src/rebar --include="*.py"   # derives vs copies

# Area 2 — doc tables restating code constants
grep -rn -i "generated\|do not edit\|autogenerated" docs/      # gated vs hand-maintained
grep -rn "discovered_from\|session_log\|plan_defect\|in_progress" docs/

# Area 3 — schema fragments re-declared
ls src/rebar/schemas/*.schema.json

# Areas 4-5 — registries, CLI verbs, path/name lists
grep -rn "_CONFIRM_SCOPE\|MUTATION_VERBS\|DECLARED_EXTRAS" src scripts tests docs
grep -n "scripts/\(check\|gen\)_[a-z_]*\.py" Makefile
grep -rn "_REQUIRED_STEP_IDS\|_validate_gate_step_ids(" src/rebar --include="*.py"

# Universal: is the copy gated? A zero-hit tests/ grep IS the finding.
grep -rn "<CONSTANT_NAME>" tests/ src/rebar scripts/
```

Two structural diffs did the decisive work, and neither is a grep:

1. regex-extract `_EVENT_HANDLERS` keys, compare as a **set** against `KNOWN_EVENT_TYPES`
   members (F1);
2. import live `ROUTES` and set-difference against markdown rows matching
   `^\| \`([a-z][a-z0-9-]*)\` \|` (F12) — **equal counts hid this drift**, so a count
   comparison would have reported agreement.

## Candidates examined per area

| area | examined | mirrors |
|---|---|---|
| 1 — enumerations duplicated across modules | 25 | F1, F2, F3, F4, F6, F7, F8, F9 |
| 2 — doc tables restating code constants | 14 | F10 |
| 3 — schema fragments re-declared | 9 | F5 |
| 4+5 — registries, CLI verbs, path/name lists | 29 | F11, F12, F13, F14 |
| **total** | **77** | **14** |

## Disposition

Each of the 14 genuine mirrors has a follow-up ticket proposing the named fix pattern,
linked `discovered_from` this task and parented under the remediation epic
**`undamaged-murderous-horse`** (`f79d-0d71-0fd7-439f`), which is linked `relates_to`
Track I (`wide-wimpy-insect`, `87cb-7121-a3b6-4606`).

The remediation lives in its own epic rather than as children of Track I deliberately:
Track I ships the *gates*, and its AC2 treats a live mirror as dispositioned once it "is
either gated/generated or has a linked child/discovered_from ticket" — i.e. filing is the
bar, fixing is follow-on work. Parenting 14 open remediation tickets under Track I would
have held that epic open on work its own acceptance criteria do not require.

Recommended order: **F1** (silent data loss), **F12** (drifted now, false gate claim,
agent-load-bearing), **F13** (one line, reuses a validator that already exists).

One caution carried into the remediation epic: the "Rejected" table above lists nine
candidate families that look duplicated but are semantically different predicates. A fix
that collapses one of those would introduce a defect rather than remove one, so each
remediation should be reviewed against that rationale.
