# rebar documentation

Use this page to select documentation by task or audience. Rebar provides a Python library, the `rebar` CLI, and the `rebar-mcp` MCP server over one git-backed event-sourced ticket store. New clients should begin with the [user guide](user-guide.md).

## Common routes

- **Use the CLI.** Read the [user guide](user-guide.md), then follow [your first change](your-first-change.md) when contributing to this repository.
- **Use MCP.** Read the [MCP reference](mcp-reference.md) for the tool inventory and the [LLM framework](llm-framework.md) for agent operations and gates.
- **Adopt rebar in another project.** Copy and adapt the provider-neutral [AGENTS.md template](../templates/AGENTS.md).
- **Contribute to rebar.** Follow [your first change](your-first-change.md), prepare the [local development environment](local-dev-env.md), and use the review and landing process in [CONTRIBUTING.md](../CONTRIBUTING.md).
- **Study the implementation.** Begin with the [architecture](architecture.md), [event schema](event-schema.md), and [concurrency model](concurrency.md).

## Troubleshooting

| Symptom | Meaning and remedy |
|---|---|
| `claim` or `transition` exits with code `10` and `ConcurrencyError` | Another writer moved the ticket under the optimistic concurrency model. Read the ticket again and choose another. Do not force the operation. See the [concurrency model](concurrency.md). |
| `rebar-mcp` is not found | The MCP server ships in an optional extra. Run `pipx install nava-rebar[mcp]` or `uvx --from nava-rebar[mcp] rebar-mcp`. See the [configuration design](config.md). |
| `unknown key '…' ignored (typo?)` appears while developing rebar | A global installation is shadowing the checkout. Activate the worktree virtual environment so `rebar` resolves to the checkout build. See the [local development environment](local-dev-env.md). |

## Documentation ownership

The [documentation policy](documentation-policy.md) defines roles, audiences, lifecycles, canonical sources, correction methods, and writing guidance. The rules are authoring guidance. They do not define a mechanical prose gate.

The [generated artifact catalog](generated-artifacts.md) identifies derived and parity-gated files with their sources, regeneration commands, and enforcement gates.

## Documentation directory

Each maintained top-level Markdown page appears once in this directory. A page may serve more than one audience. Its primary route determines its placement.

### Clients

These pages explain supported use of rebar without requiring knowledge of its implementation.

- **[README.md](README.md)**. This documentation landing page.
- **[api-stability.md](api-stability.md)**. Stability expectations for each public surface during the `0.x` series.
- **[cli-reference.md](cli-reference.md)**. Generated command and option reference for the `rebar` CLI.
- **[clone-guidance.md](clone-guidance.md)**. Supported space-efficient checkout methods and their tradeoffs.
- **[commit-ticket-trailer.md](commit-ticket-trailer.md)**. Commit trailer requirements for projects that require ticket references.
- **[config-reference.md](config-reference.md)**. Generated reference for configuration keys, defaults, aliases, and lifecycle states.
- **[env-vars.md](env-vars.md)**. Generated inventory of environment variables read by rebar.
- **[exit-codes.md](exit-codes.md)**. Process status contract for CLI automation.
- **[identity-setup.md](identity-setup.md)**. Environment setup for attribution, signing, key rotation, and Jira identity mapping.
- **[import-export.md](import-export.md)**. NDJSON import and export procedures and their loss boundaries.
- **[jira-sync-setup.md](jira-sync-setup.md)**. Setup for bidirectional synchronization with Jira.
- **[llm-example-configs.md](llm-example-configs.md)**. Complete configuration examples for supported LLM provider combinations.
- **[manifest-signing.md](manifest-signing.md)**. User-facing attestation signing and verification behavior.
- **[mcp-auth.md](mcp-auth.md)**. OAuth 2.1 resource server authentication for MCP over HTTP.
- **[mcp-client-setup.md](mcp-client-setup.md)**. Wire the copilot, codex, and claude MCP clients to the remote rebar MCP endpoint with a static bearer PAT.
- **[mcp-reference.md](mcp-reference.md)**. Generated MCP tool inventory and schema reference.
- **[output-schemas.md](output-schemas.md)**. Machine-readable output contracts for CLI operations.
- **[s3-backend.md](s3-backend.md)**. Optional S3 ticket store setup and operating constraints.
- **[scale-envelope.md](scale-envelope.md)**. Measured store sizes and supported operating expectations.
- **[security.md](security.md)**. Generated reference for credential projection and child process environments.
- **[session-id-shims.md](session-id-shims.md)**. Session provenance capture for supported coding agents.
- **[ticket-model.md](ticket-model.md)**. Ticket status, hierarchy, links, tags, and lifecycle concepts.
- **[user-guide.md](user-guide.md)**. Task-oriented guide for routine ticket work.
- **[writing-a-passing-plan.md](writing-a-passing-plan.md)**. Entry point for plans that must pass the plan-review gate.

### Contributors

These pages explain the development process, maintenance contracts, and contributor tools.

- **[bug-creation-contract.md](bug-creation-contract.md)**. Requirements for automated bug creation and deduplication.
- **[chatgpt-agent-guide.md](chatgpt-agent-guide.md)**. Workflow for connector-limited sessions without a checkout or tracker.
- **[code-navigation.md](code-navigation.md)**. Semantic and text search responsibilities for code navigation.
- **[code-review-fp-ledger.md](code-review-fp-ledger.md)**. Process for preserving confirmed code-review false positives as evaluation cases.
- **[coverage.md](coverage.md)**. Coverage baseline and measurement procedure.
- **[documentation-policy.md](documentation-policy.md)**. Documentation roles, correction methods, protected forms, and writing guidance.
- **[generated-artifacts.md](generated-artifacts.md)**. Ownership, regeneration, and enforcement catalog for derived and parity-gated files.
- **[jira-fixtures.md](jira-fixtures.md)**. Jira fixture capture, replay, and maintenance procedure.
- **[local-dev-env.md](local-dev-env.md)**. Checkout-specific environment setup for development commands and gates.
- **[mutation-testing.md](mutation-testing.md)**. Mutation testing workflow and interpretation.
- **[passing-code-review.md](passing-code-review.md)**. Entry point for preparing and previewing a Gerrit code review.
- **[plan-review-criteria-guide.md](plan-review-criteria-guide.md)**. Generated registry of plan-review criteria and detection detail.
- **[reuse-surface.md](reuse-surface.md)**. Library API and reusable subsystem reference.
- **[review-policy.md](review-policy.md)**. Meaning of the Gerrit `LLM-Review` and `Verified` votes.
- **[workflow-authoring-v2.md](workflow-authoring-v2.md)**. Contract for authoring prompts and workflow steps.
- **[workflow-editor.md](workflow-editor.md)**. Visual workflow editor usage and constraints.
- **[your-first-change.md](your-first-change.md)**. Contributor walkthrough from setup through Gerrit submission.

### Architecture

These pages explain implementation boundaries, invariants, and subsystem contracts.

- **[architecture.md](architecture.md)**. Top-level design of the store, library, CLI, and MCP facades.
- **[attest-substrate.md](attest-substrate.md)**. Signing substrate, envelope format, and signature scheme registry.
- **[concurrency.md](concurrency.md)**. Optimistic concurrency, convergent updates, and shared ticket writes.
- **[config.md](config.md)**. Configuration design of record and precedence model.
- **[event-schema.md](event-schema.md)**. Append-only event format and ticket state reduction.
- **[grounding.md](grounding.md)**. Evidence oracle used to ground review findings in repository content.
- **[identity.md](identity.md)**. Identity, attribution, authorship, and key lifecycle model.
- **[llm-framework.md](llm-framework.md)**. LLM operation framework, execution seams, and structured findings.
- **[managed-refs.md](managed-refs.md)**. Provenance model for references synchronized across systems.
- **[mapping-seam.md](mapping-seam.md)**. Provider-neutral reconciler mapping seam: config-driven axes, resolvers, and the `suggest-mapping` probe.
- **[migrations.md](migrations.md)**. Idempotent ensure registry and migration lifecycle.
- **[plan-review-gate.md](plan-review-gate.md)**. Plan-review gate semantics, attestations, and invalidation rules.
- **[repo-snapshot-gates.md](repo-snapshot-gates.md)**. Repository snapshot isolation for code-reading gates.
- **[review-kernel.md](review-kernel.md)**. Shared multi-pass review framework.
- **[workflow-engine.md](workflow-engine.md)**. Synchronous workflow interpreter and intended extension surface.

Focused records cover the [batch runner seam](design/batch-runner-seam.md) and the [HMAC operation certificate removal](migrations/hmac-opcert-removal.md). The [sample ticket log](sample-ticket-log.jsonl) illustrates the event format.

### Architecture Decision Records

Browse the generated [ADR index](adr/README.md) for numbered decisions and status information. ADRs preserve decisions that establish architectural invariants and the rationale behind them.

### Operations

These pages support deployment, release, incident response, and recurring maintenance.

- **[ci-provider-matrix.md](ci-provider-matrix.md)**. Credentialed provider coverage and test cadence for LLM integrations.
- **[comment-echo-reclaim-runbook.md](comment-echo-reclaim-runbook.md)**. Preparation, quiet-window publication, and rollback protocol for the one-time reconciler comment-echo history reclaim.
- **[dependency-advisory-runbook.md](dependency-advisory-runbook.md)**. Response procedure for dependency vulnerability findings.
- **[gerrit-aws-setup.md](gerrit-aws-setup.md)**. Deployment of Gerrit and the review bot on AWS.
- **[jira-dc-capability-map.md](jira-dc-capability-map.md)**. Measured Jira Data Center behavior and regeneration workflow.
- **[maintenance-audit-runbook.md](maintenance-audit-runbook.md)**. Repeatable principal-engineer maintenance audit.
- **[orphaned-processes.md](orphaned-processes.md)**. Prevention, detection, and recovery procedure for orphaned helper processes.
- **[releasing.md](releasing.md)**. Release procedure for PyPI, Homebrew, and the MCP Registry.

The generated [security reference](security.md) also provides credential and process environment information for operators.

### Research

These pages preserve analyses that inform future work without defining current behavior.

- **[oss-comparison-and-remediation.md](oss-comparison-and-remediation.md)**. Comparison with open source ticket systems and a prioritized remediation strategy.
- **[remediation-implementation-plan.md](remediation-implementation-plan.md)**. Implementation companion for the open source comparison.

Additional research includes the [task decomposition survey](research/task-decomposition-sota-2026.md) and the [experiment index](experiments/README.md). Detailed experiment records cover the [plan-review gate](experiments/plan-review-gate/README.md), [code grounding](experiments/code-grounding-spike/README.md), and [workflow remediation prototypes](experiments/workflow-remediation-pocs/README.md).

### Historical evidence

These pages preserve completed migrations, contract changes, and validation results. They do not define maintained guidance.

- **[88ab-feature-branch-evidence.md](88ab-feature-branch-evidence.md)**. Validation record for the Gerrit feature branch workflow.
- **[bash-migration.md](bash-migration.md)**. Record of the completed Bash to Python migration.
- **[dco-rollout-evidence.md](dco-rollout-evidence.md)**. End-to-end validation record for DCO enforcement.
- **[release-notes.md](release-notes.md)**. Preserved contract changes for agents and maintainers.
- **[serena-symbol-reference-coaching-sample.md](serena-symbol-reference-coaching-sample.md)**. Recorded plan-review runs for symbol reference coaching.

The [archive index](archive/README.md) covers completed planning and handoff records. Frozen plan-review corpora are indexed under [recorded runs](experiments/plan-review-gate/runs/README.md). Calibration evidence includes the [trust boundary](calibration/T5c_trust_boundary.md), [completion floor](calibration/completion_floor.md), [overlap batch confidence](calibration/overlap_batch_confidence.md), and [plan kind sets](calibration/plan_v5_kind_sets.md). Third-party license texts are preserved under [licenses](licenses/).
