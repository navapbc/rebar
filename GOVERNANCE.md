# Governance

This document explains how rebar is governed: who makes decisions, how people
become maintainers, and what role Nava PBC plays. rebar is **maintainer-led**.
Code review and the canonical contribution flow happen on a self-hosted **Gerrit**
server; **GitHub is a read-only mirror**. See [`CONTRIBUTING.md`](CONTRIBUTING.md)
for how to contribute a change.

## 1. Overview

rebar is a maintainer-led open-source project, created and stewarded at **Nava
PBC**. The current **lead maintainer** is **Joe Oakhart** (`@JoeOakhartNava`).
Decisions are made in the open through Gerrit review and rebar's own ticket
store; there is no separate steering committee. Because GitHub is a read-only
mirror, all code decisions land through Gerrit, not through GitHub pull requests.

## 2. Roles

- **Contributor** — anyone who proposes a change through Gerrit review, files an
  issue, or improves the docs. No special access required.
- **Maintainer** — a trusted contributor with **Gerrit submit rights** (the
  `Contributors` group), **write access to the rebar ticket store**, and a say in
  the project's direction. Maintainers review and submit changes.
- **Lead maintainer** — holds **release authority** (publishing to PyPI, the
  Homebrew tap, and the MCP registry), administers project infrastructure, and
  has final say when consensus is not reached (see §3). Currently a single
  person; the role can be shared or transferred as the project grows.

## 3. Decision-making

Routine decisions are made by **consensus among maintainers**, expressed in the
normal course of Gerrit review and on rebar tickets. When maintainers do not
converge, the **lead maintainer has the final say**. Architecturally significant
choices are recorded as **ADRs under [`docs/adr/`](docs/adr/)** so the reasoning
is durable and reviewable. There is no formal voting machinery — the project is
small enough that discussion-to-consensus, with a documented tiebreaker, is
sufficient.

## 4. Becoming a maintainer

Maintainership is earned through **sustained, quality contributions reviewed on
Gerrit** — a track record of a few well-reviewed changes merged is the proof.
There is no application form: the **lead maintainer invites** a contributor to
become a maintainer once that track record is clear. New maintainers are added to
the Gerrit `Contributors` group and listed in [`MAINTAINERS.md`](MAINTAINERS.md).

## 5. Stepping down / emeritus

Maintainers may step down at any time. After extended inactivity, a maintainer's
access is **removed for security reasons** — this is **non-punitive** operational
hygiene, not a judgment on past contributions. Former maintainers are moved to
the **Emeritus** section of [`MAINTAINERS.md`](MAINTAINERS.md) with thanks, and
are welcome to return.

## 6. Nava PBC's role

rebar is created and stewarded at **Nava PBC**. Nava **provides** the project's
infrastructure — the Gerrit host (`rebar.solutions.navateam.com`), the GitHub
organization (`navapbc`), CI, and employment of the current lead maintainer — and
serves as the **Code of Conduct backstop** (see [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)).
Nava does **not** direct individual contributions: contributors participate as
individuals, and technical decisions are made by maintainers on their merits.

**Succession & project authority (pending formal designation).** Whether ultimate
authority over the project's assets — the PyPI name `nava-rebar`, the Gerrit host,
and the `navapbc/rebar` repository — rests with the individual lead maintainer or
with Nava PBC has **not yet been formally designated**. Interim arrangement: these
assets are administered under **Nava PBC's accounts and infrastructure**, while the
lead maintainer holds day-to-day authority over releases and the roadmap. Nava PBC
is the practical continuity backstop if the lead maintainer becomes unavailable.
This section will be updated with the org's formal answer (and a distinct named CoC
backstop contact) once designated.

## 7. Changing this document

Changes to this document are proposed as a **Gerrit change** against `GOVERNANCE.md`
and approved by the **lead maintainer**, like any other change to the project.
