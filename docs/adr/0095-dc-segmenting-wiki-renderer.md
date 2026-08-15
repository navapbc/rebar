# 0095 — Data Center Markdown-to-wiki rendering is a segmenting, verify-then-fall-back renderer

> **Testing-assurance clause partially superseded by
> [ADR 0096](0096-pandoc-corpus-verification-boundary.md) (2026-08-14).** The
> requirement below that every conversion assertion execute the real binary no
> longer governs test routing. This ADR's production renderer, pin, platform,
> safety, degradation, immutability, and cutover decisions remain authoritative.

Status: Accepted
Date: 2026-08-12
Ticket: `271c-e88f-3220-4c9b` (epic `708d-0767-1a51-4928`)

## Decision

Jira Data Center rich text is produced by a **segmenting** renderer
(`adapters/jira_family/wiki_render.py`) that converts only structurally-safe units
through pandoc and passes everything else through byte-for-byte, rather than by
converting a whole body in one call.

pandoc is delivered by the **`pypandoc-binary==1.17` wheel** (the `wiki` extra),
which bundles the pandoc executable, so the path is self-contained with no host
install.

The renderer is **one-way** (Markdown → wiki). Jira wiki markup is lossy for
punctuation-dense prose, so loop prevention must not depend on a codec fixed point;
story `3388` supplies state-based echo suppression instead.

## Invariants

1. **Nothing is ever corrupted.** A unit that cannot be converted losslessly is not
   converted. After conversion, every code fragment of the source must reappear
   byte-identically and in order, or that unit falls back to its original Markdown.
2. **Code is content.** Arrows inside fences, four-space blocks and inline spans are
   never rewritten. Arrow substitution runs on the Markdown *before* pandoc, in one
   ordered pass in which `<!--` and `-->` map to themselves, so the `->` inside
   `-->` can never become `-→`.
3. **HTML comments survive exactly.** pandoc deletes them, and rebar's own
   `<!-- rebar:reconciler-echo -->` marker is an HTML comment, so comments are
   locked in phase 1 and pass through untouched.
4. **ASCII tables are wrapped, never converted** — exact bytes, once, in
   `{noformat}`.
5. **Rendering settles.** Passes 2–5 over the corpus are byte-identical. This
   requires distinguishing *unambiguous* wiki markers (`{{…}}`, `[label|target]`,
   `{color:…}`) from *ambiguous* ones (`*strong*`, `_emphasis_`): a rendered bullet
   such as `* *strong* … {{mono}}` begins with `* ` and would otherwise be
   re-rendered as CommonMark, flipping `*strong*` to `_strong_` and escaping
   `{{mono}}` to `\{\{mono\}\}` — an unstable 2-cycle. Unambiguous markers settle a
   unit on their own; ambiguous markers only settle a unit carrying no
   CommonMark-only marker.
6. **Degrading never loses content.** Missing pypandoc returns the body unchanged
   and warns; a conversion or preservation failure returns that unit unchanged.
   Reasons are fixed tokens and a body is never logged. The warning is emitted per
   call, not deduplicated: a once-guard would need mutable module state, which
   invariant 7 forbids, and the caller owns log rate limiting.
7. **Module state is immutable** and the module is **unwired**: `WikiTextCodec`
   stays identity and no live send path calls the renderer.

## Pin provenance

Observed in a clean environment on this pin, and asserted by non-skipping tests:

- `pypandoc.__version__ == "1.17"`, bundled pandoc **3.9**.
- `commonmark -> jira --wrap=none`:
  - `# Heading` → `h1. {anchor:}Heading\n` (the anchor is content-free and is stripped)
  - `` `mono` `` → `{{mono}}\n`
  - `**b**` → `*b*\n`; `*i*` → `_i_\n`; `[l](http://x)` → `[l|http://x]\n`
- The jira writer emits **no** `{color:…}` form for any color source: an HTML color
  span is stripped to plain text under both the `commonmark` and `html` readers.
  Color is therefore never a pandoc *output* to pin — it is only pre-existing wiki
  the inline scanner must recognize and pass exactly. Consequently **no test
  monkeypatches pandoc**; every conversion assertion uses the real binary.

## Alternatives considered

- **Whole-body conversion.** Echo-safe but corrupting: measured on this pin, a prose
  `->` is escaped to `\->`, a Markdown table delimiter row becomes `| \-\-\- |`,
  punctuation inside inline code is escaped (`` `->` `` → `{{\->}}`), and HTML
  comments are deleted. Rejected.
- **`md-to-jira` / `mistune-jira` / `md-jira`.** None offers pandoc's fidelity on the
  constructs rebar actually authors. Rejected.
- **Round-tripping through pandoc's jira *reader*.** It misparses arrows, ASCII
  tables and regexes, and erodes them across passes. Rejected — the renderer is
  one-way and never consumes its own output as input.

## Platform

The pin is gated on the project's Linux and macOS CI. `pypandoc-binary` also
publishes Windows wheels. A platform with no wheel falls back to the identity path
(body returned unchanged), which is echo-safe.

## Deferrals

- **Cutover, echo suppression and codec convergence** — story `3388`. Until then
  `WikiTextCodec` is identity and nothing observable changes.
- **Subprocess timeout, process-group reaping, safe-run batching, GPL/SBOM
  recording** — story `5c0e`. This ADR's renderer performs one pandoc invocation per
  renderable unit with a plain timeout.
- **Live rendered-HTML verification against a real Jira DC** — story `3289`.
