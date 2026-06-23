"""Prompt authoring backbone for the visual editor (story 6592).

The editor's prompt LIBRARY + CREATE/EDIT half: list every available prompt, work
out WHERE a created/edited prompt would be written (without writing), and persist it
atomically through the canonical front-matter writer. Kept as a focused module
(imported by ``editor.py``) so the editor's HTTP handler stays thin and these pure
functions are unit-testable without a browser.

Two write targets, AUTO-DETECTED (never asked of the user):

  * **packaged** — a writable ``nava-rebar`` SOURCE CHECKOUT (``pyproject.toml`` with
    ``[project].name == "nava-rebar"`` AND a writable packaged ``reviewers/`` dir):
    the prompt is a first-class built-in, so it is written to the packaged ``.md`` and
    the derived ``reviewers/index.json`` is regenerated (the index is never left stale).
  * **project** — otherwise, a writable (or creatable) ``<repo>/.rebar/prompts/`` dir:
    the prompt is a PROJECT override at ``.rebar/prompts/<id>.md`` (this does NOT touch
    the packaged index — a project override is not part of the packaged catalog).
  * **none** — neither location is writable: :func:`save_prompt` refuses with the reason.

Every write is ATOMIC (write a temp file in the SAME directory, then ``os.replace``),
so a failed write can never leave a half-written or corrupted prompt on disk — the
original (if any) is untouched until the rename succeeds.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from rebar.llm.prompts import (
    PromptError,
    _catalog_dir,
    _packaged_prompt_files,
    get_prompt,
    regenerate_prompt_index,
    write_front_matter,
)

# A safe prompt-id slug: lowercase alnum start, then alnum/dash. This is the file
# stem used for `.rebar/prompts/<id>.md` and the packaged `<id>.md`, so it must be a
# safe, traversal-free, lower-kebab token (matching the existing built-in ids).
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# The CLOSED category vocabulary the palette/library groups by (mirrors the prompt
# model). A prompt with no/unknown category is grouped under "uncategorized" by the UI.
CATEGORIES: tuple[str, ...] = ("review", "verifier", "transform", "code", "exploration")


class PromptWriteError(PromptError):
    """A created/edited prompt could not be persisted: an invalid/empty id, an id
    COLLISION on create-new without ``overwrite``, or NEITHER write location being
    writable. A typed error (subclasses :class:`PromptError`) so the editor endpoint
    surfaces a clear 4xx rather than an opaque traceback — and because the write is
    atomic, a refusal NEVER leaves a corrupted file behind."""


def _valid_id(prompt_id: str) -> bool:
    return bool(prompt_id) and bool(_SLUG.match(prompt_id))


def _project_prompt_dir(repo_root: Any) -> Path | None:
    if not repo_root:
        return None
    return Path(repo_root) / ".rebar" / "prompts"


def _project_prompts(repo_root: Any) -> dict[str, Path]:
    """Map each project prompt id → its ``.rebar/prompts/<id>.md`` file (BASE prompts
    only; a ``<id>.<variant>.md`` overlay is not an independent prompt)."""
    out: dict[str, Path] = {}
    pdir = _project_prompt_dir(repo_root)
    if not pdir:
        return out
    try:
        entries = sorted(pdir.glob("*.md"))
    except OSError:
        return out
    for path in entries:
        stem = path.name[:-3]
        if "." in stem:  # a `<id>.<variant>.md` overlay, not a base prompt
            continue
        out[stem.replace("_", "-")] = path
    return out


def _summary(prompt_id: str, *, repo_root: Any, source: str) -> dict[str, Any]:
    """The library summary row for one prompt id, read via the unified resolver so a
    project override is reflected. Best-effort: an unreadable prompt degrades to a
    minimal row (id + source) rather than failing the whole listing."""
    row: dict[str, Any] = {
        "id": prompt_id,
        "title": "",
        "category": None,
        "is_reviewer": False,
        "source": source,
        "inputs": None,
        "outputs": None,
        "description": "",
    }
    try:
        p = get_prompt(prompt_id, repo_root=repo_root if source == "project" else None)
        row.update(
            title=p.title,
            category=p.category,
            is_reviewer=p.is_reviewer,
            inputs=p.inputs,
            outputs=p.outputs,
            description=p.description,
        )
    except Exception:  # noqa: BLE001 - a malformed prompt still lists (minimal row), never crashes
        pass
    return row


def list_prompts(repo_root: Any = None) -> list[dict[str, Any]]:
    """Every available prompt for the editor's library: built-ins (the packaged
    ``reviewers/*.md``) PLUS project prompts (``.rebar/prompts/*.md``).

    Each row is ``{id, title, category, is_reviewer, source, inputs, outputs,
    description}``. A project override of a built-in id appears ONCE, marked
    ``source="project"`` (the override wins). Rows are sorted by id; group by the CLOSED
    :data:`CATEGORIES` vocabulary in the UI."""
    project = _project_prompts(repo_root)
    rows: dict[str, dict[str, Any]] = {}
    for pid in _packaged_prompt_files():
        rows[pid] = _summary(pid, repo_root=None, source="builtin")
    for pid in project:  # a project override of a built-in id wins (source="project")
        rows[pid] = _summary(pid, repo_root=repo_root, source="project")
    return [rows[pid] for pid in sorted(rows)]


def _is_nava_rebar_checkout(repo_root: Any) -> bool:
    """True iff ``<repo>/pyproject.toml`` exists and declares
    ``[project].name == "nava-rebar"`` — i.e. this is the rebar source tree itself
    (where editing the PACKAGED prompt is the right write-back)."""
    if not repo_root:
        return False
    pyproject = Path(repo_root) / "pyproject.toml"
    try:
        import tomllib

        with open(pyproject, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):  # missing / malformed pyproject → not a checkout
        return False
    return data.get("project", {}).get("name") == "nava-rebar"


def _packaged_dir() -> Path:
    """The packaged ``reviewers/`` directory as a real filesystem path (the editor only
    runs against an installed/source tree, so this resolves to a concrete path)."""
    return Path(str(_catalog_dir()))


def _dir_writable_or_creatable(d: Path) -> bool:
    """Whether ``d`` is a writable directory, or can be created (its nearest existing
    ancestor is writable). Used to decide the project-override target without writing."""
    probe = d
    while True:
        if probe.exists():
            return os.access(probe, os.W_OK) if probe.is_dir() else False
        parent = probe.parent
        if parent == probe:  # reached the filesystem root without an existing dir
            return False
        probe = parent


def prompt_write_target(prompt_id: str, repo_root: Any = None) -> dict[str, Any]:
    """Auto-detect WHERE a created/edited ``prompt_id`` would be written — WITHOUT
    writing anything.

    Returns ``{kind, path, writable, reason}`` where ``kind`` is:

      * ``"packaged"`` — a writable ``nava-rebar`` source checkout: target the packaged
        ``reviewers/<id with _>.md`` (the prompt becomes a first-class built-in);
      * ``"project"`` — else a writable/creatable ``<repo>/.rebar/prompts/``: target
        ``.rebar/prompts/<id>.md`` (a project override);
      * ``"none"`` — neither location writable: ``writable=False`` + a clear reason.

    The path is shown to the user BEFORE Save (so the write location is never a
    surprise)."""
    pkg_dir = _packaged_dir()
    if _is_nava_rebar_checkout(repo_root) and pkg_dir.is_dir() and os.access(pkg_dir, os.W_OK):
        path = pkg_dir / f"{prompt_id.replace('-', '_')}.md"
        return {
            "kind": "packaged",
            "path": str(path),
            "writable": True,
            "reason": "writable nava-rebar source checkout: writing the packaged built-in prompt",
        }
    pdir = _project_prompt_dir(repo_root)
    if pdir is not None and _dir_writable_or_creatable(pdir):
        return {
            "kind": "project",
            "path": str(pdir / f"{prompt_id}.md"),
            "writable": True,
            "reason": f"writing a project override at {pdir}/{prompt_id}.md",
        }
    reason = (
        "neither location is writable: not a writable nava-rebar source checkout, and "
        f"the project prompt dir ({pdir if pdir is not None else '<no repo_root>'}) is not "
        "writable/creatable"
    )
    return {"kind": "none", "path": None, "writable": False, "reason": reason}


def _prompt_exists(prompt_id: str, repo_root: Any, target: dict[str, Any]) -> bool:
    """Whether a prompt with this id already exists at EITHER write location (so a
    create-new collision is caught regardless of which target was resolved)."""
    if prompt_id in _packaged_prompt_files():
        return True
    if prompt_id in _project_prompts(repo_root):
        return True
    tpath = target.get("path")
    return bool(tpath) and Path(tpath).exists()


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` ATOMICALLY: a temp file in the SAME directory then
    ``os.replace`` (an atomic rename on the same filesystem). A failure before the
    rename leaves the original file untouched; the temp file is cleaned up."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)  # atomic on POSIX/Windows (same dir = same filesystem)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_prompt(
    prompt_id: str,
    meta: dict[str, Any],
    body: str,
    repo_root: Any = None,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Persist a created/edited prompt, ATOMICALLY, through the canonical writer.

    Body+front-matter are serialized by :func:`write_front_matter` (LF, schema_version
    stamped, body byte-for-byte) and written via a temp-file + ``os.replace``. The
    target is :func:`prompt_write_target`. After a PACKAGED write the derived index is
    regenerated (a project override does not change the packaged index, so regen is
    skipped there).

    Returns ``{path, kind, regenerated_index}``. Raises :class:`PromptWriteError` on the
    non-happy paths (each leaves any existing file intact):

      * invalid/empty id (must match ``^[a-z0-9][a-z0-9-]*$``);
      * create-new id COLLISION when ``overwrite=False``;
      * NEITHER location writable (carries the ``prompt_write_target`` reason)."""
    if not _valid_id(prompt_id):
        raise PromptWriteError(
            f"invalid prompt id {prompt_id!r}: must match ^[a-z0-9][a-z0-9-]*$ "
            "(lowercase letters/digits/dashes, not starting with a dash)"
        )
    target = prompt_write_target(prompt_id, repo_root=repo_root)
    if not target["writable"]:
        raise PromptWriteError(target["reason"])
    if not overwrite and _prompt_exists(prompt_id, repo_root, target):
        raise PromptWriteError(
            f"a prompt with id {prompt_id!r} already exists; pass overwrite=true to confirm "
            "replacing it"
        )
    text = write_front_matter(dict(meta or {}), body)
    path = Path(target["path"])
    _atomic_write(path, text)
    regenerated = False
    if target["kind"] == "packaged":
        # Keep the derived index in lockstep with the packaged tree (a new/edited
        # built-in must appear / drift-free). A project override never touches it.
        regenerate_prompt_index(repo_root=repo_root)
        regenerated = True
    return {"path": str(path), "kind": target["kind"], "regenerated_index": regenerated}
