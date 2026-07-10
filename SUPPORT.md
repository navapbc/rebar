# Support

Thanks for using **rebar**! This page explains where to get help and how to
report problems. Bug reports, feature requests, and usage questions go to
**[GitHub Issues](https://github.com/navapbc/rebar/issues)** — that is the
primary, zero-friction channel and needs no extra accounts. Code *changes* are
different: **GitHub is a read-only mirror** of the source, and the canonical
contribution flow lives on a self-hosted Gerrit server; see
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Documentation first

Most questions are answered by the docs:

- **[README.md](README.md)** — overview, install, and the Quickstart golden path.
- **`rebar --help`** (and `rebar <subcommand> --help`) — the authoritative,
  always-current command reference.
- **[docs/](docs/)** — architecture, event schema, concurrency, the LLM/agent
  framework, the workflow engine, and more.
- **[docs/api-stability.md](docs/api-stability.md)** — which surfaces are stable.

## Where to ask / report

**GitHub Issues (<https://github.com/navapbc/rebar/issues>) are the primary
channel.** Open a new issue and pick the matching form from the chooser:

- **Bugs** — use the 🐛 bug form. It asks for your rebar version, environment,
  and what you did / saw / expected. No Gerrit account required.
- **Feature requests** — use the ✨ feature form (problem + proposed solution).
- **Usage questions** — use the 💬 question form. rebar has no separate forum or
  Discussions board (see the note below), so questions live in Issues too. If you
  only need a quick pointer and would rather not open an issue, emailing
  **hello@navapbc.com** is a best-effort fallback — but Issues are the primary,
  tracked channel.
- **Code changes / patches** — Issues are for reports, not code. `main` advances
  only through Gerrit review: propose your change on Gerrit
  (`https://rebar.solutions.navateam.com`) per [`CONTRIBUTING.md`](CONTRIBUTING.md).
  Do **not** open a GitHub pull request — it cannot be merged here.
- **Security vulnerabilities** — do **not** report these in public. Follow
  [`SECURITY.md`](SECURITY.md) (private email / GitHub Private Vulnerability
  Reporting).
- **Code of Conduct concerns** — see [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
  (enforcement contact: hello@navapbc.com).

> **GitHub Discussions are intentionally off.** rebar keeps a single inbox —
> GitHub Issues — instead of enabling Discussions: with a single maintainer there
> is no community to self-answer a separate Q&A board, so usage questions go in
> Issues (the question form). Revisit trigger: if question-issues start clogging
> the bug/feature tracker, split them out into Discussions (issue → discussion
> conversion is one click).

## What is out of scope

- **Merging GitHub pull requests.** `main` only advances via a Gerrit-submitted
  change that then replicates to GitHub; GitHub PRs are not merged. The PR
  template explains the redirect.
- **Real-time / guaranteed-SLA support.** rebar is pre-1.0 open-source software
  provided without a support guarantee.
