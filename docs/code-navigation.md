# Code navigation — Serena and `grep` fail in opposite directions

`AGENTS.md` §"Navigating the codebase" carries the rule as a three-row table. This note records
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
| a current line number | `grep` on the working tree — the LSP's numbering is offset and its index can lag |

For a cross-cutting change, the safe sequence is: Serena for the reference set, then one `grep`
for the symbol's **name as a string** to catch the dynamic sites. The second step is cheap and
it is the one that was skipped when this bit us.
