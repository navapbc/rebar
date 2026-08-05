"""Guide↔criterion PIN parity — the drift gate for criterion ids cited in PROSE guides.

WHY this exists (bug 828a). The packaged author-facing guides under ``rebar._guides``
(:data:`~rebar.llm.plan_review.registry.AUTHOR_GUIDES`) cite criterion ids inline — "name the
alternative you rejected (G6)" — as if the sentence were the criterion. It isn't: the
authoritative text is the criterion's rubric in the registry. In 828a the plan guide told
authors to name a rejected alternative while G6 had been written to explicitly NOT require one,
and the contradiction shipped for a month because NOTHING coupled the prose to the registry.
The pre-existing parity gate (``registry.validate_criteria_guide``) is scoped to the GENERATED
``docs/plan-review-criteria-guide.md``; a hand-written guide is invisible to it.

WHY a digest pin and not a content check. No deterministic gate can decide whether a sentence
faithfully paraphrases a rubric — that is a semantic judgement. What a gate CAN guarantee is
that a cited criterion never changes SILENTLY: pin a digest of each cited criterion's rendered
authoritative text, and when the criterion moves, the pin stops matching and CI fails with an
instruction to re-read the criterion, confirm the guide's claim still holds, and re-pin. The
gate converts an invisible semantic drift into a loud, cheap, human-decidable moment.

Design notes that matter:

* **The regenerated artifact is a DERIVED PIN MANIFEST, never the prose.** This reuses the
  repo's regenerate-in-place + parity-diff contract (``reviewers/index.json``,
  ``docs/plan-review-criteria-guide.md``), but nothing here ever rewrites a hand-authored
  guide — an author's words are their own.
* **Extraction depends on the id VOCABULARY only** — never on headings, fences, tables, or any
  other markup. Ordinary prose edits (reflowing a paragraph, moving a section) therefore cannot
  break the gate, and a citation in any surrounding syntax — ``(G6)`` or ``(`G6`)`` — is caught
  by construction, because a backtick is not a word character.
* **The pins are computed from the PACKAGED built-ins** (``load_criteria(repo_root="")``),
  exactly as :func:`registry.regenerate_criteria_guide` does, so a project's
  ``.rebar/criteria_routing.json`` overlay can never rotate the shipped pins and turn every
  downstream checkout red.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from . import registry

# The derived manifest lives BESIDE the guides it pins, inside the package, so an installed
# rebar can validate itself without a `docs/` tree (the same in-package data pattern as
# `criteria_routing.json` and the guides themselves).
PINS_FILENAME = "criterion-pins.json"

# Bumped when the manifest's SHAPE changes. An unexpected value is reported rather than
# mis-compared, so an old rebar reading a new manifest fails loudly instead of declaring
# every pin stale.
PINS_SCHEMA_VERSION = 1

_GUIDE_PACKAGE = "rebar._guides"

#: The command that rewrites the manifest — quoted in every problem message that a
#: regeneration would clear, so a failing CI log is self-service.
REGENERATE_COMMAND = "python -m rebar.llm.plan_review.guide_parity regenerate"


def cited_criteria(text: str, vocabulary: Iterable[str]) -> tuple[str, ...]:
    """The criterion ids from the CLOSED ``vocabulary`` that ``text`` cites, sorted + deduped.

    Word-bounded matching against a closed vocabulary is the whole trick: it needs NO structure
    in the guide (no heading convention, no fenced block, no citation syntax), so prose can be
    freely rewritten without breaking the gate, and an id-shaped token that is not a real
    criterion (``G9``, ``Z1``) is never reported — a typo'd citation cannot silently create a
    pin for a criterion that does not exist.

    The alternation is built LONGEST-FIRST because rebar's ids share prefixes (``T1`` vs
    ``T10``–``T14``, ``T5a``–``T5e``): with ``\\b`` on both sides ``T1`` can never match inside
    ``T10``, and the longest-first ordering makes the regex prefer ``T10`` where both could
    apply rather than leaving a truncated ``T1`` hit behind.
    """
    ids = sorted({v for v in vocabulary if v}, key=lambda v: (-len(v), v))
    if not ids:
        return ()
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(i) for i in ids) + r")\b")
    return tuple(sorted(set(pattern.findall(text))))


def criterion_digest(criterion: Mapping[str, Any]) -> str:
    """A stable sha256 hex digest over a criterion's RENDERED authoritative text.

    Rendering goes through :func:`registry._guide_section_body` — the exact text
    ``rebar explain <id>`` serves — so the pin covers the scenario/rubric body, the checklist,
    the posture, the exec tier, and the facet. Anything an author could have read when they
    wrote the sentence citing this criterion is inside the digest; if any of it moves, the pin
    stops matching. Digesting the rendered view (rather than the raw descriptor dict) also keeps
    the pin insensitive to incidental key-ordering or non-authoring metadata.
    """
    return hashlib.sha256(registry._guide_section_body(dict(criterion)).encode("utf-8")).hexdigest()


def compute_pins(
    guides: Mapping[str, str], criteria: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Build the pin manifest: ``{guide filename: {cited criterion id: digest}}``.

    ``guides`` maps a guide FILENAME (as in ``AUTHOR_GUIDES.values()``) to its text; ``criteria``
    maps criterion id → descriptor, and its keys ARE the extraction vocabulary. Pure and
    idempotent: identical inputs produce a byte-identical dict, with every mapping sorted, so
    the committed JSON never churns and a diff means real drift.
    """
    pinned: dict[str, dict[str, str]] = {}
    vocabulary = sorted(criteria)
    for filename in sorted(guides):
        cited = cited_criteria(guides[filename], vocabulary)
        pinned[filename] = {cid: criterion_digest(criteria[cid]) for cid in cited}
    return {"schema_version": PINS_SCHEMA_VERSION, "guides": pinned}


def diff_pins(
    pins: Mapping[str, Any] | None,
    guides: Mapping[str, str],
    criteria: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Compare a committed manifest against the live guides + registry; return problems.

    An empty list means in sync. Five failure classes, each naming the criterion id AND the
    guide it concerns so a CI log points straight at the sentence to re-read:

    * ``stale`` — cited, present, but the criterion's text CHANGED under the prose. This is the
      828a class and the reason the gate exists; clearing it requires a human to re-read the
      criterion and confirm the guide's claim still holds before re-pinning.
    * ``unpinned`` — a citation was added without regenerating, so it is unguarded.
    * ``retired`` — a pinned id left the registry; the GUIDE still cites it, so the guide (not
      the manifest) is what must change.
    * ``orphan`` — a pin outlived its citation; prune it so the manifest stays a true index.
    * missing manifest — nothing to compare against; the gate would otherwise pass vacuously.
    """
    if pins is None:
        return [
            f"guide criterion-pin manifest is missing ({PINS_FILENAME}); "
            f"regenerate it with `{REGENERATE_COMMAND}`"
        ]
    version = pins.get("schema_version")
    if version != PINS_SCHEMA_VERSION:
        return [
            f"guide criterion-pin manifest has schema_version {version!r}, "
            f"expected {PINS_SCHEMA_VERSION} — refusing to compare pins that may mean something "
            f"else; upgrade rebar or rebuild the manifest with `{REGENERATE_COMMAND}`"
        ]

    pinned_guides = pins.get("guides") or {}
    problems: list[str] = []
    vocabulary = sorted(criteria)
    for filename in sorted(set(guides) | set(pinned_guides)):
        text = guides.get(filename, "")
        cited = set(cited_criteria(text, vocabulary)) if filename in guides else set()
        pinned: Mapping[str, Any] = pinned_guides.get(filename) or {}
        for cid in sorted(cited):
            expected = criterion_digest(criteria[cid])
            if cid not in pinned:
                problems.append(
                    f"criterion {cid!r} is cited by {filename} but is unpinned — the citation is "
                    f"unguarded against criterion drift; run `{REGENERATE_COMMAND}`"
                )
            elif pinned[cid] != expected:
                problems.append(
                    f"criterion {cid!r} pinned by {filename} is stale — the criterion's "
                    f"authoritative text changed since the guide cited it. Re-read the criterion "
                    f"(`rebar explain {cid}`), confirm the guide's claim about it still holds "
                    f"(fix the prose if not), then regenerate the pins with "
                    f"`{REGENERATE_COMMAND}`"
                )
        for cid in sorted(pinned):
            if cid not in criteria:
                problems.append(
                    f"criterion {cid!r} pinned by {filename} is retired — it is no longer in the "
                    f"criteria registry, so the GUIDE must be updated to stop citing it "
                    f"(renamed? removed?) before the pin can be rebuilt"
                )
            elif cid not in cited:
                problems.append(
                    f"criterion {cid!r} pinned by {filename} is an orphan — the guide no longer "
                    f"cites it, so prune the pin; run `{REGENERATE_COMMAND}`"
                )
    return problems


def _pins_path() -> Path:
    """Filesystem path of the packaged manifest (mirrors ``prompts.regenerate_prompt_index``,
    which likewise resolves an in-package data file through ``importlib.resources`` and then
    writes through it — correct for a source checkout, which is the only place regeneration
    is ever run)."""
    return Path(str(resources.files(_GUIDE_PACKAGE) / PINS_FILENAME))


def _real_guides() -> dict[str, str]:
    """The REAL packaged guides as ``{filename: text}``, read through
    :func:`registry.explain_guide` so this gate and ``rebar explain`` can never disagree about
    what the shipped prose says. Keyed by FILENAME (not the short name) so the manifest is
    readable as an index of the files it pins."""
    return {
        filename: registry.explain_guide(name) for name, filename in registry.AUTHOR_GUIDES.items()
    }


def _real_criteria() -> dict[str, dict[str, Any]]:
    """The PACKAGED built-in criteria as ``{id: descriptor}``. ``repo_root=""`` deliberately
    excludes any project ``.rebar/criteria_routing.json`` overlay — exactly as
    :func:`registry.regenerate_criteria_guide` does — so a downstream project's re-tuning can
    never rotate the digests committed in rebar's own tree."""
    return {c["id"]: c for c in registry.load_criteria(repo_root="")}


def load_guide_pins() -> dict[str, Any] | None:
    """Read the packaged pin manifest, or ``None`` when it is absent (which :func:`diff_pins`
    reports as a problem rather than a vacuous pass)."""
    try:
        raw = (resources.files(_GUIDE_PACKAGE) / PINS_FILENAME).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None
    loaded: dict[str, Any] = json.loads(raw)
    return loaded


def build_guide_pins() -> dict[str, Any]:
    """:func:`compute_pins` over the REAL packaged guides and the REAL packaged registry,
    plus the ``_generated_by`` marker banner (ticket 9100).

    JSON has no comment syntax, so this reserved key is how the committed manifest
    announces it is derived. ``compute_pins`` itself stays marker-free — its own unit
    tests compare its output directly — and :func:`diff_pins` reads only
    ``schema_version``/``guides``, so the extra key is inert for the parity gate."""
    return {
        "_generated_by": (
            "GENERATED from digests of the criteria cited by the `src/rebar/_guides/*.md` "
            "prose guides — do not edit by hand. Regenerate with "
            f"`{REGENERATE_COMMAND}`; a CI drift gate fails the build if this file is stale."
        ),
        **compute_pins(_real_guides(), _real_criteria()),
    }


def regenerate_guide_criterion_pins() -> str:
    """Write the canonical pin manifest into ``rebar._guides`` and return the path written.

    Canonical JSON (sorted keys, 2-space indent, trailing newline) keeps the committed file
    byte-stable, so a regeneration that changes nothing produces an EMPTY diff — the property
    the regenerate-in-place + parity-diff contract depends on.
    """
    text = json.dumps(build_guide_pins(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path = _pins_path()
    path.write_text(text, encoding="utf-8")
    return str(path)


def validate_guide_criterion_pins() -> list[str]:
    """The gate entry point: problems (empty == in sync) for the committed manifest against the
    packaged guides + registry. Folded into ``registry`` 's ``validate-routing`` command so it
    runs wherever the existing criteria parity gate already runs."""
    return diff_pins(load_guide_pins(), _real_guides(), _real_criteria())


def _main(argv: list[str] | None = None) -> int:
    """``python -m rebar.llm.plan_review.guide_parity regenerate|validate``."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else ""
    if cmd == "regenerate":
        print(f"wrote {regenerate_guide_criterion_pins()}")  # noqa: T201
        return 0
    if cmd == "validate":
        problems = validate_guide_criterion_pins()
        if problems:
            print("guide criterion-pin parity gate FAILED:", file=sys.stderr)  # noqa: T201
            for p in problems:
                print(f"  - {p}", file=sys.stderr)  # noqa: T201
            return 1
        pins = load_guide_pins() or {}
        count = sum(len(v) for v in (pins.get("guides") or {}).values())
        print(f"guide criterion-pin parity gate: OK ({count} pinned citations in sync).")  # noqa: T201
        return 0
    print(  # noqa: T201
        "usage: python -m rebar.llm.plan_review.guide_parity regenerate | validate",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
