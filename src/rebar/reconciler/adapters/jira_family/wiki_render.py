"""Segmenting Markdown-to-wiki renderer for Jira Data Center (story 271c, epic 708d).

Data Center stores rich text as WIKI markup (REST v2) while rebar authors Markdown
and currently sends it literally, so ``# Heading`` reads as a hash in Jira.

Converting a whole body in one pandoc call is not an option. Measured on the pinned
pandoc 3.9 (and originally on 3.6.1), its jira writer escapes a prose ``->`` to
``\\->``, rewrites a Markdown table delimiter row to ``| \\-\\-\\- |``, escapes
punctuation inside inline code (`` `->` `` becomes ``{{\\->}}``), and DELETES HTML
comments outright — a lone ``<!-- rebar:reconciler-echo -->`` renders to the empty
string, which would drop rebar's own reconciler echo marker.

So this module SEGMENTS: it converts only structurally-safe units and passes
everything else through byte-for-byte. A unit that cannot convert losslessly is
never converted, which is what makes the renderer non-corrupting rather than merely
lossy.

Pipeline:

1. **Lock** complete ``{noformat}`` spans, HTML comments, backtick/tilde fences and
   four-space-indented blocks — including their internal blank lines — so nothing
   downstream can split or rewrite them.
2. **Split** the intervening text into blank-line-separated blocks, preserving
   separators and order. A list is ONE unit — pandoc needs the whole list to emit a
   correct wiki list, so it is never split per item.
3. **Classify** each unit through six ordered rules (see :func:`_classify`) and
   either pass it exactly, wrap it once in ``{noformat}``, or render it via pandoc.
4. **Verify** the conversion: every code fragment of the source must reappear
   byte-identically, in order, or that unit falls back to its original Markdown.

pandoc ships bundled inside the pinned ``pypandoc-binary`` wheel (the ``wiki``
extra), so the DC path is self-contained with no host install. When pypandoc is
absent the renderer returns its input unchanged — plain Markdown is trivially
echo-safe, so the degraded mode loses richness, never content.

**This module is pure and unwired.** ``WikiTextCodec`` stays fully identity and no
live send path calls it; story 3388 performs that cutover together with the
echo-safety layer, and story 5c0e adds the subprocess timeout and process-group
reaping. Nothing observable changes on merge.

Per ``docs/adr/0083-reconciler-vendor-adapter-seam.md`` this package imports nothing
from ``adapters/jira/``; pandoc is not a Cloud dependency, so the renderer belongs
here in the shared Jira-family layer. ``docs/adr/0095-dc-segmenting-wiki-renderer.md``
records the engine decision, the pin provenance and the deferred boundaries.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

logger = logging.getLogger(__name__)

# The single pandoc conversion this module performs. ``commonmark`` matches the
# Markdown dialect rebar bodies are authored in; ``--wrap=none`` stops pandoc
# reflowing prose, which would churn the wire on every pass.
_PANDOC_FROM: Final = "commonmark"
_PANDOC_TO: Final = "jira"
_PANDOC_ARGS: Final = ("--wrap=none",)

# One structured event name for every degraded path. The reason is a fixed token
# and a body is NEVER logged — these bodies are ticket content.
_FALLBACK_EVENT: Final = "wiki_render_fallback"

# pandoc's jira writer emits an anchor macro after every heading
# (``h1. {anchor:}Title``). It carries no content, so it is stripped.
_ANCHOR_RE: Final = re.compile(r"\{anchor:[^}]*\}")

_FENCE_RE: Final = re.compile(r"^\s*(```+|~~~+)")
_NOFORMAT_OPEN_RE: Final = re.compile(r"^\s*\{noformat\}\s*$")
_INDENTED_RE: Final = re.compile(r"^ {4,}\S")
_COMMENT_OPEN: Final = "<!--"
_COMMENT_CLOSE: Final = "-->"

# --- classifier patterns ----------------------------------------------------
_WIKI_HEADING_RE: Final = re.compile(r"^\s*h[1-6]\.\s")
_WIKI_MACRO_RE: Final = re.compile(r"^\s*\{(code|noformat)[:}]")
_PIPE_DELIM_RE: Final = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
_BOX_RULE_RE: Final = re.compile(r"^\s*\+[-+=]{2,}\+\s*$")
_HTML_TAG_RE: Final = re.compile(r"<[/!]?[A-Za-z][^>]*>")
# Regex-ish prose: constructs pandoc escapes destructively.
_REGEX_RE: Final = re.compile(r"(\(\?|\\[bdsw]|\[\^[^\]]*\]\s*[*+?{]|\[[^\]]+\]\s*\{\d+,\d*\})")

# Pre-existing Jira inline markup the renderer must recognize and pass exactly, so a
# Jira-authored body — or this renderer's OWN output on a later pass — is never
# re-rendered as Markdown.
#
# The split matters. UNAMBIGUOUS forms exist only in wiki, so they settle a unit on
# their own even when it also looks like a CommonMark list: a rendered bullet such as
# ``* *strong* … {{mono}}`` starts with ``* `` and would otherwise be re-rendered,
# which flips ``*strong*`` to ``_strong_`` and escapes ``{{mono}}`` to ``\{\{mono\}\}``
# — an unstable 2-cycle. AMBIGUOUS forms overlap CommonMark emphasis, so they only
# settle a unit that carries no CommonMark-only marker.
_JIRA_UNAMBIGUOUS_RES: Final = (
    re.compile(r"\[[^\]|]+\|[^\]]+\]"),  # [label|target]
    re.compile(r"\{color:[^}]+\}.*?\{color\}", re.DOTALL),  # {color:name}...{color}
    re.compile(r"\{\{[^}]+\}\}"),  # {{monospace}}
)
_JIRA_AMBIGUOUS_RES: Final = (
    re.compile(r"(?<![*\w])\*[^*\s][^*]*\*(?![*\w])"),  # *strong*, never **
    re.compile(r"(?<![_\w])_[^_\s][^_]*_(?![_\w])"),  # _emphasis_
)
# CommonMark-only markers: their presence means the unit is Markdown, not wiki.
_COMMONMARK_ONLY_RE: Final = re.compile(
    r"(\*\*)|(^\s{0,3}#{1,6}\s)|(^\s{0,3}>)|(^\s{0,3}[-+*]\s)|(^\s{0,3}\d+\.\s)|(`)|(!\[)|(\[[^\]]*\]\([^)]*\))",
    re.MULTILINE,
)

# --- arrows -----------------------------------------------------------------
# ONE ordered pass. The HTML-comment delimiters are matched FIRST and map to
# themselves, so the ``->`` inside ``-->`` is consumed as part of the delimiter and
# can never be rewritten to ``-→``.
_ARROW_MAP: Final = {
    _COMMENT_OPEN: _COMMENT_OPEN,
    _COMMENT_CLOSE: _COMMENT_CLOSE,
    "<->": "↔",
    "->": "→",
    "<-": "←",
    "=>": "⇒",
}
_ARROW_RE: Final = re.compile("|".join(re.escape(token) for token in _ARROW_MAP))

# Inline code: a run of backticks with a matching closing run, on one line.
_INLINE_CODE_RE: Final = re.compile(r"(?P<ticks>`+)(?P<body>[^`]+)(?P=ticks)")

# Unit kinds.
_EXACT: Final = "exact"
_TABLE: Final = "table"
_RENDER: Final = "render"
_BLANK: Final = "blank"


def _pandoc_path() -> str | None:
    """Locate the wheel-bundled pandoc, or ``None`` when the extra is absent.

    Deliberately NOT cached: module state stays immutable so tests and callers see
    a consistent answer, and the probe is a cheap attribute lookup.
    """
    try:
        import pypandoc
    except ImportError:
        return None
    try:
        return str(pypandoc.get_pandoc_path())
    except OSError:
        return None


def _lock_and_split(markdown: str) -> list[tuple[str, str]]:
    """Phase 1 + 2: lock verbatim spans, then split the rest into units.

    Returns ``(kind, text)`` pairs whose concatenation with newlines reproduces the
    input exactly, so reassembly cannot lose or reorder anything.
    """
    units: list[tuple[str, str]] = []
    lines = markdown.split("\n")
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]

        if not line.strip():
            start = index
            while index < total and not lines[index].strip():
                index += 1
            units.append((_BLANK, "\n".join(lines[start:index])))
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0] * 3
            start = index
            index += 1
            while index < total and not lines[index].strip().startswith(marker):
                index += 1
            index = min(index + 1, total)
            units.append((_EXACT, "\n".join(lines[start:index])))
            continue

        if _NOFORMAT_OPEN_RE.match(line):
            start = index
            index += 1
            while index < total and not _NOFORMAT_OPEN_RE.match(lines[index]):
                index += 1
            index = min(index + 1, total)
            units.append((_EXACT, "\n".join(lines[start:index])))
            continue

        if _COMMENT_OPEN in line:
            start = index
            while index < total and _COMMENT_CLOSE not in lines[index]:
                index += 1
            index = min(index + 1, total)
            units.append((_EXACT, "\n".join(lines[start:index])))
            continue

        if _INDENTED_RE.match(line):
            start = index
            while index < total and (_INDENTED_RE.match(lines[index]) or not lines[index].strip()):
                index += 1
            while index > start and not lines[index - 1].strip():
                index -= 1
            units.append((_EXACT, "\n".join(lines[start:index])))
            continue

        start = index
        while index < total and lines[index].strip():
            if index > start and (
                _FENCE_RE.match(lines[index])
                or _NOFORMAT_OPEN_RE.match(lines[index])
                or _COMMENT_OPEN in lines[index]
            ):
                break
            index += 1
        block = "\n".join(lines[start:index])
        units.append((_classify(block), block))

    return units


def _mask_code(text: str) -> str:
    """Blank out inline-code spans so classifiers see prose only."""
    return _INLINE_CODE_RE.sub(lambda m: "`" + " " * len(m.group("body")) + "`", text)


def _classify(block: str) -> str:
    """Return this block's unit kind, applying the six rules in order."""
    lines = block.split("\n")

    # 1 — already Jira block markup.
    if _WIKI_HEADING_RE.match(block) or _WIKI_MACRO_RE.match(block):
        return _EXACT

    masked = _mask_code(block)

    # 2 — pre-existing Jira INLINE markup. Unambiguous forms settle the unit on
    # their own; ambiguous ones only when nothing CommonMark-only is present.
    if any(pattern.search(masked) for pattern in _JIRA_UNAMBIGUOUS_RES):
        return _EXACT
    if any(pattern.search(masked) for pattern in _JIRA_AMBIGUOUS_RES) and not (
        _COMMONMARK_ONLY_RE.search(masked)
    ):
        return _EXACT

    # 3 — ASCII tables: wrapped verbatim, never converted.
    pipe_lines = [line for line in lines if "|" in line]
    if len(pipe_lines) >= 2 and any(_PIPE_DELIM_RE.match(line) for line in lines):
        return _TABLE
    if any(_BOX_RULE_RE.match(line) for line in lines):
        return _TABLE

    # 4 — raw HTML (comments were already locked in phase 1).
    if _HTML_TAG_RE.search(masked):
        return _EXACT

    # 5 — regex-dense prose outside code.
    if _REGEX_RE.search(masked):
        return _EXACT

    # 6 — everything else renders.
    return _RENDER


def code_fragments(markdown: str) -> list[str]:
    """Return the literal code content of ``markdown``, in order.

    Fences, four-space blocks and inline spans. These are the parts that must
    survive conversion byte-for-byte: their content is code, so ANY rewrite —
    including a pandoc escape such as ``->`` becoming ``\\->`` inside ``{{…}}`` — is
    a content change, not a rendering change.
    """
    fragments: list[str] = []
    in_fence = False
    for line in markdown.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or _INDENTED_RE.match(line):
            if line.strip():
                fragments.append(line.strip())
            continue
        for span in _INLINE_CODE_RE.finditer(line):
            body = span.group("body").strip()
            if body:
                fragments.append(body)
    return fragments


def substitute_arrows(markdown: str) -> str:
    """Replace prose ASCII arrows with Unicode glyphs, leaving code untouched.

    Runs on the Markdown BEFORE pandoc, because pandoc's jira writer would escape a
    bare ``->`` to ``\\->``. Arrows inside fenced blocks, four-space blocks and
    inline code spans are content and are never rewritten, and the HTML-comment
    delimiters map to themselves so ``-->`` survives intact.
    """
    out: list[str] = []
    in_fence = False
    for line in markdown.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or _INDENTED_RE.match(line):
            out.append(line)
            continue
        out.append(_substitute_line(line))
    return "\n".join(out)


def _substitute_line(line: str) -> str:
    """Substitute arrows in one line, copying inline-code spans exactly."""
    pieces: list[str] = []
    cursor = 0
    for span in _INLINE_CODE_RE.finditer(line):
        pieces.append(_ARROW_RE.sub(lambda m: _ARROW_MAP[m.group(0)], line[cursor : span.start()]))
        pieces.append(span.group(0))
        cursor = span.end()
    pieces.append(_ARROW_RE.sub(lambda m: _ARROW_MAP[m.group(0)], line[cursor:]))
    return "".join(pieces)


_PANDOC_GRACE_SECONDS: int = 3  # SIGTERM grace before SIGKILL
_PANDOC_DRAIN_SECONDS: int = 2  # bounded post-SIGKILL reap/drain (D-state safe)
_PANDOC_TIMEOUT_DEFAULT: float = 10.0  # mirrors ReconcilerConfig.dc_pandoc_timeout_s


def _pandoc_timeout() -> float:
    """The per-invocation wall-clock ceiling, from ``reconciler.dc_pandoc_timeout_s``.

    Read at render-pass time (or standalone conversion-call time) so an operator
    can widen it without a redeploy, and fail-SAFE rather than fail-open: an
    unreadable config, a ``rebar`` package that is not importable (the engine
    ships as stdlib-only subprocess package data), or a non-positive value all
    fall back to the built-in default. A timeout of zero would mean "kill pandoc
    immediately", degrading every unit to raw Markdown — a config fault must not
    silently disable rendering.

    ``AttributeError`` is in that set for a reason rather than by reflex: the
    reconciler engine is loaded as package data and can run against a rebar whose
    config predates this key, and callers substitute partial config objects. A
    renderer that raised because one field was missing would take down the pass
    over something it can trivially default.
    """
    try:
        from rebar.config import ConfigError, resolve_pandoc_timeout
    except ImportError:
        return _PANDOC_TIMEOUT_DEFAULT
    try:
        return resolve_pandoc_timeout(_PANDOC_TIMEOUT_DEFAULT)
    except (ConfigError, AttributeError, TypeError, ValueError):
        return _PANDOC_TIMEOUT_DEFAULT


def _convert(markdown: str, pandoc: str, timeout: float | None = None) -> str | None:
    """Run pandoc over one unit; ``None`` on any failure (never raises).

    ``timeout`` is normally resolved once by the render pass. Direct callers may
    omit it and retain the safe call-time configuration lookup.

    Spawns pandoc DIRECTLY rather than through pypandoc's ``convert_text``: the
    high-level API hands back no process handle, and it sets no timeout, so a
    pathological body can spin the jira reader indefinitely (one corpus body ran
    13.5 minutes at 95.8% CPU).

    The timeout is enforced caller-side, and on expiry the whole process GROUP is
    reaped through the shared ``rebar._proc.reap_process_group``. A plain
    ``subprocess.run(timeout=...)`` would not do: it reaps only the DIRECT child,
    so a pipe-holding grandchild survives and keeps burning CPU (bug d843,
    bpo-30154). ``start_new_session=True`` is what makes the child a group leader
    so the reaper's ``killpg`` has a group to kill; the two go together.

    Note the ONE place this deliberately diverges from the ACLI caller it
    otherwise mirrors: acli reads nothing from stdin and is spawned with
    ``stdin=DEVNULL``, whereas pandoc reads the unit FROM stdin. Copying that
    kwarg would feed pandoc an empty document and silently degrade every unit, so
    the pipe is explicit here.
    """
    import subprocess  # local: keeps the module's import surface stdlib-light

    try:
        proc = subprocess.Popen(
            [pandoc, "-f", _PANDOC_FROM, "-t", _PANDOC_TO, *_PANDOC_ARGS],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError:
        logger.debug(_FALLBACK_EVENT, extra={"reason": "conversion_failure"})
        return None
    try:
        stdout, _stderr = proc.communicate(
            input=markdown,
            timeout=_pandoc_timeout() if timeout is None else timeout,
        )
    except subprocess.TimeoutExpired:
        _reap_pandoc(proc)
        logger.debug(_FALLBACK_EVENT, extra={"reason": "timeout"})
        return None
    except (OSError, subprocess.SubprocessError):
        _reap_pandoc(proc)
        logger.debug(_FALLBACK_EVENT, extra={"reason": "conversion_failure"})
        return None
    if proc.returncode != 0:
        logger.debug(_FALLBACK_EVENT, extra={"reason": "conversion_failure"})
        return None
    return _ANCHOR_RE.sub("", stdout).rstrip("\n")


def _reap_pandoc(proc: Any) -> None:
    """Reap a timed-out pandoc child AND its process group (bug d843).

    Thin wrapper over :func:`rebar._proc.reap_process_group` — the single source
    of truth for the SIGTERM -> grace -> SIGKILL -> bounded-drain, ESRCH/EPERM
    guarded, D-state-safe group reap already shared by the ACLI transport and the
    grounding harness. This caller pins only its own timing constants and log
    identity; any behaviour change belongs in the shared helper, not here.

    Imported function-locally, like this module's other non-stdlib imports. Never
    raises: a renderer that cannot reap must still fall back to Markdown rather
    than take the pass down.
    """
    try:
        from rebar._proc import reap_process_group

        reap_process_group(
            proc,
            grace=_PANDOC_GRACE_SECONDS,
            drain=_PANDOC_DRAIN_SECONDS,
            label="pandoc",
            logger=logger,
        )
    except Exception:  # noqa: BLE001 — reaping is best-effort; the fallback still stands
        logger.debug(_FALLBACK_EVENT, extra={"reason": "reap_failure"})


def render_markdown_to_wiki(markdown: str) -> str:
    """Render ``markdown`` to Jira wiki markup, converting only what is lossless.

    Structurally-safe units convert through pandoc; ASCII tables are wrapped once in
    ``{noformat}``; pre-existing Jira markup, raw HTML, HTML comments, code and
    regex-dense prose pass through byte-for-byte. Original order and spacing are
    preserved.

    Returns the input unchanged when pandoc is unavailable, and falls back per unit
    when a conversion fails or would not preserve that unit's code fragments.
    """
    if not markdown or not markdown.strip():
        return markdown

    pandoc = _pandoc_path()
    if pandoc is None:
        logger.warning(_FALLBACK_EVENT, extra={"reason": "pandoc_absent"})
        return markdown

    timeout = _pandoc_timeout()
    rendered: list[str] = []
    for kind, text in _lock_and_split(markdown):
        if kind == _TABLE:
            rendered.append("{noformat}\n" + text + "\n{noformat}")
        elif kind == _RENDER:
            rendered.append(_render_unit(text, pandoc, timeout))
        else:  # _EXACT and _BLANK both pass through untouched
            rendered.append(text)
    return "\n".join(rendered)


def _render_unit(text: str, pandoc: str, timeout: float | None = None) -> str:
    """Convert one unit with a pass timeout, falling back to source on any doubt."""
    converted = _convert(substitute_arrows(text), pandoc, timeout)
    if converted is None:
        return text
    expected = code_fragments(text)
    if expected:
        cursor = 0
        for fragment in expected:
            position = converted.find(fragment, cursor)
            if position < 0:
                logger.debug(_FALLBACK_EVENT, extra={"reason": "preservation_failure"})
                return text
            cursor = position + len(fragment)
    return converted
