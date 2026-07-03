"""Prompt + criteria library WRITE + structured-ENUMERATE data model (story B-DM).

This is the backend the visual editor's "select / create criteria & prompts" UX
(story B-UX) sits on. It is ADDITIVE over :mod:`rebar.llm.prompting.prompts` (the read/index
layer) and :mod:`rebar.llm.plan_review.registry` (criteria routing) — it does not
change their read or index behaviour.

Two surfaces:

* :func:`enumerate_library` — the authorable list for the editor's pickers. For each
  prompt AND criterion it returns ``{id, kind, title, description, inputs, outputs,
  is_overlay, source}`` (a superset of the read-only inspector data). Criteria are
  the prompts whose front-matter ``category == "plan-review-criterion"``; a
  criterion's overlay flag is sourced from the registry
  (:func:`rebar.llm.plan_review.registry.is_overlay`). It covers BOTH packaged
  reviewers and ``.rebar/prompts/<id>.md`` user entries (user wins on id clash,
  mirroring :func:`rebar.llm.prompting.prompts.get_prompt`'s resolution order).

* :func:`create_prompt` / :func:`update_prompt` — write a VALIDATED prompt-library
  ``.md`` (front-matter + body) to the writable user-override home
  ``.rebar/prompts/<id>.md``. The packaged ``reviewers/`` dir is READ-ONLY at runtime
  (it ships in the wheel and may be on a read-only mount), so authoring never writes
  there — and therefore never touches the committed ``reviewers/index.json``.

**Why the committed index is NOT regenerated on a user write (the drift-gate
contract).** ``reviewers/index.json`` is DERIVED from the *packaged* reviewer
front-matter only (:func:`rebar.llm.prompting.prompts.build_prompt_index` iterates the
packaged dir), and the CI drift gate diffs it after ``regenerate-index``. A
user-authored prompt lives outside the packaged dir, so it is correctly absent from
that committed artifact — the gate stays green precisely BECAUSE an authoring write
never dirties it. Freshly-authored entries are still immediately visible to the
editor because :func:`enumerate_library` MERGES the packaged set with a live scan of
``.rebar/prompts`` rather than reading them out of the committed index. (This is the
"enumerate merges two sources; user entries are not in the committed index"
resolution of the index-derivation design question.)

Stdlib-only at import (PyYAML stays lazy inside the prompts helpers).
"""

from __future__ import annotations

from pathlib import Path

from rebar.llm.prompting.prompts import (
    EXECUTION_MODES,
    PromptError,
    PromptNotFound,
    _packaged_prompt_files,
    _split_front_matter_raw,
    get_prompt,
    write_front_matter,
)

# A criterion is a prompt-library entry whose category is this EXPLICIT flag (mirrors
# how a reviewer is `category == "review"`). A criterion's id is its prompt id, of the
# form ``plan-review-<cid>`` (the registry's `_PROMPT_ID_PREFIX`); the bare ``<cid>``
# is what the registry's routing + `is_overlay` key off.
CRITERION_CATEGORY = "plan-review-criterion"
_CRITERION_PREFIX = "plan-review-"

# The id rule is the SINGLE source shared with the prompt-authoring write path
# (`prompt_authoring._valid_id`, epic drag-gripe-brake): `[A-Za-z0-9][A-Za-z0-9-]*` —
# alnum start then alnum/dash, forbidding `/`, `\`, `.`, `..`, and whitespace (so an
# authored id is a safe, traversal-free `.rebar/prompts/<id>.md` stem and can never
# masquerade as a `<id>.<variant>.md` overlay). Intentionally CASE-INSENSITIVE: criterion
# ids carry uppercase + digits (e.g. `plan-review-G1G2`, `plan-review-T5a`). `_validate_id`
# delegates to the shared validator so the two editor endpoints can never diverge.

# Front-matter keys an authored entry MUST carry (the picker-facing surface
# `enumerate_library` exposes). Kept minimal: the per-type richness the clarity gates
# reward is the author's concern, not this write-validator's.
_REQUIRED_KEYS: tuple[str, ...] = ("title", "description")


class LibraryWriteError(PromptError):
    """An authoring write was rejected by validation (bad id, malformed/missing
    front-matter, or an id collision). Subclasses :class:`PromptError` (hence
    ``LLMError``) so it surfaces cleanly across the library / CLI / MCP surfaces."""


class InvalidPromptIdError(LibraryWriteError):
    """The supplied id is empty, reserved, or otherwise not a legal library id (it
    would be unsafe as a ``.rebar/prompts/<id>.md`` filename component)."""


class PromptExistsError(LibraryWriteError):
    """:func:`create_prompt` was asked to create an id that already has a user entry
    at ``.rebar/prompts/<id>.md`` — use :func:`update_prompt` to modify it."""


# ── enumerate (the editor's picker source) ──────────────────────────────────────


def _user_prompt_ids(repo_root) -> set[str]:
    """The base prompt ids authored under ``.rebar/prompts`` (a ``<id>.<variant>.md``
    overlay is not an independent entry). The user filename stem IS the id (unlike the
    packaged files, user overrides are stored under the dash-form id verbatim)."""
    out: set[str] = set()
    pdir = Path(repo_root) / ".rebar" / "prompts"
    try:
        entries = sorted(pdir.glob("*.md"))
    except OSError:
        return out
    for path in entries:
        stem = path.name[:-3]
        if "." in stem:  # a `<id>.<variant>.md` overlay, not a base prompt
            continue
        out.add(stem)
    return out


def _is_overlay_criterion(prompt_id: str) -> bool:
    """Whether a criterion id is an overlay (Txx), per the registry's authority."""
    from rebar.llm.plan_review import registry

    cid = (
        prompt_id[len(_CRITERION_PREFIX) :]
        if prompt_id.startswith(_CRITERION_PREFIX)
        else prompt_id
    )
    return registry.is_overlay(cid)


def _entry_view(prompt_id: str, prompt, *, source: str) -> dict:
    """The authorable-entry shape the editor's pickers consume — a superset of the
    read-only inspector data (adds ``kind``/``is_overlay``/``source``)."""
    is_criterion = prompt.category == CRITERION_CATEGORY
    return {
        "id": prompt_id,
        "kind": "criterion" if is_criterion else "prompt",
        "title": prompt.title,
        "description": prompt.description,
        "inputs": prompt.inputs,
        "outputs": prompt.outputs,
        "execution_mode": prompt.execution_mode,
        "category": prompt.category,
        "is_overlay": _is_overlay_criterion(prompt_id) if is_criterion else False,
        "source": source,
    }


def enumerate_library(*, repo_root=None) -> list[dict]:
    """The authorable list of prompts + criteria for the editor's pickers.

    Returns one ``{id, kind, title, description, inputs, outputs, execution_mode,
    category, is_overlay, source}`` entry per BASE prompt across BOTH the packaged
    reviewers and (when
    ``repo_root`` is given) the ``.rebar/prompts`` user entries. A user entry that
    shares an id with a packaged one wins (``source == "user"``), mirroring
    :func:`get_prompt` resolution. ``kind`` is ``"criterion"`` for
    ``category == "plan-review-criterion"`` prompts, else ``"prompt"``; ``is_overlay``
    is the registry's overlay flag for criteria (always ``False`` for plain prompts).

    Best-effort: an unreadable / newer-schema entry is skipped rather than aborting
    the whole enumeration (the editor must still list every healthy entry)."""
    packaged = set(_packaged_prompt_files())
    user = _user_prompt_ids(repo_root) if repo_root else set()
    out: list[dict] = []
    for pid in sorted(packaged | user):
        try:
            prompt = get_prompt(pid, repo_root=repo_root)
        except PromptError:
            continue  # malformed / newer-schema entry — skip, keep enumerating
        source = "user" if pid in user else "packaged"
        out.append(_entry_view(pid, prompt, source=source))
    return out


def enumerate_criteria(*, repo_root=None) -> list[dict]:
    """The criteria-only view over :func:`enumerate_library` (a thin convenience for
    the editor's criteria picker).

    Caveat: a user-authored criterion is ENUMERABLE here but is NOT executed by the
    plan-review gate unless it is in the canonical registry AND ``criteria_routing.json``
    (routing derivation is out of scope for this write/enumerate layer)."""
    return [e for e in enumerate_library(repo_root=repo_root) if e["kind"] == "criterion"]


# ── create / update (the authoring write path) ──────────────────────────────────


def _validate_id(prompt_id: str) -> None:
    # Delegate to the SINGLE shared id rule (prompt_authoring._valid_id) so the
    # /prompt/save and /library/create endpoints can never diverge (epic drag-gripe-brake).
    from rebar.llm.workflow.prompt_authoring import _valid_id

    if not _valid_id(prompt_id):
        raise InvalidPromptIdError(
            f"invalid prompt id {prompt_id!r}: a library id must match "
            r"[A-Za-z0-9][A-Za-z0-9-]* (no '/', '\\', '.', '..', whitespace)"
        )


def _validate_and_canonicalize(text: str) -> str:
    """Parse + validate an authored ``.md`` and return its CANONICAL serialization.

    Raises :class:`PromptError` for malformed front-matter (bad YAML, a leading BOM,
    or a newer ``schema_version`` — via :func:`_split_front_matter_raw`) and
    :class:`LibraryWriteError` for missing required front-matter keys or an invalid
    ``execution_mode``."""
    # Fold CRLF/CR → LF before the front-matter check, matching `parse_front_matter`'s
    # normalization. `_split_front_matter_raw` (byte-preserving) does NOT fold, so a
    # CRLF-authored `.md` would otherwise fail the `\n`-anchored fence as "missing
    # front-matter"; `write_front_matter` re-emits the body LF-canonical regardless.
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    meta, body = _split_front_matter_raw(text)
    if not meta:
        raise LibraryWriteError(
            "missing required front-matter: an authored prompt must begin with a "
            f"'---' YAML block declaring at least {list(_REQUIRED_KEYS)}"
        )
    missing = [k for k in _REQUIRED_KEYS if not (isinstance(v := meta.get(k), str) and v.strip())]
    if missing:
        raise LibraryWriteError(
            f"missing required front-matter key(s) {missing} (each must be a non-empty string)"
        )
    mode = meta.get("execution_mode")
    if mode is not None and mode not in EXECUTION_MODES:
        raise LibraryWriteError(
            f"invalid execution_mode {mode!r}; it must be one of {EXECUTION_MODES}"
        )
    return write_front_matter(meta, body)


def _user_path(repo_root, prompt_id: str) -> Path:
    return Path(repo_root) / ".rebar" / "prompts" / f"{prompt_id}.md"


def _invalidate_caches() -> None:
    """Drop the registry's cached criteria/routing so a freshly-authored criterion is
    visible in-process without a restart (``load_catalog``/``get_prompt`` read the
    disk each call, so only the registry's ``lru_cache``s need clearing)."""
    from rebar.llm import criteria as _criteria
    from rebar.llm.plan_review import registry

    registry._routing_index.cache_clear()
    # The overlay-merged views are (gate_key, repo_root, overlay-signature)-keyed lru_caches —
    # clear them too so a freshly-authored criterion/overlay is visible in-process without a
    # restart (epic 3156). Since story 5065 the overlay merge lives in the SHARED
    # `rebar.llm.criteria` layer (clearing it covers BOTH gates); plan-review's descriptor memo
    # (`_load_criteria_cached`) stays gate-local. (An overlay EDIT self-invalidates via its
    # content signature; this clear covers a same-signature in-place authoring write.)
    _criteria.clear_caches()
    registry._load_criteria_cached.cache_clear()


def _write(prompt_id: str, text: str, *, repo_root) -> Path:
    # Persist through the SHARED atomic writer (prompt_authoring._atomic_write: a temp file
    # in the same directory + os.replace), NOT a bare path.write_text — so a crash/interrupt
    # mid-write can never leave a half-written `.rebar/prompts/<id>.md` (epic
    # drag-gripe-brake). Shared by create_prompt AND update_prompt, so both are atomic.
    # _atomic_write handles the parent mkdir + the atomic replace.
    from rebar.llm.workflow.prompt_authoring import _atomic_write

    canonical = _validate_and_canonicalize(text)
    path = _user_path(repo_root, prompt_id)
    _atomic_write(path, canonical)
    _invalidate_caches()
    return path


def create_prompt(prompt_id: str, text: str, *, repo_root) -> Path:
    """Create a NEW prompt-library entry at ``.rebar/prompts/<id>.md`` and return its
    path. ``text`` is the full ``.md`` (front-matter + body); it is validated and
    rewritten in canonical form.

    Raises :class:`InvalidPromptIdError` for a bad id, :class:`PromptExistsError` if a
    USER entry already exists at that id (use :func:`update_prompt` to modify it — note
    that authoring over a *packaged* id is allowed: it writes a user OVERRIDE), and
    :class:`PromptError`/:class:`LibraryWriteError` for malformed/missing front-matter.
    Does NOT touch the committed packaged ``reviewers/index.json`` (see module
    docstring)."""
    _validate_id(prompt_id)
    if _user_path(repo_root, prompt_id).is_file():
        raise PromptExistsError(
            f"prompt id {prompt_id!r} already exists at .rebar/prompts/{prompt_id}.md; "
            "use update_prompt to modify it"
        )
    return _write(prompt_id, text, repo_root=repo_root)


def update_prompt(prompt_id: str, text: str, *, repo_root) -> Path:
    """Modify an EXISTING user prompt-library entry at ``.rebar/prompts/<id>.md`` and
    return its path. Same validation as :func:`create_prompt`.

    Raises :class:`PromptNotFound` if no user entry exists at that id (create it first
    with :func:`create_prompt`)."""
    _validate_id(prompt_id)
    if not _user_path(repo_root, prompt_id).is_file():
        raise PromptNotFound(
            f"no user prompt entry at .rebar/prompts/{prompt_id}.md to update; "
            "use create_prompt to author it"
        )
    return _write(prompt_id, text, repo_root=repo_root)
