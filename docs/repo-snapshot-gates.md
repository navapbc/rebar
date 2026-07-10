# Repo-snapshot isolation for the code-reading gates

The rebar LLM **code-reading gates** — `review_plan`, `verify_completion`, `review_ticket`,
`review_code`, `scan_spec` (CLI: `rebar review-plan` / `verify-completion` / `review` /
`review-code` / `scan-spec`; the same five MCP tools) — read PROJECT SOURCE CODE. The MCP
server is a long-lived process pinned to ONE working directory, so reading that mutable,
shared checkout meant a gate read whatever branch + uncommitted edits happened to be present
at call time. That produced a **false-negative completion verdict** when a parallel task
switched the shared checkout, and an HMAC-signed verdict computed against a moving branch is
not reproducible.

Every code-reading op now takes a client-chosen **`ref`** and a **`source`** mode and reads a
*faithful, immutable, reproducible* view of the repository — never the server's mutable
checkout. Design grounded against Gitaly, Sourcegraph gitserver/zoekt, the GitHub tarball API,
Bazel, ccache, Nix, and in-toto. Architecture: [adr/0005-snapshot-cache-architecture.md](adr/0005-snapshot-cache-architecture.md);
drift/ref coherence: [adr/0002-code-drift-invalidation.md](adr/0002-code-drift-invalidation.md).

## `ref` + `source` semantics

| Control | Values | Default | Meaning |
|---|---|---|---|
| `--ref` / `ref` | a branch, tag, or full SHA | `origin/main` | the code version the gate verifies |
| `--source` / `source` | `attested` \| `local` | `attested` | how that code is read |

- **`attested`** (default) — resolve `ref` to an immutable SHA (fetching `origin` first so a
  moving branch/tag is current), materialize a faithful snapshot of the committed tree at that
  SHA, and run the gate against it. The verdict is **reproducible** and **branch-independent**
  (identical regardless of the server's checked-out branch), and it is **SIGNED**: the pinned
  SHA is recorded as `verified_at_sha` and bound into the signature (see *HMAC trust model*).
- **`local`** (opt-in) — read the server's in-place checkout directly (uncommitted/dirty
  content allowed). It is **NEVER signed** (no `verified_at_sha`, no attestation) and is the
  documented **back-out** that restores the prior "read the local checkout" behavior — e.g. a
  single developer's *verify-before-push* flow. The `rebar transition … closed` CLI close gate
  verifies an attested snapshot of `HEAD` (the committed state about to be pushed; `HEAD`
  resolves from the local object DB, so the close does NOT fetch); the MCP `verify_completion`
  tool defaults to attested `origin/main` (distributed verification of merged code).

**Defaults are configurable, not hardcoded.** `ref`/`source` resolve through the standard
precedence: `REBAR_GATE_REF` / `REBAR_GATE_SOURCE` env > the `[snapshot]` config table
(`ref` / `source`) > the built-in `origin/main` / `attested`. (`review-code` defaults its
`ref` to the reviewed `head` rather than `origin/main`, so its file context matches the diff.)

### Reviewing code that is committed but not yet landed — use `--ref HEAD`

The default `origin/main` ref verifies **merged** code, which is what you want for a change
that stands alone. But when a review **depends on code you have committed locally but not yet
landed on `origin/main`** — a stacked change built atop other un-merged commits, or work on a
feature branch — the default ref materializes a snapshot that **predates** your local commits.
The gate then reads source that lacks the symbols those commits add and reports them as
**`<symbol> does not exist` false findings** (a plan that references a function only present in
an earlier commit of your own stack looks unimplementable).

**Fix: pass `--ref HEAD`** (e.g. `rebar review-plan <id> --ref HEAD`). `HEAD` resolves from the
local object DB — no fetch — so the snapshot includes your committed-but-unlanded code and the
findings reflect the tree you are actually building on. The resulting attestation is accepted
by `claim` exactly like an `origin/main` one. Reach for it whenever a gate flags symbols you
know you committed in the same stack; keep the `origin/main` default for standalone changes.

`REBAR_ROOT` only locates the **object DB** to fetch from; in attested mode it does NOT
determine which code is read — the snapshot at `ref` does. This is the cwd/branch decoupling.

## Private-repo fetch credentials + descriptive errors

Attested mode `git fetch`es the verified ref from `origin`, so a server pointed at a
**private** repository needs **read credentials**: a git credential helper, a deploy key, or a
token configured in the server's clone. Fetching an *arbitrary SHA* additionally requires the
remote's `uploadpack.allowReachableSHA1InWant` (else name a containing ref and let it resolve);
the fetch prefers `--filter=blob:none` over deep history transfer.

Failure behavior (attested **fails closed** — it never serves the wrong tree, and never hangs
on a prompt: `GIT_TERMINAL_PROMPT=0`):

- **Missing/invalid credentials** → a descriptive `SnapshotFetchError` naming the remedy
  ("configure a git credential helper, a deploy key, or a token for the server's clone … then
  retry"). `local` mode never fetches, so it still runs.
- **Unresolvable / absent `ref`** → a `SnapshotRefError` telling you to name a valid
  branch/tag/SHA (and the `allowReachableSHA1InWant` prerequisite for a bare SHA).
- **Unreachable object DB at `REBAR_ROOT`** → a clear error; the gate does not fall back to
  some other tree.
- **Invalid `--source`** → rejected at the CLI/tool boundary with the allowed enum
  (`attested` | `local`).

On the CLI these surface as a single `Error: …` line with a non-zero exit; over MCP they are
raised to the caller.

## HMAC trust model + limits (and the in-toto shape)

An attested verdict is signed with **HMAC-SHA256** under the environment's signing key (see
the README "Signing a manifest of verified steps" section). The verified SHA is bound through
the **existing signed manifest channel** as a manifest step — `verified-at-sha:<sha>` — NOT a
new signed-payload field: it enters the signed bytes without changing the canonical payload or
bumping `PAYLOAD_VERSION`, so **no prior signature is invalidated**. `verify_signature` surfaces
`verified_at_sha` (derived from the signed step — the trust anchor — never an unsigned echo).

**Limits (by design):** HMAC gives **integrity + intra-domain authenticity** only — anyone
holding the shared environment key can both produce and verify a signature. It provides **no
non-repudiation, no public verifiability, and no transparency log**. The pin is shaped as an
**in-toto v1 Statement** subject (`{name: <ticket_id>, digest: {sha1: <verified_at_sha>}}` +
`predicateType`) so a future move to a DSSE / asymmetric-key / transparency-log envelope is an
*envelope swap*, not a data-shape rewrite. A ticket **closed without a signature** is the
durable signal that validation was not attested (a `--force-close`, or a `local`-mode verify).

## Adding a new agentic operation (the safeguard)

Anything that runs a TOOL-USING agent (file-reading tools) MUST follow this process — it is
enforced at runtime, not by convention. When the runner wires an agent's read-only file
tools, `rebar.llm.config.assert_gated()` fails closed unless execution is inside a gate
**session** (`gate_source.gate_read_root`, which both the five gates and `run_workflow` enter).
So a new agent op that forgets to route through `gate_source` raises immediately rather than
silently reading the server's mutable checkout.

To add a new agentic operation: resolve a handle with
`gate_source.resolve_gate_handle(ref, source, repo_root)`, run the agent inside
`with gate_source.gate_read_root(handle):`, and re-root any explicit `LLMConfig` with
`gate_source.apply_handle(cfg, handle)`. `run_workflow` does this automatically for workflows
that contain `agent`/`batch` steps. The offline `TestModel` runner (`model_override`) and the
`REBAR_GATE_ALLOW_UNGATED=1` env (audited) are the only exemptions.

## Snapshot store env knobs + disk reclamation

The content-addressed snapshot store lives **outside** the repo so a gate's read-only/no-git
tools never reach it. Tunables (env > `[snapshot]` config > documented default):

| Setting | Env | Default | Meaning |
|---|---|---|---|
| temp root | `REBAR_GATE_TMPDIR` | system temp dir | base of the snapshot store (never a hardcoded `/tmp`); point it at a roomy LOCAL filesystem |
| free-space watermark | `REBAR_GATE_FREE_WATERMARK_BYTES` | 2 GiB | reclaim snapshots when free disk drops below this |
| recency grace | `REBAR_GATE_GRACE_SECONDS` | 120 | never evict an entry used within this window |
| max age (cold-trim) | `REBAR_GATE_MAX_AGE_SECONDS` | 7 days | evict genuinely cold entries regardless of free space |
| integrity reverify period | `REBAR_GATE_REVERIFY_SECONDS` | 0 (off) | re-check entries for corruption every N seconds |
| janitor interval | `REBAR_GATE_JANITOR_INTERVAL_SECONDS` | 300 | background reclamation cadence |

**Disk-cap behavior.** A single background janitor (off the hot path) reclaims under the
free-space watermark using LRU by touch-on-read `mtime`, skipping the grace window, with a
secondary max-age cold-trim. Eviction is `rename`-to-trash **then** `rmtree` (never an in-place
delete of a live entry), so a reader holding an open file keeps reading via POSIX
delete-on-last-close; a new lookup that misses simply re-materializes. The cache is
regenerable — losing it costs only re-materialization.

**EFS / NFS `flock` caveat.** Cross-process coordination (single-flight populate, the GC
interlock) uses `fcntl.flock` with an atomic-`mkdir` fallback. `flock` semantics on networked
filesystems (EFS/NFS) are unreliable; a serverless/shared-volume deployment that places the
snapshot store on EFS/NFS must account for this — tracked on the `alto-fruit-punch` epic
(serverless lifecycle: AWS volume placement + the EFS/NFS flock caveat). For a single-host
server on a local filesystem, `flock` is sound.
