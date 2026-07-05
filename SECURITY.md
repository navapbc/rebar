# Security Policy

We take the security of rebar seriously and appreciate responsible disclosure.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
pull requests, or discussions.** (GitHub is a read-only mirror of this project;
see [`CONTRIBUTING.md`](CONTRIBUTING.md).)

Report privately through either channel:

1. **Email (primary, always available):** send details to
   **hello@navapbc.com** with a subject line beginning `[rebar security]`.
2. **GitHub Private Vulnerability Reporting:** if enabled on the mirror, use the
   **Security → Report a vulnerability** button
   (https://github.com/navapbc/rebar/security/advisories/new). If that page is
   unavailable, use the email channel above.

Please include, to the extent you can:

- the affected component (CLI, Python library, MCP server, or reconciler) and
  version (`nava-rebar` release or commit SHA);
- a description of the issue and its impact;
- reproduction steps or a proof of concept;
- any suggested remediation.

## What to Expect

- **Acknowledgement:** we aim to acknowledge your report within **5 business days**.
- **Assessment:** we will investigate and keep you informed of progress.
- **Coordinated disclosure:** we will work with you on a disclosure timeline and
  credit you (if you wish) once a fix is available. Please give us a reasonable
  window to remediate before any public disclosure.

## Supported Versions

rebar is pre-1.0 and ships from `main`. Security fixes are applied to the latest
released version of `nava-rebar` on PyPI and to `main`. Older releases are not
maintained; please upgrade to the latest release to receive fixes.

| Version        | Supported          |
| -------------- | ------------------ |
| Latest release | :white_check_mark: |
| Older releases | :x:                |
