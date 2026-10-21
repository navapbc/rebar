# ADR 0120 — Consolidate the CLI verb surface by hard cutover

**Status:** Accepted
**Date:** 2026-10-20

## Context

On origin/main 2e81cb9767c1 the CLI exposed 72 visible top-level commands (71 once the overlap
epic removed `enrich`). Twenty of them duplicated an operation that already existed on a
golden-path verb, fitted one flag on it, or belonged under one storage-maintenance noun:

- `exists` and `format` duplicated `resolve`;
- `check-ac`, `clarity-check` and `quality-check` were three entry points to one gate family;
- `tag` and `untag` duplicated `edit`'s `--add-tag` / `--remove-tag`;
- `get-file-impact` and `get-verify-commands` each read one field `show` already returns;
- `list-descendants` and `session-logs` were filtered reads that `deps` and `list` could express;
- `fsck`, `fsck-recover`, `doctor`, `tracker-maintenance`, `tracker-footprint`, `export`,
  `import`, `compact` and `compact-all` are storage maintenance, two of them carrying a redundant
  `tracker-` prefix and two of them a one-ticket / whole-store pair.

Each spelling carried its own route, parser factory, committed help artifact and exit-code row,
so the surface an agent had to learn grew with every near-duplicate.

The operator ruled on 2026-09-06 (epic toadyish-sulfurous-ovenbird): a hard cutover with no
working aliases; the existing removed-name hint mechanism kept and populated, so a removed
spelling fails loudly and names its replacement; every doc surface accurate; and release notes
listing each old-to-new spelling.

## Decision

Remove the twenty spellings and add two routes, `check` and `store`:

| Removed | Replacement |
| --- | --- |
| `exists` / `format` | `resolve <id>` / `resolve <id> --display[=MODE]` |
| `check-ac` / `clarity-check` / `quality-check` | `check <id> --ac` / `--clarity` / (default) |
| `tag` / `untag` | `edit <id> --add-tag` / `--remove-tag` |
| `get-file-impact` / `get-verify-commands` | `show <id> --field=file_impact` / `--field=verify_commands` |
| `list-descendants` | `deps <id> --descendants` |
| `session-logs` | `list --type=session_log --sort=-created --limit=5` |
| `fsck`, `fsck-recover`, `doctor`, `export`, `import` | `store <same name>` |
| `tracker-maintenance` / `tracker-footprint` | `store maintenance` / `store footprint` |
| `compact <id>` / `compact-all` | `store compact <id>` / `store compact --all` |

1. **Hard cutover.** No removed spelling routes. Each exits `2` through argparse's
   invalid-choice error, then a `hint:` line naming its replacement. The single registry is
   `_help_route._REMOVED_SPELLINGS: dict[str, str]`, which also holds the five older pre-1.0
   bridge aliases. A future removal adds one entry; nothing else needs to change.
2. **Merged verbs take flags, not new nouns.** Each replacement is an existing golden-path verb
   plus at most one flag, so an agent that knows `show`, `list`, `deps`, `edit` and `resolve`
   already knows where the operation lives. `check` is the one new verb: it gathers three gate
   entry points and delegates to their unchanged handlers.
3. **Storage maintenance is one family.** `rebar store <sub>` dispatches through a separate
   member table, `_registry.STORE_ROUTES`. Each member keeps the handler, adapter, init policy
   and mount behaviour it had as a top-level command, because the four policies differ:
   `doctor` initializes read-only unless `--repair`, `fsck-recover` skips initialization under an
   injected tracker, `export` initializes without reconverging, and `footprint` never mounts.
   `store compact` selects the one-ticket arm or the sweep on `--all`.
4. **Only the CLI spelling moves.** Every surviving operation keeps its `--output json` bytes and
   exit codes. Library functions (`rebar.fsck`, `rebar.tag`, `rebar.export_tickets`, …), MCP tool
   names and `OUTPUT_SCHEMAS` keys are unchanged, since those surfaces carry their own stability
   promises (`docs/api-stability.md`) and already name the operation, not the old CLI verb.

Kept flat on purpose: the golden path (`init create claim transition ready list search show`),
the gate verbs (`review-plan verify-completion review-code sign-review explain`), the existing
families (`bridge workflow identity llm config metrics audit`), and the CI- and runbook-invoked
trust verbs (`verify-commit-ticket`, `verify-identity`, `verify-opcert`), which a shipped merge
gate calls by name. `reclaim-collapse` belongs to the ADR 0106 work and is not moved.

## Consequences

- The visible top-level surface drops from 71 to 53 commands.
- A script or CI job that used a removed spelling fails with exit 2 and a hint naming the
  replacement, rather than silently doing something else; `.github/workflows/compact-sweep.yml`
  moved to `rebar store compact --all` in the same change that removed `compact-all`.
- `docs/api-stability.md`'s default removal window (keep a deprecated CLI name working for one
  minor release) does not apply to these twenty spellings. Like ADR 0116's config aliases, this
  is an operator-approved pre-1.0 exception, and it does not license removing a library, MCP,
  JSON-schema or event-schema name.
- Member help under `rebar store <sub> --help` is rendered from each member's parser factory;
  only the family listing is a committed help artifact.

## Alternatives considered

- **Keep the old spellings as working aliases for a release.** Rejected by the operator: few
  clients, and an alias period keeps both spellings in every teaching surface.
- **Mark removed routes `retired=True`.** Rejected: a registry test asserts that no shipped
  route is retired, and a retired route still occupies the route table; the hint registry
  carries the replacement text without a route.
- **Group the trust verbs or the LLM verbs too.** Rejected: several are invoked by name from CI
  and runbooks, and regrouping a subset would split one family across two spellings.
