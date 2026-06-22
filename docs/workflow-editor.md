# The workflow visual editor (`rebar workflow edit`)

> **Audience: rebar developers and workflow *authors*.** This is an edit-time
> authoring tool. **rebar *clients* — the agents and humans driving tickets over the
> CLI / MCP — do not need any of this**: they run workflows, they don't draw them, so
> they install nothing extra and never read past this paragraph. Nothing here is on the
> client/runtime path.

`rebar workflow edit <file>` opens a `.rebar/workflows/*.yaml` workflow in a **local,
ephemeral, visual editor** (a constrained [bpmn-js](https://bpmn.io) canvas) so a human
can *see* the flow and *edit* it — steps, branches, loops, maps, dependencies, and each
step's config — then **Save** it straight back to the IR. The diagram is a *view*: the
YAML IR stays the source of truth and no visual artifact is ever committed to git.

```bash
rebar workflow edit .rebar/workflows/code-review.yaml      # opens your browser
rebar workflow edit .rebar/workflows/code-review.yaml --no-open   # prints the URL
```

It serves a loopback-only HTTP server, opens the browser, and round-trips every Save
back to the file (`Ctrl-C` to stop). On Save it writes **only** the IR `.yaml` (plus a
`.yaml.bak` of the prior version) — the BPMN is held in memory and discarded.

## What you need to run it (almost nothing)

The editor front-end (bpmn-js + properties panel + auto-layout) is **built ahead of time
and vendored into the wheel** as a single self-contained bundle. So to *use* the editor
you need only:

- a `pip install nava-rebar` (base — no extra), and
- a web browser.

There is **no CDN dependency and no Node/npm at runtime**: the Python side is stdlib
(`http.server`), and the bundle is served locally from package data. Authoring a workflow
visually therefore needs nothing beyond the base install. Node/npm are needed **only by a
rebar developer who is rebuilding the bundle or running the faithful E2E tier** (below).

## What the editor gives you

- **A readable layout.** On open the diagram is laid out with
  [`bpmn-auto-layout`](https://github.com/bpmn-io/bpmn-auto-layout): a left-to-right flow
  where parallel steps get their own rows and arrows dock to node edges. (The Python
  serializer no longer hand-rolls geometry; any incoming DI is discarded and recomputed.)
- **A properties panel** ([`bpmn-js-properties-panel`](https://github.com/bpmn-io/bpmn-js-properties-panel)
  + a small custom *Rebar* provider): select any step to see its **kind** (scripted /
  agent / branch / loop / map) and its **rebar config** — the `<rebar:Config>` JSON that
  carries `with` / `mode` / `model` / loop bounds / the branch condition — and edit it in
  place. (This is the exact payload the Python round-trip reads back, so what you edit is
  what gets written; structured per-field entries can layer on later without changing the
  contract.)
- **Constrained editing.** The palette is BPMN-only, so a shape that can't map back to the
  IR can't be drawn; an un-mappable edit is **rejected on Save** with located errors and
  the file is left untouched.

> **Known representation choice.** Nested constructs (`loop` / `map` / `branch` arms) map
> to BPMN **sub-processes**, which `bpmn-auto-layout` renders **collapsed** (a box you
> drill into), not expanded inline. The top-level flow stays clean; the body opens on its
> own canvas. Inline expansion is a possible future enhancement.

## How it maps to the IR (the round-trip)

`rebar.llm.workflow.bpmn` is a lossless IR ↔ BPMN 2.0 serializer:

| IR construct | BPMN element |
|---|---|
| scripted (`uses`) | `bpmn:scriptTask` |
| agent (`prompt`) | `bpmn:serviceTask` + `<rebar:Agent>` |
| `branch` | `bpmn:exclusiveGateway` + a `then`/`else` sub-process arm each |
| `loop` | `bpmn:subProcess` + `standardLoopCharacteristics` |
| `map` | `bpmn:subProcess` + `multiInstanceLoopCharacteristics` |
| `needs` | `bpmn:sequenceFlow` |

Structure (id, kind, `needs`, nesting) is read from the BPMN; exact non-structural config
travels in a `<rebar:Config value="…json…">` extension, which survives a real editor save
because the `rebar` **moddle descriptor** is registered (an unregistered extension is
silently stripped by bpmn-io).

**Branch arms** are a subtlety worth knowing: each `then`/`else` arm is a sub-process
stamped with a `<rebar:Config _role=then|else>` marker, and reconstruction recovers the
arms **structurally** — from the gateway→arm sequence flow plus that role marker — rather
than by parsing the arm's id. (Earlier the arm id used `@`, which is a legal XML attribute
but an **illegal BPMN id (NCName)**: the real bpmn-io parser dropped the whole arm on
Save, silently deleting the branch. The id-independent reconstruction is robust to bpmn-js
regenerating ids on edit.)

## Security model (it can write a file in your repo)

The save endpoint can overwrite the workflow file, so it is guarded on three axes:

- **Loopback only** — the server binds `127.0.0.1`; it is never on a public interface.
- **Per-session token** — `/save` requires an `X-Rebar-Token` embedded in the served page;
  a cross-origin page can't read the page to learn it (CSRF defense).
- **Host check** — a loopback `Host` header is required (DNS-rebinding defense).
- **XXE-blocked** — the save parse does not resolve external entities.

## Rebuilding the bundle (developers only)

The bundle sources live in `src/rebar/llm/workflow/editor_assets/` (npm project); the
built output `dist/editor.js` + `dist/editor.css` is **committed and shipped**. Rebuild
after changing the front-end:

```bash
npm --prefix src/rebar/llm/workflow/editor_assets install
npm --prefix src/rebar/llm/workflow/editor_assets run build   # -> dist/editor.{js,css}
```

Commit the regenerated `dist/`. `edit_workflow` fails with a clear hint if the bundle is
absent (a source checkout that hasn't built it).

## Testing (faithful, self-skipping)

Two tiers cover the editor:

- **Offline unit tests** (`tests/unit/workflow/test_bpmn.py`, `test_editor.py`) — the
  IR↔BPMN serializer, the host page contract, the save round-trip + security guards, and
  invariants like "no emitted id is an illegal NCName." Always run.
- **Faithful E2E tier** (`tests/e2e/`) — round-trips BPMN through the **real bpmn-io
  libraries** (`bpmn-moddle` for serialization, `bpmn-auto-layout` for layout) via a small
  Node harness (`tests/e2e/js/`), so the contract is checked against the same code the
  browser runs — not the permissive `xml.etree` the unit tests use. This is what catches
  *faithfulness* bugs (e.g. an id the real parser rejects but `xml.etree` keeps). The tier
  is **opt-in and self-skipping**: it needs Node + a one-time `npm install`, and skips with
  a clear reason when Node is unavailable (so the always-on Python suite is unaffected).

See also [docs/llm-framework.md](llm-framework.md) (the agent/runner side of the workflow
engine) and the agent-facing tool guide in the repo-root `CLAUDE.md`.
