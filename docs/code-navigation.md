# Code navigation — Serena and `grep` fail in opposite directions

`AGENTS.md` §"Navigating the codebase" carries the rule as a four-row table. This note records
the evidence behind it, because the rule replaced an earlier one that was **wrong in an
important case** — and a rule that is wrong some of the time teaches agents to discount it all
of the time.

Both tools are load-bearing. Neither is a superset of the other.

## The LSP cannot see a symbol named by a string literal

Serena is LSP-backed (Pyright over `src/rebar`). It resolves *symbols*. When a symbol is named
by a **string**, there is no symbol reference for the LSP to resolve, and the site is invisible
to it.

Reproduced in this repo on `_anthropic_cache_settings`:

```
find_referencing_symbols(_anthropic_cache_settings, src/rebar/llm/anthropic_model.py)
  -> src/rebar/llm/runner.py                    (import + the call site)
  -> tests/unit/test_pydantic_ai_runner.py      (import + 4 call sites)
  -> tests/interfaces/store/test_execution_mode_dispatch.py   ** NOT RETURNED **
```

The missing site is:

```python
monkeypatch.setattr(runner_mod, "_anthropic_cache_settings", lambda resolved: None)
```

`grep -rn _anthropic_cache_settings src/ tests/` finds it.

This is not a hypothetical. The identical pattern for `_build_retrying_anthropic_model` broke
epic `061c` story S1: the change passed `tests/unit` and failed only in full-suite pre-flight,
because the patch site that mattered was named by a string. An agent following an
unconditional "prefer Serena" rule would have missed exactly the site that failed CI.

The same blind spot applies to any dynamic reference: `getattr(mod, "name")`,
`importlib.import_module`, `setattr`, entry-point strings in `pyproject.toml`, a symbol named in
a config file or a prompt template.

## The LSP cannot bind an attribute on a receiver typed `Any` — and says so with silence

The string-literal blind spot at least *looks* incomplete once you know to expect it. This one
does not: Serena returns a well-formed **empty** result, indistinguishable from "this symbol is
genuinely unused".

Reproduced in this repo on `AcliRestMixin.set_entity_property` (defined at
`src/rebar/_engine/rebar_reconciler/adapters/jira/acli_rest.py:266`):

```
find_referencing_symbols("AcliRestMixin/set_entity_property",
                         "src/rebar/_engine/rebar_reconciler/adapters/jira/acli_rest.py")
  -> {}                                    # zero references
```

`grep -rn set_entity_property src/rebar/` finds three real call sites:

- `src/rebar/_engine/rebar_reconciler/dispatch_one.py:321` —
  `_call_with_retry(client.set_entity_property, jira_key, "local_id", local_id)`
- `src/rebar/_engine/rebar_reconciler/apply_inbound_events.py:214` —
  `_call_with_retry(client.set_entity_property, jira_key, "local_id", local_id)`
- `src/rebar/_engine/rebar_reconciler/binding_store.py:517` —
  `client.set_entity_property(keyed, "local_id", local_id)`

This is not "Serena is broken", and it is not "that file is unindexed". The control is a sibling
method in the same class in the same file: `find_referencing_symbols` on
`AcliRestMixin/_direct_rest_put_raw` returns **5** references across 3 files — three inside
`acli_rest.py` and one in `acli.py:693` via `self`, plus one in `jira-capability-probe.py:98`
through a local `client`. A typed attribute resolves too: `TicketTransport` in
`_engine/rebar_reconciler/_backend.py` correctly returns `Backend/transport`, its return
annotation.

The discriminator is the **static type of the receiver**, not "untyped" in any loose sense:

- `jira-capability-probe.py:73` binds `client = AcliClient(...)` — an *inferred concrete type*,
  so the `client._direct_rest_put_raw` call at `:98` is **found**.
- `dispatch_one.py:191` `def create_one(mutation: dict, client, ...)` — an unannotated parameter,
  i.e. implicit `Any` → **missed**.
- `apply_inbound_events.py:206`
  `def _inbound_create_writeback_jira(client, jira_key, local_id, tracker_dir) -> None:` —
  unannotated, implicit `Any` → **missed**.
- `binding_store.py:479` `def recover_pending_bindings(self, client: Any, *, failure_sink=...)`
  — an **explicit** `Any` annotation, which fails identically to the unannotated case →
  **missed**.

So the rule is: *an attribute access on a receiver whose static type is `Any` — implicit
(unannotated parameter) or explicit (`: Any`) — cannot be bound to a definition by Pyright, so
`find_referencing_symbols` returns an empty result.* Writing this off as "unannotated code" is
wrong; `binding_store.py:479` spells the annotation out and is missed all the same.

The related untyped plumbing in the same area behaves the same way — `dispatch_one.py:517`
`def _update_one_apply_parent(fields, issue_key, client) -> bool:` and
`dispatch_apply_phases.py:91`
`def _update_one_apply_reporter(fields, issue_key, client) -> None:`.

The practical consequence is an asymmetry. A **non-empty** Serena result is trustworthy: what it
returns is really a reference. An **empty** result is only trustworthy when every receiver is
statically typed — otherwise it means "Pyright could not tell", and it reads exactly like "no
callers". An agent asking "who calls `set_entity_property`?" would conclude the method is unused,
the opposite of the truth, for a method whose absence on the DC transport crashed a live
reconcile pass. Confirm an empty result with `grep` before you act on it.

## Do not trust the LSP's line numbers — check the working tree

Two distinct effects, both observed:

1. **The reported numbering is not the editor's.** In the run above, every line Serena reported
   was exactly **one less** than the working tree's 1-based number (the import at `27` vs `28`,
   the call at `336` vs `337`, the test sites at `312/316/317` vs `313/317/318`).
2. **The index can lag edits.** During epic `061c` Serena reported the cache gate at
   `runner.py:336` and `test_structured.py:175` while the tree held `:341` and `:176`, after a
   commit in the same session rewrote 79 lines of `runner.py`.

So use Serena to find *which files and which symbols*, then confirm the *line* against the
working tree before you cite or patch it.

## `grep` reports matches the LSP correctly ignores

The failure runs the other way too. During the same investigation `grep` for `output_mode()`
returned two hits in **comments** (`runner.py:78`, `:160`) that Serena did not — because they
are not references. A hand-enumerated `grep` list therefore needs its own filtering pass, which
is a second place to make a mistake.

## Net

| Need | Tool |
|---|---|
| who calls / imports a symbol | Serena `find_referencing_symbols` — semantic, no comment false positives |
| symbol named as a **string** (`monkeypatch.setattr`, `getattr`, `importlib`) | `grep` — the LSP cannot resolve these |
| calls on a **receiver** whose static type is `Any` (unannotated parameter, or explicit `: Any`) | `grep` — Pyright cannot bind the attribute, so Serena returns an empty result, not an error |
| a current line number | `grep` on the working tree — the LSP's numbering is offset and its index can lag |

For a cross-cutting change, the safe sequence is: Serena for the reference set, then one `grep`
for the symbol's **name as a string** to catch the dynamic sites. The second step is cheap and
it is the one that was skipped when this bit us.
