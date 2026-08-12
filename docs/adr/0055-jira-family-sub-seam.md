# ADR 0055 — The Jira-family sub-seam, shared `jira` provenance, and the Data Center horizon

**Status:** Accepted
**Date:** 2026-07-30
**Relation:** AMENDS [`0083-reconciler-vendor-adapter-seam.md`](0083-reconciler-vendor-adapter-seam.md).
That ADR's two-layer model (backend-neutral core + `adapters/<backend>/`), its phasing
constraints, and its `Backend` port are UNCHANGED and still binding. This ADR adds a third
layer *inside* the adapter half, records the provenance decision the family forced, and
narrows one claim 0035 made about what a contract suite proves.
**Epic:** `e369-a449-4773-48fb` — *Jira Server / Data Center support via a Jira-family seam
(not a forked second adapter)*. Story J8 `8c35-1a5e-927b-4644`.

> **Numbering caution.** THREE files in `docs/adr/` carry the number 0035 —
> `0035-rc2b-snapshot-horizon-safe-replay.md`, `0082-code-review-two-lane-tier-tagged-impact.md`,
> and `0083-reconciler-vendor-adapter-seam.md`. A bare "ADR 0035" is ambiguous in this repo.
> Every reference below cites the **filename**.

## Context

Self-hosted clients (CMS and similar federal deployments) run **Jira Data Center**, which
rebar could not reconcile against. DC differs from Cloud on exactly three axes: REST v2 with
wiki-markup/plain-text bodies instead of v3 + Atlassian Document Format; Personal Access Token
bearer auth instead of the ACLI subprocess; and name-based user identity instead of Cloud's
opaque `accountId`.

`0083-reconciler-vendor-adapter-seam.md` provides one seam — neutral core on one side,
`adapters/<backend>/` on the other — and that seam is the right shape for a *different vendor*.
It says nothing about a second member of the *same* vendor family, and GitHub PR #120 showed
what happens in that gap: a second adapter package that rode the `Backend` port correctly but
reached into `adapters/jira/` privates (`_map_local_to_jira_fields`, `_LOCAL_TO_JIRA_STATUS`,
`_LOCAL_TO_JIRA_PRIORITY`, `_sanitize_label`, `_RELATION_TO_JIRA_LINK`)
and forked `map_fields_to_remote` as a verbatim copy with one line changed. That creates a
de-facto Jira-family layer with **no contract**: `adapters/jira/` becomes simultaneously a
concrete backend and a shared library, with nothing pinning what is shared and nothing stopping
a Cloud change from breaking DC.

Three further decisions were about to be made silently by inheritance rather than deliberately:

1. **Provenance.** The inbound apply path stamps a hardcoded creation channel — see
   `apply_inbound_records._inbound_create_write_create_event`, which calls
   `validate_creation_channel("jira")`. (Cite the symbol, not a line: this literal has already
   moved once under an earlier draft of the J8 plan.) A DC backend inheriting that literal would
   decide the Cloud/DC provenance question by accident.
2. **A third-party runtime dependency.** The DC transport is the first place the reconciler
   depends on a package rebar does not own.
3. **The verification oracle.** Atlassian stopped issuing DC trial licenses (30 March 2026) and
   publishes no official OpenAPI/Swagger spec for DC — only a WADL. There is no document to
   conform to and no licence to buy.

Left unrecorded, each of these gets re-litigated the next time a Jira-like tracker appears.

## Decision

### (a) The seam is TWO levels for one vendor family

`adapters/` now holds three packages, not two:

```
rebar_reconciler/
  adapters/
    jira_family/          # Jira-family SHARED layer — one implementation, both deployments
    jira/                 # Jira Cloud (ACLI subprocess transport)
    jira_datacenter/      # Jira Data Center (pycontribs/jira transport)
```

`jira_family/` holds the units that are Jira-family-*general* rather than Cloud-specific, under
**public** names: the local↔Jira value maps (`LOCAL_STATUS_TO_JIRA`, `LOCAL_PRIORITY_TO_JIRA`),
the link-relation vocabulary (`RELATION_TO_JIRA_LINK`), the field sanitizers, the `rebar-id:`
identity convention (`JiraIdentityConvention`).

> **Superseded in part (task `f020`).** This layer originally also held the absence-probe
> classifier `classify_probe_response`. Bug `3b5f` removed every consumer of the inbound
> absence-probe chain, and `f020` deleted the dormant port together with the classifier, so
> `jira_family/probe.py` no longer exists.

**Dependency direction is one-way and machine-checked.** `jira_family/` imports nothing from
`adapters/jira/` or `adapters/jira_datacenter/`; concrete backends import it and it never
imports them back. That is enforced by a structural AST oracle,
`tests/unit/rebar_reconciler/test_jira_family_boundary_heldout.py`, which runs inside the
ordinary `make test` that CI's `Verified` gate already executes — no new CI job, no new dev
dependency — in the same style as `test_backend_neutrality.py`. The oracle is itself given teeth
by a synthetic-violation test, because a tautological graph checker that never fires would pass
every other assertion in the file.

**Sharing is by dependency inversion where the module cannot move.**
`0083-reconciler-vendor-adapter-seam.md` §(a) pins `adf.py`, `outbound_fields.py`, and
`comment_limits.py` as Jira-coupled **and** location-pinned: the file-location dynamic loader
threads them by filename, so relocating them requires teaching the loader the new sub-package —
that is 0035's **Phase 2**. Location-pinned means "cannot change path", not "cannot be edited",
so those modules keep their paths and their *contents* change to import from `jira_family/` and
to accept `jira_family` contracts as parameters. Same single-implementation guarantee, at a
fraction of the risk to the live-validated Cloud path.

### (b) Two contracts parameterize the family

Only the axes that genuinely differ are pinned as contracts. Everything else is shared code.

| Contract | Module | Cloud | Data Center |
|---|---|---|---|
| `RichTextCodec` | `adapters/jira_family/rich_text.py` | `AdfCodec` (`adapters/jira/rich_text_codec.py`) | `WikiTextCodec` |
| `UserIdentityModel` | `adapters/jira_family/identity_model.py` | resolves against `account_id` | resolves against `name` |

`RichTextCodec` has **four** operations — `fit_outbound`, `normalize_outbound`, `to_wire`,
`decode_inbound` — not two. `fit_outbound` and `normalize_outbound` are deliberately distinct
because Cloud's send path composes both while the description sanitizer applies only the fit;
collapsing them would silently change the observable behaviour of whichever caller lost its step.
The Protocol and the DC implementation live in `jira_family/`; the Cloud implementation lives on
the Cloud side, because the shared layer may not import a concrete backend (§(a)).

`UserIdentityModel` pins the 3-state account-resolution fast-path
(`resolve(local_value, remote_identity) -> (value, authoritative, is_account_id)`) plus
`to_payload`. The state machine is Cloud's pre-existing `resolve_assignee` behaviour, reproduced
once and parameterized by the remote-identity key and by whether a resolved value can ever be an
accountId (Cloud only; DC has none). Both implementations take their lookup resolver as an
**explicit constructor parameter** — nothing is discovered with `getattr`. That is not style: a
resolver that silently goes missing makes every resolution non-authoritative, and a permanently
non-authoritative assignee makes the outbound diff re-emit a change it can never converge (the
churn class of PR #120's defect). A required constructor parameter makes that defect unwritable.

One further unit *was* parameterized rather than shared verbatim: `classify_probe_response`
took the resolved-status set as a **required keyword-only** parameter, because a self-hosted DC
workflow may name its resolved states anything. That classifier was deleted with the dormant
absence-probe port by task `f020`; the parameterization principle it illustrated still governs
any future Jira-family unit whose values are workflow *names* rather than protocol constants.

### (c) Provenance: Cloud and DC share ONE `jira` identity — deliberately

Cloud and DC are the **same vendor**. They therefore share:

- the single `jira` creation channel (`validate_creation_channel`);
- the `jira-` local-id prefix (`inbound_translate`, `apply_inbound`, `reducer._processors`);
- the `jira_key` binding vocabulary in `binding_store.py`.

The family is **declared by the backend**, not derived in the core: each backend carries an
`identity_family` attribute (`"jira"` on both `adapters/jira/backend.py` and
`adapters/jira_datacenter/backend.py`), and the core reads it via
`apply_inbound_records._backend_creation_channel`. Two alternatives were rejected there. A
string transform (family = everything before the first `-`) keeps the core vendor-neutral but
fails **open**: a future backend registered as `import-foo` would silently collapse onto
`import`, a real member of the channel vocabulary, and mis-stamp every identity it minted with
nothing raising. A literal lookup table in the core fails closed but puts vendor names back into
a core module, which `test_backend_neutrality.py` forbids. Asking the backend does both.

**What this defers to epic `be74-7832-03a8-48ac`:** generalizing `jira_key`, the `jira-` prefix,
and the creation-channel vocabulary. That work is `be74`'s spine — a genuinely different vendor
(GitHub) makes it necessary — and it touches 50+ binding-store sites. Pulling it forward here
would mean a store migration for no user-visible benefit and would delay the DC delivery.

**`RemoteRef.instance` was INTENDED design when this ADR was written; it is now built —
see the amended open-issue entry below, which also records that it protects LESS than the next
paragraph claims (it does not prevent local-id collision).**
`0083-reconciler-vendor-adapter-seam.md` §(d) item 4 defines
`RemoteRef{vendor, instance, remote_id}` precisely so two deployments of one vendor never
collide, and that is the mechanism this provenance decision is *designed* to lean on. It is not
built: `src/` contains only the frozen dataclass definition and its docstring — `grep -rn
"RemoteRef(" src/` returns nothing, and the only constructions anywhere in the repo are two test
helpers. So today, two DC deployments (or a Cloud and a DC deployment) reconciled into one store
are **not** disambiguated by anything. Do not build on this field until it is populated; the gap
is tracked as [rebar:6a91-7429-e521-4a2e].

### (d) `pycontribs/jira`, extra-gated — and why Cloud was NOT migrated onto it

The DC transport is built on **`pycontribs/jira`**, the maintained Python Jira client, declared
only by the opt-in `[jira-datacenter]` extra (`jira>=3.8,<4`). `3.8` is the first release with
Server/DC Personal Access Token support (`token_auth=`), the DC 8.14+ auth mode this transport
uses; the `<4` ceiling avoids an unreviewed major-version jump changing the client's object model
out from under the transport's `.raw`-unwrapping boundary.

Every module under `adapters/jira_datacenter/` imports `jira` **lazily**, inside the function
that needs it, never at module top. That preserves the engine's `dependencies = []` contract:
`import rebar` — and importing the DC package itself — stays dependency-free, and a client that
does not adopt DC pays nothing. The guarantee has CI teeth: `jira` is on the absent-module list
in `.github/workflows/_optionality.yml`, alongside `langchain`, `langgraph`, `anthropic`,
and `opentelemetry`.

**Cloud's ACLI path was deliberately not migrated onto `pycontribs/jira`.** The `acli*` cluster
is live-validated against a production Jira and carries a ~29-test patch surface; rewriting its
transport would put the only working path at risk to serve a second deployment that does not need
it, and it would fold a delivery epic into a refactor 0035 already schedules as Phase 2 work.
`adapters/jira_datacenter/` imports nothing from `adapters/jira/` for the same reason.

### (e) The Docker harness is the verification oracle — because nothing else can be

There is no official OpenAPI/Swagger spec for Jira Data Center (WADL only), and no DC trial
licence to buy. So conformance cannot be checked against a document, and the alternative — a
fake encoding our own assumptions — proves only that we are self-consistent.

The oracle is therefore a **real Jira 8.17.1 DC instance in Docker**
(`tests/external/live_jira_dc/`), the same substrate the `pycontribs/jira` project validates
itself against — a vendored reproduction of that project's `docker/jira-test-image/Dockerfile`,
pinned by digest to its `addono/jira-software-standalone` base. 8.17.1 is ≥ 8.14, so PAT bearer
auth is exercised for real. Its limits are recorded rather than papered over:

- **Pinning the image digest does not pin Jira.** The image does not contain Jira; `atlas-run`
  downloads it from `maven.atlassian.com` at container start (~900 artifacts). Egress to
  Atlassian is a hard runtime dependency, and a withdrawal of those artifacts breaks the harness
  despite the pinned digest.
- **`linux/amd64` only.** On an emulated arm64 host Jira does not become usable within an hour;
  the harness's home is the native-x86_64 `jira-dc-harness` job in
  `.github/workflows/external-integration.yml`.
- **8.x only, and older than a client such as CMS is likely running.** It proves REST v2 protocol
  semantics, not deployment-specific workflow/field/permission behaviour — tracked separately as
  [rebar:9895-ac7d-4276-4f08].

### (f) The horizon: Data Center goes read-only on 28 March 2029

Data Center receives no new features today and goes **read-only on 28 March 2029**. This adapter
is therefore a **deliberately time-boxed investment** for existing self-hosted clients, and the
guidance to a future maintainer is explicit:

- The **durable asset is the family seam**, not the DC adapter. `jira_family/`, `RichTextCodec`,
  `UserIdentityModel`, and the `identity_family` declaration outlive DC and are what a third Jira
  deployment — or a Jira-shaped tracker — would reuse.
- Investment in `adapters/jira_datacenter/` should be **corrective, not expansive**: fix defects
  that affect real client instances; do not build new capability that only DC can express.
- When the horizon arrives, retiring `adapters/jira_datacenter/` should be a package deletion
  plus a registry key, with `jira_family/` and the Cloud path untouched. If a future change makes
  that untrue, it has re-coupled something this ADR uncoupled.

### (g) What execution proved about the port — and about what a contract suite certifies

`0083-reconciler-vendor-adapter-seam.md`'s **proof-of-seam** argument is that a backend-agnostic
contract suite is what certifies a backend. Driving the DC assembly against a real Jira for the
first time showed that argument holds **only as far as the port is COMPLETE and TYPED**.

**The port did not state what the core requires.** `TicketTransport` declared **six** members
(`create_issue`, `get_issue`, `update_issue`, `transition_issue_by_name`, `add_label`,
`search_issues`) while the core reaches for **twenty-one** distinct transport members. Twelve of
those were absent from the DC transport and declared on **no Protocol at all**.

**Why 21 and not 15 — the methodological point, and the most transferable line in this ADR.**
A call-form-only AST scan (`client.foo(...)`) finds fifteen. It misses six the reconciler reaches
for as **values** — `create_issue`, `delete_issue_link`, `get_issue`, `set_parent`,
`set_relationship`, `update_issue` — because the core routinely passes a bound method to a
helper, e.g. `_call_with_retry(client.delete_issue_link, link_id)`. **An audit of this seam must
match attribute ACCESS, not invocation.** That narrow pattern produced a wrong member count twice
during this epic; the structural oracle that now guards the port
(`tests/unit/rebar_reconciler/test_transport_port_completeness_heldout.py`) matches attribute
access for exactly this reason.

**The consequence was measured, not theorised.** A DC writing reconcile pass crashed on
`set_entity_property` while `isinstance(backend, Backend)` passed, the backend contract suite
passed, and 1600+ reconciler unit tests passed. Worse, the crash was the *lucky* failure mode:
seven of the twelve missing members have call sites that swallow `Exception` at **every**
invocation, so a DC deployment would appear to converge while silently not syncing comments,
links, parents, or properties, and not validating assignees. Filed as
[rebar:a357-b747-ece9-4cf5], closed by [rebar:bda8-370d-23ad-4e36]; the neutral-exception half is
[rebar:bb8c-8283-80c6-4052].

**Root cause, and the conditionality it forces.** The core passes the transport as an
**unannotated** parameter, so mypy cannot enforce port completeness — proven three ways:
unannotated, no error; `client: Any`, no error; `client: TicketTransport`, mypy rejects the
undeclared call. Tracked as [rebar:cc77-8120-bcc6-47e8]. Therefore:

> **A contract suite can only test what the port DECLARES.** "The contract suite certifies a
> backend" is conditional on the port being **complete** (its declared surface derived from the
> core's actual call sites, kept honest by a structural test rather than authorial discipline)
> **and typed** (transport parameters annotated, so the gap is visible to mypy). An
> under-declared port yields a certificate that means less than it reads as, and it fails in the
> most expensive direction: silently, at runtime, on the *second* backend rather than the first.

This is a property of the **seam**, not of the DC adapter. Epic `be74-7832-03a8-48ac` (GitHub
Issues) inherits it.

The defect was invisible to every static and unit-level signal and was caught only by a live
pass. That is the evidence for keeping a live tier in this seam's verification story at all.

### (h) The OSS comparison: the architecture converged; the gaps were all operational

An independent comparison of this integration against mature open-source Jira integrations
(session log [rebar:558e-55ce-d9f1-4fa9]) did **not** overturn the epic's design decisions. A
maintained client library rather than a hand-rolled REST client, PAT bearer auth, and an
ephemeral containerised instance as the oracle are all what mature projects do — and the rejected
PR #120, which hand-rolled a REST v2 client, would have diverged from that consensus rather than
toward it.

**Every gap the comparison found was instead in an OPERATIONAL EDGE** — behaviour that only
appears against a real, *differently-configured* instance:

| Edge | What it breaks | Ticket |
|---|---|---|
| Search-index lag (DC's Lucene index is eventually consistent) | keyless-pending recovery searches JQL, sees nothing, unbinds — the next pass creates a **duplicate** issue | [rebar:21fc-51d7-90ca-4a03] |
| Server-side pagination caps (`jira.search.views.default.max`) | a truncated first page read as "that is all there is": measured 20 of 250 parents recovered, silently | fixed in-flight |
| Admin-toggled rate limiting (DC 8.6+, **off by default**) | a static pacing policy is not implementable; the fix is honour `Retry-After`, degrade when absent | [rebar:b586-2fda-8a15-4260] |
| Screen configuration (required fields / labels not on the create screen) | a failed identity write **deletes** the created issue | [rebar:387d-09c5-3d50-4a1e] |
| Project moves (bindings key on the Jira KEY, which changes; the numeric id does not) | old key stops resolving | [rebar:7c26-4ac8-04a3-440e] |

**State the pattern plainly, because it tells the next backend author where to look:** the seam
design held up; the gaps were all in assuming a Cloud-shaped instance behaves like a DC-shaped
one. Every one was invisible to 1693 passing unit tests, a certified plan, and two green plan
reviews. **Two were invisible to the live harness as well** — it is a single default-configured
8.17.1 instance with no lagging index, no lowered pagination cap, no rate limiting, and no
restricted create screen. So "live testing is the oracle" is necessary and still not sufficient:
**the oracle covers only the configurations the oracle happens to have.**

### (i) Rejected alternatives

| Rejected | Why |
|---|---|
| **Fork per deployment** (PR #120's separate adapter package) | Two copies of the field maps and sanitizers drift — they had *already* drifted, `outbound_fields`/`config.py` carrying a `"deleted": "Done"` entry `jira_fields` lacked. Cross-adapter private imports are also a dependency direction `0083-reconciler-vendor-adapter-seam.md` never sanctioned, and they make `adapters/jira/` a concrete backend and a shared library at once, with nothing pinning what is shared. |
| **A hand-rolled stdlib REST client** | PR #120's client is competent, but `pycontribs/jira` already absorbs DC-vs-Cloud auth, pagination, and payload quirks that thousands of users have found. Confining it to an opt-in extra keeps the cost off every rebar install, so the "no dependencies" objection does not apply. |
| **`atlassian-python-api`** instead of `pycontribs/jira` | Surveyed in ADR 0004; it does support DC with PAT auth. `pycontribs/jira` wins on two specifics: it ships the Dockerized-Jira-SDK harness pattern adopted as this epic's oracle, and `JIRA(server=…, token_auth=…)` covers DC-vs-Cloud divergence behind one client object. Either would work; picking one and confining it to an extra is what matters. |
| **Generalizing the store vocabulary now** (`jira_key`, the `jira-` prefix, the channel vocabulary) | It is epic `be74`'s spine, touches 50+ binding-store sites, and would force a store migration for no user-visible benefit while delaying the DC delivery. Cloud and DC are the same vendor; there is nothing yet to generalize *for*. |
| **Migrating Cloud onto `pycontribs/jira`** | Puts the only live-validated path at risk (~29-test patch surface, production Jira) to serve a deployment that does not need it. See §(d). |
| **Moving the location-pinned modules now** (`adf.py`, `outbound_fields.py`, `comment_limits.py`) | Tidier layout, but it requires teaching the dynamic loader the new sub-package and re-targeting every `mock.patch("rebar_reconciler.<mod>.<attr>")` site — `0083-reconciler-vendor-adapter-seam.md` **Phase 2**. Dependency inversion gets the same single-implementation guarantee at a fraction of the risk. |
| **Documenting all of this only in `docs/architecture.md`** | rebar's ADR set is where rejected alternatives live. Without them, fork-per-deployment and the hand-rolled client get re-proposed the next time a Jira-like tracker appears. |

## Consequences

- **`adapters/` is now three packages, and the extra level is a fact a reader must know.** The
  layer diagram in `0083-reconciler-vendor-adapter-seam.md` §(c) shows two levels; the family
  layer sits inside the adapter half and is one-way by machine-checked contract.
- **A second Jira-family deployment is a configuration, not a fork.** A third one implements the
  two contracts and registers a key; it writes no value maps, no sanitizers, and no link
  vocabulary of its own.
- **`0083-reconciler-vendor-adapter-seam.md`'s "first real second backend is out of scope"
  statement is superseded.** Epic `e369` landed Jira Data Center ahead of the GitHub adapter
  `be74` was reserved for. That file has been corrected in all three places the claim appeared
  and now points here.
- **"The contract suite certifies a backend" is now a conditional claim** (§(g)), and the
  condition is enforced by a structural test rather than by discipline.
- **`RemoteRef.instance` is now BUILT — and it protects less than this ADR assumed**
  ([rebar:6a91-7429-e521-4a2e], which amended this entry). `Backend` declares `remote_ref()`,
  both concrete backends implement it, and `instance` is derived from the configured base URL and
  injected at construction.
  **The correction matters more than the implementation.** Two things this ADR ran together are
  distinct: Cloud-vs-DC was ALREADY separated by `vendor` itself (`"jira"` vs
  `"jira-datacenter"`), so `instance` was never needed for that; what it disambiguates is two
  deployments of the SAME vendor. And it does **NOT** prevent the collision that actually bites —
  `inbound_translate._jira_key_to_local_id` is `"jira-" + jira_key.lower()` and consults nothing
  else, so two DC deployments each owning a project `DIG` still both mint `jira-dig-123`. Nothing
  reads `instance` when a local id is minted. Making the id deployment-aware would change the id
  scheme for every existing Jira-sourced ticket — a breaking, store-wide migration deliberately
  not undertaken. So one store reconciled against two same-vendor deployments STILL has no
  local-id disambiguator; it now has a typed identity value that names the deployment.
  A `RemoteRef` is also not persisted anywhere, which is why deriving `instance` from a mutable
  base URL is safe: a URL change re-labels nothing on disk.
- **rebar gains its first third-party runtime dependency in the reconciler**, confined to
  `[jira-datacenter]`, lazily imported, and CI-checked absent from a default install.
- **The DC adapter has a stated end date.** Effort on it should be corrective; effort on
  `jira_family/` compounds.
- **`config.py`'s third copy of the status map remains** — the one knowingly-remaining literal
  outside `adapters/`, deferred to bug [rebar:fe15-3bc4-ed70-4b61] because its call sites are
  engine-level config with a wider blast radius than this seam.
