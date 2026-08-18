"""Byte-preserving persistence owner for binding state (RP-02 S1).

``BindingRepository`` is the SOLE owner of the four files the Jira binding subsystem
keeps on disk:

* live      ``<tracker_dir>/.bridge_state/bindings.json``
* retired   ``<tracker_dir>/.bridge_state/bindings-retired.json``
* rotation  ``<tracker_dir>/.bridge_state/get_rotation.json`` (the GET-rotation sidecar)
* alerts    ``<repo_root>/bridge_state/bridge_alerts/*.jsonl`` (best-effort lifecycle log)

It owns *persistence only*. Lifecycle policy — bind/confirm/retire/tombstone/comment
bookkeeping — stays with its current owner, which mutates the dictionaries this class
hands out and then calls :meth:`save`. That is why the views below are OPEN views over
the ORIGINAL dictionaries rather than copies: a defensive copy here would silently
discard every in-place write those owners make.

Three deliberate asymmetries, each load-bearing:

* **The live store fails CLOSED.** An unparseable ``bindings.json`` (typically git
  conflict markers from a tickets-branch merge) raises rather than degrading to an
  empty store. An empty store would report every ticket as unbound, so the very next
  outbound pass would re-create every issue in Jira — an irreversible mass-duplicate
  write. Aborting the pass is reversible; duplicating Jira issues is not.
* **The retired store fails OPEN.** A retired binding wrongly treated as live costs one
  wasted GET (it re-404s and re-retires), never a duplicate write, so a corrupt retired
  file degrades to an empty set plus a deduped alert.
* **The rotation sidecar fails OPEN.** Rotation is an optimization; losing its history
  costs a bounded extra GET. A failed sidecar write must not abort the save, and the
  legacy inline ``last_get_pass`` floor is then RETAINED — it is only scrubbed from the
  live entries once the sidecar durably holds the stamp.

Alerting is best-effort by the same logic: any failure inside :meth:`alert` is swallowed
so observability can never break a sync pass.

Deliberately NOT claimed: multi-file atomicity, a journal or write-ahead log, format
migration, an eager rewrite on load, deep-copied views, tickets-branch publication, or
write elision. :meth:`save` is UNCONDITIONAL — it always attempts rotation persistence
before replacing live state, even when nothing changed in memory.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from rebar_reconciler import get_rotation

__all__ = ["BindingRepository"]

#: The shape a first-ever pass starts from. Deep-copied per construction (never handed
#: out by reference) so one repository's in-place mutations cannot leak into the next.
_EMPTY_STORE: dict[str, Any] = {
    "version": 2,
    "bindings": {},
    "reverse": {},
    # Append-only comment-sync map, local_comment_key(HLC) -> Jira comment ID.
    "comment_ids": {},
}

#: Version stamp written into the retired-binding file's payload envelope.
_RETIRED_VERSION = 1

#: Temp-file prefixes for the atomic writes. Pinned because operators and the
#: leftover-temp cleanup oracle recognize state by these names.
_LIVE_TEMP_PREFIX = "bindings_"
_RETIRED_TEMP_PREFIX = "bindings_retired_"


def _corrupt_live_message(path: Path, exc: BaseException) -> str:
    """Compose the fail-CLOSED abort message for an unparseable live store.

    It names the offending file and the exact ``git show`` route back, because only the
    operator can decide how to resolve a merge conflict on the tickets branch — the
    reconciler deliberately refuses to guess, since guessing wrong writes duplicate
    Jira issues.
    """
    return (
        f"bindings.json is corrupt or contains git conflict markers "
        f"and cannot be parsed — aborting reconcile pass to prevent "
        f"duplicate Jira mutations. File: {path}. "
        f"Original error: {exc}. "
        f"Recovery: resolve the merge conflict or restore the file "
        f"from the tickets branch with: "
        f"git show tickets:.tickets-tracker/.bridge_state/bindings.json"  # tickets-boundary-ok
    )


def _write_json_atomically(path: Path, payload: Any, *, prefix: str) -> None:
    """Replace ``path`` with ``payload`` atomically, in the exact committed byte form.

    ``tempfile.mkstemp`` in the DESTINATION directory plus ``os.replace`` — same
    filesystem, so the rename is atomic and a crash mid-write can never leave a
    truncated binding file behind. The serialization (``indent=2``, ``sort_keys=True``,
    single trailing newline) is a contract, not a preference: these files are committed
    to the tickets branch and diffed, so stable key order keeps the diffs reviewable.

    The temp file is unlinked on ANY failure (including ``KeyboardInterrupt`` /
    ``SystemExit``, hence ``BaseException``) so an aborted pass leaves no litter beside
    the real state; the error then propagates — these writes fail CLOSED.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class BindingRepository:
    """Sole owner of the live, retired, rotation, and alert binding-state files."""

    def __init__(self, tracker_dir: Path) -> None:
        """Load binding state from ``tracker_dir``. READS ONLY — never writes.

        Construction must not materialize or rewrite anything: an eager rewrite would
        churn the tickets branch on every pass and, worse, would re-serialize a store
        that a newer rebar may have written with fields this build does not understand,
        before a single thing has changed. ``.bridge_state`` is therefore not created
        here either; the first :meth:`save` creates it.

        Load order matters. The rotation sidecar is read and then merged with the legacy
        inline stamps found in the live store, so a store written before the sidecar
        existed still contributes its rotation floor. The retired-locals reverse index
        ({local_id: jira_key}) is derived once here so the outbound differ can ask "was
        THIS local ticket confirmed-deleted?" without re-reading the retired file per
        unbound ticket.
        """
        self._path = tracker_dir / ".bridge_state" / "bindings.json"
        self._retired_path = self._path.with_name("bindings-retired.json")
        self._rotation_path = self._path.with_name("get_rotation.json")
        # Lifecycle alerts live beside the repo, not inside the tracker; tracker_dir is
        # ``<repo_root>/.tickets-tracker``.  # tickets-boundary-ok
        self._repo_root = tracker_dir.parent
        self._data = self._load()
        self._rotation = get_rotation.load(self._rotation_path)
        get_rotation.merge_legacy(self._rotation, self._data["bindings"])
        self._retired: set[str] = self._load_retired()
        # A legacy list-form retired file carries no entries, so this degrades to empty
        # rather than failing — the key set is still available via retired_keys().
        self._retired_locals: dict[str, str] = {
            str(entry["local_id"]): key
            for key, entry in self.retired_entries().items()
            if isinstance(entry, dict) and entry.get("local_id")
        }

    # -- paths -------------------------------------------------------------

    @property
    def path(self) -> Path:
        """The live ``bindings.json``."""
        return self._path

    @property
    def retired_path(self) -> Path:
        """The sibling ``bindings-retired.json`` (soft-deleted bindings)."""
        return self._retired_path

    @property
    def rotation_path(self) -> Path:
        """The ``get_rotation.json`` sidecar.

        Separate from the live store on purpose: advancing a small GET-rotation cursor
        must not rewrite every binding entry.
        """
        return self._rotation_path

    @property
    def repo_root(self) -> Path:
        """The repository root that lifecycle alerts are keyed off."""
        return self._repo_root

    # -- open views over the ORIGINAL dictionaries -------------------------

    @property
    def data(self) -> dict[str, Any]:
        """The whole loaded document, including fields this build does not recognize.

        Handed out by reference. Unknown top-level and per-entry keys are carried
        through untouched so a load/save round trip is byte-identical — the file is
        shared with other writers, and dropping what we do not understand would corrupt
        their state.
        """
        return self._data

    @property
    def bindings(self) -> dict[str, Any]:
        """The ``{local_id: entry}`` map — the SAME object as ``data["bindings"]``."""
        return self._data["bindings"]

    @property
    def reverse(self) -> dict[str, Any]:
        """The ``{jira_key: local_id}`` index — the SAME object as ``data["reverse"]``."""
        return self._data["reverse"]

    @property
    def rotation(self) -> dict[str, str]:
        """The in-memory ``{jira_key: pass_id}`` rotation stamps (an open view)."""
        return self._rotation

    # -- retired state -----------------------------------------------------

    def retired_keys(self) -> set[str]:
        """The retired (soft-deleted) Jira keys, as read at construction.

        The live set object, kept in lock-step by whoever performs a retire/unretire —
        not a per-call re-read, so a tombstone check costs nothing.
        """
        return self._retired

    def retired_locals(self) -> dict[str, str]:
        """The retired file's ``{local_id: jira_key}`` reverse index, as read at load.

        Retirement UNBINDS the local ticket, so by the time the outbound differ sees it
        there is no live key to look up; this index is the only thing that distinguishes
        "was paired with a confirmed-deleted issue" from "never bound at all".
        """
        return self._retired_locals

    def retired_entries(self) -> dict[str, Any]:
        """Read the retired file's full ``{jira_key: entry}`` map. FAIL-OPEN.

        Re-read from disk so a caller about to rewrite the file works from its current
        contents. An absent file, a corrupt file, or a legacy list-form ``retired``
        value all yield ``{}`` rather than raising: see the module docstring for why
        retired state degrades instead of aborting. (Unlike :meth:`_load_retired`, this
        read is silent — the alert is raised once, at load.)
        """
        if not self._retired_path.exists():
            return {}
        try:
            with open(self._retired_path, encoding="utf-8") as handle:
                data = json.load(handle)
            retired = data.get("retired", {})
            return retired if isinstance(retired, dict) else {}
        except (json.JSONDecodeError, ValueError, OSError):
            return {}

    def save_retired(self, entries: dict[str, Any]) -> None:
        """Atomically persist the retired-binding map. FAILS CLOSED (raises).

        Retirement is reversible only because the entry survives in this file, so a
        silently-lost write would make a soft delete indistinguishable from a hard one.
        """
        _write_json_atomically(
            self._retired_path,
            {"version": _RETIRED_VERSION, "retired": entries},
            prefix=_RETIRED_TEMP_PREFIX,
        )

    # -- unit of work ------------------------------------------------------

    def save(self) -> None:
        """Persist rotation FIRST, then atomically replace the live store.

        Unconditional: no dirty-gating, no write elision. The ordering is the contract.
        The legacy inline ``last_get_pass`` stamp is the rotation floor for readers that
        predate the sidecar, so it may only be scrubbed from the live entries once the
        sidecar write has *durably* taken it — ``get_rotation.save`` returning ``False``
        means it failed open, and dropping the inline stamp then would lose the floor
        entirely and re-GET everything. The live replacement happens either way; a
        rotation failure never aborts the save.

        The live write itself fails CLOSED (raises), because a lost binding write is
        exactly what makes the next pass create duplicate Jira issues.
        """
        if get_rotation.save(self._rotation_path, self._rotation):
            for entry in self._data["bindings"].values():
                entry.pop("last_get_pass", None)
        _write_json_atomically(self._path, self._data, prefix=_LIVE_TEMP_PREFIX)

    def alert(self, key: str, record: dict[str, Any]) -> None:
        """Append a deduped lifecycle alert to ``bridge_alerts``. BEST-EFFORT.

        ``key`` is the dedup identity (an operator resolves one alert, not a hundred);
        the stored record carries it alongside ``resolved: False``, which is what the
        dedup window and the operator surfaces read.

        ``alert_store`` is loaded lazily BY FILE PATH so this module stays importable in
        isolation (the reconciler test tree loads modules via
        ``spec_from_file_location``). Every failure in here — a missing module, an
        unwritable alerts directory, a full disk — is swallowed: alerting is
        observability, and observability must never break a sync pass.
        """
        try:
            import importlib.util as _ilu

            alert_path = Path(__file__).parent / "alert_store.py"
            spec = _ilu.spec_from_file_location("rebar_reconciler.alert_store", alert_path)
            if spec is None or spec.loader is None:
                return
            alert_mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(alert_mod)
            full_record = {**record, "key": key, "resolved": False}
            if not alert_mod.is_deduped(key, self._repo_root):
                alert_mod.append(full_record, self._repo_root)
        except Exception:  # noqa: BLE001 — alerting is best-effort
            pass

    # -- loading -----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        """Load the live store. FAILS CLOSED on corruption.

        An absent file is a legitimate first-ever pass and yields a fresh empty store
        (deep-copied from the template). An UNPARSEABLE file is not: degrading it to an
        empty store would report every ticket as unbound and mass-duplicate them in
        Jira, so this raises ``ValueError`` naming the file and the recovery route.
        """
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as handle:
                    loaded: dict[str, Any] = json.load(handle)
                    return loaded
            except (json.JSONDecodeError, ValueError, OSError) as exc:
                raise ValueError(_corrupt_live_message(self._path, exc)) from exc
        return json.loads(json.dumps(_EMPTY_STORE))  # deep copy

    def _load_retired(self) -> set[str]:
        """Load the retired-binding key set. FAILS OPEN, with a deduped alert.

        Contrast :meth:`_load`: a retired binding wrongly treated as live costs one
        wasted GET (it re-404s and re-retires), never a duplicate write — so corruption
        degrades to an empty set here instead of aborting the pass. The alert is what
        keeps that degradation visible, since an empty retired set is otherwise
        indistinguishable from "nothing has ever been retired".

        A legacy list-form ``retired`` value still yields its key set (an additive read
        of the pre-entry-map format).
        """
        if not self._retired_path.exists():
            return set()
        try:
            with open(self._retired_path, encoding="utf-8") as handle:
                data = json.load(handle)
            retired = data.get("retired", {})
            if isinstance(retired, dict):
                return set(retired.keys())
            if isinstance(retired, list):
                return set(retired)
            return set()
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            self.alert(
                key="retired-file-corrupt",
                record={
                    "kind": "binding-retired-file-corrupt",
                    "path": str(self._retired_path),
                    "error": repr(exc),
                },
            )
            return set()
