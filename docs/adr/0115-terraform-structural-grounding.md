# ADR 0115 — Terraform structural grounding is in-process, refutation-only hcl2 parsing

**Status:** Accepted (epic `a374-849c-c8f2-4234`, task `forcible-diminished-lamb` /
`08ab-60d2-3082-4b47`; §5 amended by task `depraved-classless-rooster` /
`1c52-5e73-4d61-4124` to record V1 registry metadata probing; §6 amended by task
`schematic-gleesome-ballpython` / `c6ce-4d8d-1c12-47cc` to record optional
terraform-config-inspect corroboration)
**Date:** 2026-09-03

## Context

Plan-review's infra/IaC overlay (`T10`) reasons about Terraform plans from prose alone: it has no
way to check whether an asserted-absent variable, resource, output, or module actually exists in
the repository under review. Every other codebase-grounded criterion (E4, G1G2, A1, G6) can cite a
real source span through the read-only repository tools; `T10` could not, so its findings were
un-groundable and it over-abstained.

This slice gives `T10` a structural grounding tool built on the pinned, pure-Python
`python-hcl2==8.1.3` parser. The design question was the trust boundary: how much of the Terraform
toolchain do we invoke, and what may the tool assert.

## Decision

### 1. Pure in-process hcl2 parsing — no external process, ever

The tool parses `.tf`/`.tf.json` captures with `python-hcl2` **in process**. It NEVER launches
`terraform`, `opentofu`, a provider, `tfparse`, `tflint`, `trivy`, `terraform-ls`, or
`terraform-docs`. The only subprocess is the existing grounding **worker boundary**
(`rebar.grounding.harness.run_in_worker`, 60 s), which runs the pure parse fail-open so a
parser hang or segfault degrades to an abstention rather than crashing the review.

`hcl2`/`lark` are imported **lazily** (only inside the worker, via
`rebar.grounding.terraform_parse`) so `import rebar` and every non-Terraform review stay free of
the HCL parser. The capability is gated behind an OPTIONAL extra (`grounding-terraform`); when it
is absent the tool is `available() == False` and every query returns a closed
`no_tool`/`missing_extra` abstention — never a raise.

### 2. OSS rationale — why `python-hcl2`

`python-hcl2` is Apache-2.0, dependency-light (`lark`), and parses the HCL **structure** we need
(block types, labels, spans) without a provider schema, a network fetch, or a cloud credential. It
is the smallest trusted computing base that answers "does this declaration exist, and where" — the
only question a refutation tool asks. Heavier tools (`tfparse`, `terraform-ls`) buy semantic
evaluation we deliberately do not want (see §3) at the cost of a provider/toolchain dependency and,
for several, a subprocess.

### 3. Refutation-only; the tool asserts absence-is-false, never presence-is-true

A query returns one of exactly two outcomes: `refuted` (a real declaration DISPROVES an asserted
absence, carrying its source span) or `abstain` (a closed, enumerated reason). It NEVER emits
`match` and NEVER asserts an absence. Grounding a *positive* claim ("this resource is configured
correctly") would require evaluating expressions, provider schemas, and variable resolution — the
computed, dynamic, provider-coupled surface this slice explicitly excludes. Refutation needs only
structural presence, which the parser gives deterministically.

### 4. V1 prohibitions

The following are OUT of scope for V1 and abstain with a closed reason rather than guessing:

- **Computed / dynamic values** — an interpolated `source`, a computed attribute, a splat/index
  expression (`ambiguous/dynamic_source|dynamic_expression|computed_value|splat_index`).
- **Provider attributes** — an attribute on a managed/data resource is provider-schema territory
  (`ambiguous/provider_attribute`).
- **Unknown tfvars** — a changed `.tfvars` is inspectable but is NEVER auto-selected as input
  (`ambiguous/unknown_tfvars`); `Snapshot.selected_tfvars` is `[]` unless explicitly selected.
- **Out-of-snapshot paths** — an absolute/out-of-repo `selected` path or an escaping symlink is
  refused (`private_or_internal_suspected/path_outside_snapshot`).
- **Unbounded repos** — a module/file/byte bound raises `TerraformLimitError` and yields NO partial
  snapshot (`other/module_limit|file_limit|byte_limit`).

Receipts and evidence carry source paths, spans, structural kinds, and hashes — NEVER credentials
or raw literal values. Every attribute literal and `default` value is redacted.

### 5. Registry metadata probing (V1) vs remote content fetch (still deferred)

Following a literal in-repo `module` `source` is in scope. Task `depraved-classless-rooster` /
`1c52-5e73-4d61-4124` extends V1 with a **metadata-only** `probe_source` operation that refutes
an asserted-absent `source` positive-only: a repo-contained local module refutes at T1, and
**positively-reachable registry metadata** refutes at T0. It calls only the Terraform Registry
**service-discovery and version/metadata** endpoints over HTTPS — NEVER a module
**download/archive** URL — and treats every access failure (401/403/404, 429, DNS/TLS/proxy,
5xx, cross-host redirect, timeout, malformed/oversized body) as a closed abstention, never as
absence.

**Downloading or expanding remote module content** (a git/HTTP/S3/GCS checkout, a registry
archive, Terraform Cloud API beyond registry discovery, credential helpers, provider/cloud
credentials) remains OUT of scope and approved-but-deferred to its own gated step.

**Credentialed-HTTPS trust boundary.** The metadata probe is a deliberate, bounded exception to
§1's "no network fetch" for the parse path, justified the same way ADR 0063 justifies the
web-search capability: it is optional, off by default (the public default registry needs no
credential), positive-only, and credential-redacting. It reuses **ambient** Terraform
credentials read-only in Terraform's own precedence (`TF_TOKEN_<host>` →
`TF_CLI_CONFIG_FILE`/default CLI config → `credentials.tfrc.json`) for the **exact** registry
hostname only; it never prompts, writes a credential file, runs a `credentials_helper`, or
borrows a provider/cloud credential. The target is validated **before** any token attaches:
HTTPS-only; a no-redirect opener (a cross-host redirect is a `network_error` before the
credential is sent); embedded URL credentials, literal IPs, and non-HTTPS rejected; and a
private-address host permitted only when it exactly matches a credentialed hostname. Responses
are bounded (1 MiB, 60 s worker deadline). Receipts, evidence, and logs record only
`auth_source=environment|static-file|none` and a closed `source_kind` (`local_module` |
`registry_provider` | `registry_module`) — NEVER a token, hash, path, header, or raw body. This
keeps the network trust boundary §1 avoided for the *parse* path narrow, explicit, and gated —
exactly the "explicit, gated, non-ambient step with its own ADR" the prior revision of this
section required.

### 6. Optional terraform-config-inspect corroborator

Task `schematic-gleesome-ballpython` / `c6ce-4d8d-1c12-47cc` adds one deliberately narrow
exception to §1's "no external process" rule: when an executable named exactly
`terraform-config-inspect` is already on `PATH`, rebar may run HashiCorp's shallow inspector as
an **independent corroborator**. The audited upstream basis is commit
`2fb54c236733ee65ee877105d595c124c993c64d`.

The trust boundary is executable-specific and snapshot-specific. rebar resolves only that basename,
canonicalizes it once, rejects non-regular/non-executable or repo-contained candidates, hashes it
before and after, and invokes it without a shell as:

```text
<absolute terraform-config-inspect> --json <fresh read-only snapshot>
```

The snapshot contains only the immutable indexed module's regular `.tf`/`.tf.json` files, no
symlinks, and never the real repository path. Stdin is closed; a process group/session is used where
the platform supports it; stdout/stderr are bounded; HOME/TMPDIR are temporary; Terraform/OpenTofu,
provider/cloud credential, proxy, plugin, CLI-config, and repository variables are stripped.
Malformed JSON, upstream diagnostics, schema skew, path escapes, nonzero exit, timeout, overflow,
or binary replacement all produce one redacted abstention with no partial facts.

Semantics are **match-only** and **positive-only**. A schema-valid entry can return `match@T1` for:
declared variables/outputs/resources/data resources/module calls/required providers,
`module_source_equals`, or `required_provider_present`. Missing/unequal/computed/absence claims
abstain. The corroborator cannot originate a review finding, veto ordinary review, assert absence,
or turn an abstention into negative evidence.

The standing prohibition remains in force for every other Terraform-related executable:
`terraform`, `opentofu`, `tfparse`, `tflint`, `trivy`, `terraform-ls`, and `terraform-docs` are not
permitted grounding executables.

## Consequences

- `T10` can now refute an asserted-absent declaration with a real source span, so it grounds
  instead of over-abstaining — while its trust boundary stays a single pinned OSS parser.
- The two operation-certificate kinds and the plan-review gate are unchanged; this adds a
  per-CALL tool provider, not a new gate or op-cert.
- The Pass-2 verifier issues its OWN structural query (it may reuse the immutable parse cache but
  never accepts a Pass-1 receipt as verification), preserving the two-pass independence.
- Portability holds: the capability is operation-linked and in-process, with no CI-provider
  trigger, so it satisfies `project.portability`.
