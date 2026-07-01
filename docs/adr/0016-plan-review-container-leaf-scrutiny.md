# ADR 0016: Plan-review proportionate scrutiny is keyed on container/leaf, not ticket type

- **Status:** Accepted
- **Context:** Bug *Plan-review scrutiny keyed on ticket type not container/leaf —
  security overlay skips container epics* (`a278`), discovered from the AWS-hosted
  Gerrit epic (`d251`). Relates to the project-supplied criteria overlay
  (ADR 0015) and the T10 infra overlay.

## Context

Plan-review runs a set of criteria against a ticket; `registry.applies()` decides
which criteria run (proportionate scrutiny). Historically that decision keyed on
ticket **type** via an `applies_at.levels` list of `epic`/`story`/`task`, and
`PlanContext.level` was derived directly from `ticket_type`.

This mis-modelled the real axis. The security overlay **T5c** was routed
`levels: ["task"]`, so `applies(T5c, level="epic")` was `False` — it never ran on a
container **epic**. The Gerrit epic `d251` is such an epic: it stands up an
internet-reachable Gerrit server (HTTP :443, SSH :29418) but never specifies how
humans/admins authenticate to it (it shipped the `DEVELOPMENT_BECOME_ANY_ACCOUNT`
default — anyone can become any account). The gate never security-reviewed the epic,
so the missing auth contract passed. The defect is that *type* is a poor proxy for
*altitude*: a childless epic behaves like a leaf, a story with children like a
container, and a cross-cutting risk surface (security) can appear at any altitude.

## Decision

**Proportionate scrutiny is keyed on container (has children) vs leaf (no children),
never on ticket type.**

- `applies_at.scope` lists the nodes a criterion runs at — a subset of
  `["container", "leaf"]`, either or both; absent ⇒ both. It replaces the
  type-`levels` list and the separate `container_only` flag.
- `registry.applies()` drops the type-derived `level`; it computes
  `node = "container" if has_children else "leaf"`. `is_mechanical_leaf`,
  `size_factor`/`_ticket_size`, and the DET-floor P9 file-impact check likewise key
  on container/leaf. `PlanContext.level` (the type→level map) is removed.
- Criteria are re-mapped faithfully: child-coverage criteria (G3/G4) → `container`;
  code-grounding and implementation-detail criteria (E4/A1/G1G2/E6/T5a/T5b/E5/T5d/
  T6/T9) → `leaf`; everything cross-cutting → both. **T5c security is no longer
  altitude-gated** (both) — the regression fix.
- The **bug/session_log gate exemption** is a *separate* axis (those types are exempt
  from the whole gate, upstream) and is intentionally left unchanged. The
  per-type `clarity_check` heading rewards are likewise out of scope here.
- A stale overlay using the legacy `levels`/`container_only` keys is rejected at load
  with a migration hint (fail loud, never silently ignored).

**Companion content fix — the endpoint access contract (T10 infra overlay).** The
under-specification that let `d251` through is generic: *standing up a network-reachable
service without stating its access contract*. T10 gains one checklist bullet: for every
service a plan stands up, it must state how **human** principals (users and admins)
authenticate — a named identity mechanism **or** an explicit, justified no-auth
rationale (loopback-only / behind an authenticating gateway / single-tenant private
network). Service-to-service credentials (deploy keys, webhook/API tokens, SSM secrets)
do not satisfy this — they authenticate machines, not human/admin access. This is
grounded in deny-by-default / secure-by-default (OWASP Proactive Controls C5) and the
"justify all trust boundaries" requirement (OWASP ASVS 1.1.4); the trust-boundary /
reachability gate (STRIDE; ASVS 1.4.1) is what keeps it from false-positiving local,
in-process, no-network work. T10 is chosen as the home because it fires **only** on
infrastructure intent, so it is inherently false-positive-safe on non-infra tickets.

## Consequences

- The security overlay now runs on container epics/stories that introduce a security
  surface — the class of miss that produced the Gerrit incident.
- A childless epic is scrutinised as a leaf (gets leaf criteria, e.g. code-grounding);
  a story with children is scrutinised as a container.
- **Validation (bug `a278`).** Against `d251`'s contents: the *old* criteria never
  flagged the missing Gerrit auth (T5c did not apply to the epic; forced to run, 4
  samples surfaced only encryption/token gaps). The *new* T10 endpoint-access-contract
  check flagged it in **6/6** runs (correctly naming the `DEVELOPMENT_BECOME_ANY_ACCOUNT`
  mode), with **0/4** false positives on local, no-network negatives (`8a1c` signing,
  `4702` attestations).
- Migration: any `.rebar/criteria_routing.json` overlay using `levels`/`container_only`
  must move to `scope`.
