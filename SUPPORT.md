# Support

Thanks for using **rebar**! This page explains where to get help and how to
contribute — adapted to the fact that **GitHub is a read-only mirror** of this
project. Code review and the canonical contribution flow live on a self-hosted
Gerrit server; see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Documentation first

Most questions are answered by the docs:

- **[README.md](README.md)** — overview, install, and the Quickstart golden path.
- **`rebar --help`** (and `rebar <subcommand> --help`) — the authoritative,
  always-current command reference.
- **[docs/](docs/)** — architecture, event schema, concurrency, the LLM/agent
  framework, the workflow engine, and more.
- **[docs/api-stability.md](docs/api-stability.md)** — which surfaces are stable.

## Where to ask / report

Because the GitHub mirror has Issues disabled, please use these channels:

- **Bugs, features, and design discussion** — bring them through the contribution
  flow in [`CONTRIBUTING.md`](CONTRIBUTING.md): propose a change on Gerrit
  (`https://rebar.solutions.navateam.com`), where it is reviewed and discussed.
  For questions that are not yet a change, email **hello@navapbc.com**.
- **Security vulnerabilities** — do **not** report these in public. Follow
  [`SECURITY.md`](SECURITY.md) (private email / GitHub Private Vulnerability
  Reporting).
- **Code of Conduct concerns** — see [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
  (enforcement contact: hello@navapbc.com).

## What is out of scope

- **Merging GitHub pull requests.** `main` only advances via a Gerrit-submitted
  change that then replicates to GitHub; GitHub PRs are not merged. The PR
  template explains the redirect.
- **Real-time / guaranteed-SLA support.** rebar is pre-1.0 open-source software
  provided without a support guarantee.
