# ADR 0030 — Code-grounding T2: one-shot compiler-CLI diagnostics as the v1 semantic-resolution backend

**Status:** Accepted (epic 850f — loft-teeth-zip / story S1 — avid-tile-beard)
**Date:** 2026-07-03
**Builds on:** epic 8f6c (the T0+T1 grounding floor + the abstain-by-default T2 seam)

## Context

The v1 code-grounding oracle (epic 8f6c) ships a deterministic, polyglot, fail-open **T0
(manifest/registry existence) + T1 (universal-ctags / tree-sitter syntactic)** floor and a **T2
abstain-by-default seam**: `TIER_T2` exists in the closed tier vocabulary but is never emitted, and
member/dotted references abstain inline in `resolve.refute_absence` (tagged "member binding is T2").
Epic 850f fills that seam with a real semantic backend.

Any T2 backend must respect three constraints that are the whole reason T2 was deferred:

- **Not deterministic by nature.** Semantic resolution (does this import resolve / does `.bar` exist
  on this type / does the arity match?) is a function of *machine state* — installed deps, build
  state, a background-indexing server, runtime versions — not just the artifact under review. Every
  failure mode (not-installed / not-built / not-indexed / wrong-version / hung) is indistinguishable
  from "the symbol is genuinely fake."
- **Confirm-only verdict asymmetry.** In this oracle a resolved reference is `refuted` (an asserted
  absence disproved — "present"); the abstention is `abstain` ("unknown"). A T2 `refuted` is
  trustworthy (it located a real definition); an asserted *absence* is suspect unless the environment
  is known-good. So T2 must emit a trustworthy `refuted` and otherwise **abstain** — it can never sit
  on a deterministic blocking path.
- **Cannot be borrowed from the ambient environment.** rebar ships to a broad, diverse client base;
  an externally-configured MCP server (e.g. Serena) or a host toolchain is NOT guaranteed where rebar
  runs (CLI, library, a client's `rebar-mcp`). A T2 backend must be self-contained and opt-in.

Three candidate backends were named in the epic: (1) a bundled LSP-client library, (2) one-shot
compiler/checker-CLI diagnostics, (3) containerized/hermetic resolution.

## Decision

### 1. The v1 backend is one-shot compiler-CLI diagnostics (pyright / Python first)

The v1 T2 backend runs a checker CLI once and parses its structured diagnostics — starting with
`pyright --outputjson` for Python. Rationale over the alternatives:

- **No long-lived server** → simplest to fail open and to batch (one invocation per review, reused
  across references), versus the LSP path's per-language server lifecycle + project-config surface.
- **No ambient dependency** → the checker is an opt-in extra / `PATH` probe, honouring the
  self-contained constraint.
- **Trustworthy `refuted`** → when the checker runs and the file's imports resolve, the absence of a
  diagnostic at a reference is a real semantic confirmation.

**The LSP-client-library (candidate 1) and containerized/hermetic (candidate 3) tiers are deferred**,
behind the *same* seam this epic establishes:

- *LSP-client library* multiplies the per-language server + runtime-prereq + project-config surface
  (an allowlist of ~12–60 languages, each fail-soft), which is a large, separate build.
- *Containerized/hermetic* is the high-determinism path (build the declared dev/build env and resolve
  inside it, so `absent` could graduate from suspect to trustworthy), but its **gating cost is
  security**: building an untrusted client repo executes arbitrary code (Dockerfile `RUN`,
  devcontainer `postCreateCommand`) — the bar is ephemeral single-tenant microVMs + locked egress +
  no secrets in the build env. It also abstains a lot (many repos have no buildable declared env). It
  is worth doing later, not first.

### 2. Confirm-only mapping (locked)

Run the checker over the reference's file (via the project root, so cross-module imports resolve).
For a reference in file `F`:

- **`refuted` at `TIER_T2`** iff: the checker ran and produced parseable output, **AND** `F` has
  **zero** import-resolution diagnostics (`reportMissingImports` / `reportMissingModuleSource` — the
  "environment built" precondition), **AND** no diagnostic in `F` concerns the reference (an
  unresolved-kind rule — `reportAttributeAccessIssue` / `reportUndefinedVariable` / unresolved-import
  — whose message names the reference's leaf `name`).
- **`abstain` at `TIER_T2`** (with a CLOSED structured reason) in every other case: tool absent
  (`no_tool`), non-supported language (`unsupported_lang`), no locatable file (`ambiguous`),
  unparseable output (`parse_error`), `timeout`, crash / env-not-built / a diagnostic sitting at the
  reference (`other`, with detail). A diagnostic saying the reference does *not* resolve is a
  suspected-absent — it becomes an `abstain`, **never** an asserted absence.

Because `validate_reference` carries no line/span, "a diagnostic concerns the reference" is matched by
file + unresolved-rule-kind + the leaf `name` appearing in the diagnostic message. Any diagnostic rule
the mapping does not recognize routes to `abstain` (fail-safe — never `refuted`).

### 3. The seam is a dispatch function, not a plugin registry (Rule-of-Three)

With exactly one concrete backend shipping, a general `T2Backend` Protocol + plugin registry would be
speculative abstraction. The seam (story S2) is a single module `src/rebar/grounding/semantic.py`
exposing `refute_semantic(reference, *, repo_root, config, timeout, cache) -> dict | None`,
`available_backends()`, and a closed `T2_BACKENDS` name tuple. A future backend (LSP, containerized)
adds a name to `T2_BACKENDS` and a branch to `refute_semantic` — the Protocol is introduced only when a
third real call-site justifies it.

Escalation is owned by **`oracle.refute_absence`** (the single consumer entry): after the T1/deps lane
returns, an `abstain` on a member/dotted reference or a not-found bare symbol is escalated to
`refute_semantic`; a `refuted@T2` replaces it, otherwise the T1 record is returned unchanged. The
deterministic `resolve.refute_absence` (T1) lane is **not** modified — T2 stays off the T1 path.

### 4. Consumers are unchanged; opt-in + default-off

T2 records reuse the existing three-valued contract (`refuted` / `match` / `abstain`) and the closed
vocabularies (`TIER_T2` already exists), and every consumer read passes through
`evidence.normalize_evidence`, so the DET-floor (`5fd2`) and reviewer (`9da1`) consumers need no edit.
The backend is gated behind the `grounding-t2` optional extra plus three default-off
`.rebar/grounding.toml` keys (`t2_enabled=false`, `t2_backend`, `t2_timeout_seconds`). With
`t2_enabled=false` the oracle is byte-identical to pre-epic behaviour.

## Consequences / back-out

- **Additive + inert by default.** With the extra uninstalled or `t2_enabled=false`, no T2 code path
  runs and the oracle behaves exactly as the 8f6c floor did — a regression test pins this.
- **Back-out** = set `t2_enabled=false` (config) or drop the `grounding-t2` extra; the seam module and
  the `oracle` escalation are then dead code with no behavioural effect and can be reverted
  independently of the contract.
- **IaC grounding is out of scope here.** Terraform is not a code-build environment (it declares cloud
  infrastructure, provides no language server / deps), so its own T0/T1/T2 tiering (`terraform
  validate` = IaC T1, Registry provider/module existence = IaC T0, `terraform plan` = IaC T2) is
  split into a **sibling epic**, not folded into 850f. The T0/T1/T2 tiering recurs per domain; IaC is
  a distinct lane.
- **Future backends** (LSP-client library, containerized/hermetic) plug into the same seam. The
  containerized path, when built, is the one that can let a T2 `absent` become trustworthy — under an
  egress-locked single-tenant microVM sandbox executing only the *declared dev/build* artifact
  (devcontainer / compose-dev / named build stage / Nix shell), never a production image.
