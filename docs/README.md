# rebar documentation

This is the index to rebar's `docs/` tree, grouped by **who you are and what you're
trying to do**. rebar wears three hats — a Python library, a `rebar` CLI, and a
`rebar-mcp` MCP server — over one git-backed, event-sourced ticket store, so the
docs span a wide range. Rather than an alphabetical dump, the pages below are sorted
into four audiences:

- **User** — you drive tickets day to day through the CLI.
- **Operator** — you run, configure, deploy, and release rebar (and its Jira sync).
- **Contributor** — you develop rebar itself.
- **Agent** — you operate rebar's LLM-agent surfaces, or need the gate / workflow
  internals behind them.

A page can matter to more than one audience; it is filed under the audience most
likely to reach for it first. If you're brand new, start with the
[user guide](user-guide.md).

## User

Day-to-day use of rebar through the CLI.

- **[user-guide.md](user-guide.md)** — the practical, human-facing guide to using
  rebar from the command line: create/claim/comment/link/transition tickets, search
  and list, the `idea` parking lot, session logs, and the quality gates as you
  experience them. Start here.
- **[README.md](README.md)** — this index of the `docs/` tree.
- **[import-export.md](import-export.md)** — moving tickets in and out of the store
  as NDJSON with `rebar export` / `rebar import` (a lossy interop projection, not a
  backup).

## Operator

Configuring, deploying, syncing, and releasing rebar.

- **[config.md](config.md)** — rebar's configuration surface: the `.rebar/config.conf`
  keys, precedence, and the design of record behind them.
- **[env-vars.md](env-vars.md)** — the generated registry of every `REBAR_*` (and other)
  environment variable read under `src/rebar`, with its reading module and alias status
  (kept in sync by a CI drift gate).
- **[jira-sync-setup.md](jira-sync-setup.md)** — automating the rebar ⇄ Jira
  reconciler in GitHub Actions so a project can stand up bidirectional sync.
- **[gerrit-aws-setup.md](gerrit-aws-setup.md)** — the optional/advanced deployment
  of a self-hosted Gerrit + rebar review-bot to LLM-gate every commit to a GitHub
  repo's `main`.
- **[managed-refs.md](managed-refs.md)** — the managed-reference provenance gate that
  lets a local removal of a cross-system reference propagate to a peer (e.g. Jira)
  without being resurrected on the next inbound pass.
- **[commit-ticket-trailer.md](commit-ticket-trailer.md)** — requiring every commit to
  `main` to reference a resolving rebar ticket, enforced in the CI Verified gate.
- **[scale-envelope.md](scale-envelope.md)** — how large a rebar store can comfortably
  get, with representative measured numbers.
- **[releasing.md](releasing.md)** — the runbook for cutting a release across PyPI,
  Homebrew, and the MCP Registry.
- **[release-notes.md](release-notes.md)** — agent-visible contract changes, newest
  first (rebar shares one `origin/tickets` across many clients).

## Contributor

Developing rebar itself — architecture, internals, and the dev workflow.

- **[your-first-change.md](your-first-change.md)** — **start here if you're new:** a
  warm, start-to-finish walkthrough of getting your first change reviewed and landed
  through Gerrit. ([CONTRIBUTING.md](../CONTRIBUTING.md) is the full reference.)
- **[architecture.md](architecture.md)** — the top-level design: event-sourced store,
  the three facades (library / CLI / MCP), and how they fit together.
- **[event-schema.md](event-schema.md)** — the append-only JSON event files and the
  reducer that replays them into ticket state.
- **[concurrency.md](concurrency.md)** — rebar's concurrency model: the structural
  invariants (optimistic concurrency, convergent deltas) that make concurrent
  operation safe without locks-in-the-large.
- **[migrations.md](migrations.md)** — the idempotent ensure-registry (School B,
  desired-state): how to add an ensure unit, where `run_ensures` runs, the applied-set
  marker + write-path pending-hint, the accepted trade-offs, and the future A-tier ledger.
- **[api-stability.md](api-stability.md)** — the 0.x stability promise per surface, so
  you know what you can depend on today and how changes are communicated.
- **[local-dev-env.md](local-dev-env.md)** — running the **repo checkout's** rebar (not
  a stale global build) when developing or running the gates.
- **[coverage.md](coverage.md)** — the line/branch coverage baseline and how it's
  measured.
- **[mutation-testing.md](mutation-testing.md)** — measuring whether the test suite
  actually constrains behavior, via mutmut.
- **[maintenance-audit-runbook.md](maintenance-audit-runbook.md)** — the repeatable
  recipe for the periodic principal-engineer code-health audit.
- **[jira-fixtures.md](jira-fixtures.md)** — the hermetic-but-honest Jira test fixtures
  and why hand-built snapshot dicts caused a bug class.
- **[bash-migration.md](bash-migration.md)** — the historical record of the completed
  bash→Python strangler-fig migration.
- **[oss-comparison-and-remediation.md](oss-comparison-and-remediation.md)** — rebar
  vs. OSS ticket systems: gaps, gotchas, and a prioritized remediation strategy.
- **[remediation-implementation-plan.md](remediation-implementation-plan.md)** — the
  detailed how-to companion to the OSS comparison (seams, schema impact, test plans).
- **[reuse-surface.md](reuse-surface.md)** — the developer API reference for the
  reusable subsystems (signing, LLM runtime, prompt/contract, output-schema seams).
- **[88ab-feature-branch-evidence.md](88ab-feature-branch-evidence.md)** — durable
  live-validation evidence for the epic-88ab Gerrit feature-branch flow.

## Agent

The LLM-agent operations and the gate / workflow machinery behind them.

- **[llm-framework.md](llm-framework.md)** — the `rebar.llm` framework for tool-using
  LLM agents that emit structured findings (review, verify-completion, and the seams
  to add more).
- **[plan-review-gate.md](plan-review-gate.md)** — the plan-review gate that runs when
  work **starts** (on entry to `in_progress`), and its attestation model.
- **[plan-review-criteria-guide.md](plan-review-criteria-guide.md)** — the
  registry-generated reference of every plan-review criterion (one section per
  criterion; `rebar explain <id>` prints one).
- **[review-kernel.md](review-kernel.md)** — the shared four-pass review framework
  (finder → … ) behind rebar's multi-pass LLM reviews.
- **[code-review-fp-ledger.md](code-review-fp-ledger.md)** — recording confirmed
  code-review false positives as tickets that become NO-FIRE eval cases.
- **[grounding.md](grounding.md)** — the code-grounding oracle: a pure evidence oracle
  that grounds review findings in the actual code (it never decides block/advisory).
- **[repo-snapshot-gates.md](repo-snapshot-gates.md)** — repo-snapshot isolation for
  the code-reading gates (review-plan / verify-completion / review / review-code /
  scan-spec).
- **[manifest-signing.md](manifest-signing.md)** — the HMAC attestation on a ticket:
  a signed manifest of verified steps as machine-checkable proof a gate ran.
- **[output-schemas.md](output-schemas.md)** — rebar's machine-readable output
  contract: one canonical flag, every JSON shape pinned by a validated JSON Schema.
- **[exit-codes.md](exit-codes.md)** — the CLI process-status contract the
  parallel-agent workflow keys off (e.g. a lost claim race is exit 10).
- **[workflow-engine.md](workflow-engine.md)** — the workflow engine's intended use:
  the synchronous interpreter over YAML workflows that is the substrate for the LLM
  gates.
- **[workflow-authoring-v2.md](workflow-authoring-v2.md)** — authoring
  contract-bearing prompts and steps (prompt front-matter, closed key set,
  execution_mode, the CI drift gate).
- **[workflow-editor.md](workflow-editor.md)** — the visual workflow editor
  (`rebar workflow edit`) for authoring workflows.

## Subdirectories

- **[adr/](adr/)** — Architecture Decision Records. One file per decision, numbered
  `NNNN-<slug>.md` starting at `0001` (a few numbers are shared across parallel
  workstreams). Browse the directory for the full set rather than expecting them
  listed here.
- **[design/](design/)** — focused design notes for individual seams (e.g. the
  batch-runner seam).
- **[calibration/](calibration/)** — calibration notes for the LLM gates (completion
  floor, trust-boundary tuning).
- **[experiments/](experiments/)** — reproducible prototypes and analysis backing the
  remediation and plan-review work (scripts + markdown; see its own `README.md`).
- **[archive/](archive/)** — completed, historical planning/handoff documents kept for
  provenance; not living docs (see its own `README.md`).
- **[licenses/](licenses/)** — third-party license texts bundled with rebar.

## Reference data

- **[sample-ticket-log.jsonl](sample-ticket-log.jsonl)** — a small sample of the
  append-only event log (one JSON event per line) that backs a ticket, for reference
  when reading [event-schema.md](event-schema.md).
