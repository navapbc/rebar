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

The editor front-end (bpmn-js + properties panel) is **built ahead of time and vendored
into the wheel** as a single self-contained bundle. So to *use* the editor you need only:

- a `pip install nava-rebar` (base — no extra), and
- a web browser.

There is **no CDN dependency and no Node/npm at runtime**: the Python side is stdlib
(`http.server`), and the bundle is served locally from package data. Authoring a workflow
visually therefore needs nothing beyond the base install. Node/npm are needed **only by a
rebar developer who is rebuilding the bundle or running the faithful E2E tier** (below).

## What the editor gives you

- **A readable layout.** The Python serializer emits a layered left-to-right layout: rank
  by longest path → x; same-rank siblings stacked → y, so parallel steps never collide;
  edge waypoints dock to node edges (not centre-to-centre, so arrows don't cut through
  labels). Nested constructs (`loop` / `map` / `branch` arms) are emitted as **expanded
  sub-processes** sized to contain their bodies, so the nesting shows **inline on the
  canvas** rather than as a collapsed drill-down box.
  ([`bpmn-auto-layout`](https://github.com/bpmn-io/bpmn-auto-layout) was evaluated and
  rejected: for our coordinate-free, start/end-event-free, nested IR it stacks nodes in a
  single column and emits no edges.)
- **Visible flow semantics.** A **start event** points at the workflow's root step(s) and
  the terminal step(s) point at an **end event**, so the entry/exit points are obvious. A
  `branch` exclusiveGateway is **labelled with its `when` condition**, and its two outgoing
  flows are labelled **`then (true)`** / **`else (false)`**, so the decision and which path
  it takes are on the canvas. A step with more than one unconditional outgoing flow (e.g.
  `fetch → {commits, graph}`) is a **parallel fan-out** — those successors run concurrently
  (standard BPMN uncontrolled split); only a gateway introduces a conditional choice.
- **A properties panel** ([`bpmn-js-properties-panel`](https://github.com/bpmn-io/bpmn-js-properties-panel)
  + a small custom *Rebar* provider): select any step to see its **kind** (scripted /
  agent / batch / branch / loop / map), its **action** (`uses` / `prompt`, for scripted/agent
  steps), the resolved **prompt text** (read-only, for agent steps — so you can see what
  the agent runs), the **condition** (for branches), the **`if:` overlay predicate** (for
  scripted/agent/batch steps — the step is included only when it is truthy), and its **rebar
  config** — the `<rebar:Config>` JSON that carries `with` / `mode` / `model` / loop bounds /
  the branch condition — edited through **structured per-field entries** (no raw JSON). (The
  config is the exact payload the Python round-trip reads back, so what you edit is what gets
  written.) Panel groups are **collapsed by default** — click a group header (e.g. *Rebar*)
  to expand it.
- **The v3 `batch` step.** A batch step (budgeted fan-out of a finder prompt over an authored
  **criteria** list) is a `bpmn:serviceTask` told apart from an agent step by its config. The
  *Rebar* panel shows its **finder** (`prompt`), **`usd_budget`**, and **`model_ladder`**; a
  separate **Batch criteria** list group lets you **add / remove / edit** each criterion (a
  prompt-library id + an optional `when` overlay predicate) with the stock list ＋/✕ controls.
  A `ServiceTask kind` toggle converts a step between **agent** and **batch** (seeding/dropping
  its `batch` config), so a batch can be authored from a freshly-drawn Service Task.
- **Constrained editing.** The palette is BPMN-only, so a shape that can't map back to the
  IR can't be drawn; an un-mappable edit is **rejected on Save** with located errors and
  the file is left untouched. A newly-drawn plain task maps to a **scripted** step by
  default (so your node is never lost); to make it an agent step use bpmn-js's change-type
  menu (the wrench on the context pad) → Service Task, then set its action + config in the
  *Rebar* panel group.

## How it maps to the IR (the round-trip)

`rebar.llm.workflow.bpmn` is a lossless IR ↔ BPMN 2.0 serializer:

| IR construct | BPMN element |
|---|---|
| scripted (`uses`) | `bpmn:scriptTask` |
| agent (`prompt`) | `bpmn:serviceTask` + `<rebar:Agent>` |
| `batch` (v3: finder + criteria) | `bpmn:serviceTask` + `<rebar:Batch>` (batch dict in `<rebar:Config>`) |
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

Three tiers cover the editor, escalating in fidelity:

- **Offline unit tests** (`tests/unit/workflow/test_bpmn.py`, `test_editor.py`) — the
  IR↔BPMN serializer, the generated DI layout (no overlaps, edges present, sub-processes
  expanded with children contained), the host page contract, the save round-trip + security
  guards, and invariants like "no emitted id is an illegal NCName." Always run.
- **Faithful serialization E2E** (`tests/e2e/test_editor_e2e.py`) — round-trips BPMN
  through the **real `bpmn-moddle`** (the editor's read/write layer) via a small Node
  harness (`tests/e2e/js/roundtrip.mjs`), so the contract is checked against the same code
  the browser runs — not the permissive `xml.etree` the unit tests use. This is what
  catches *faithfulness* bugs (e.g. an id the real parser rejects but `xml.etree` keeps).
- **Real-browser E2E** (`tests/e2e/test_editor_browser.py`) — runs the ACTUAL bundle in
  headless Chromium ([Playwright](https://playwright.dev)) against a live editor server and
  asserts the runtime behavior nothing else can: edges render, shapes don't overlap, the
  properties panel reacts to selection, and an edit made in the panel **persists to the IR
  on Save**. This tier exists because editor changes that merely syntax-checked once shipped
  broken at render time — the browser is the only faithful oracle for the bundle.

All E2E tiers are **opt-in and self-skipping**: they need Node + a one-time `npm install`
(and the browser tier a Chromium download), and skip with a clear reason when those are
unavailable, so the always-on Python unit suite is unaffected.

The IR↔BPMN round-trip was de-risked up front by
[`visual_bpmn_roundtrip_poc.mjs`](experiments/workflow-remediation-pocs/visual_bpmn_roundtrip_poc.mjs)
(see the [de-risk POC index](experiments/workflow-remediation-pocs/README.md)).

See also [docs/llm-framework.md](llm-framework.md) (the agent/runner side of the workflow
engine) and the agent-facing tool guide in the repo-root `AGENTS.md`.
