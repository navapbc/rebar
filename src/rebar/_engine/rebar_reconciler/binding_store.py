"""Local binding store for Jira bidirectional sync.

Maps local ticket IDs ↔ Jira issue keys.  Persisted as JSON at
`.tickets-tracker/.bridge_state/bindings.json` on the tickets branch.  # tickets-boundary-ok

Write-ahead protocol
--------------------
1. bind_pending(local_id)          — mark outbound create in-flight
2. Jira client.create_issue(...)   — obtain DIG-NNNN
3. Jira client.add_label / set_entity_property — plant rebar-id marker
4. bind_confirm(local_id, jira_key) — finalise binding
5. save()                          — atomic persist

Recovery (next pass startup): recover_pending_bindings(client) searches
Jira for the rebar-id label and either confirms or unbinds each pending
entry.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_EMPTY_STORE: dict[str, Any] = {
    "version": 1,
    "bindings": {},
    "reverse": {},
}

# Bug 1e08-1a35-0267-4ca6 — binding lifecycle (GC) defaults. These are the
# reconciler's only int-valued binding env vars; parsed defensively below so a
# typo'd ops value degrades to the default rather than aborting the pass
# (mirrors applier.py's best-effort _get_rebar_id_guard_mode_from_config).
_DEFAULT_ABSENT_RETIRE_GRACE = 3


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Parse an int env var defensively: malformed → default; clamp >= minimum.

    The reconciler has no dotted-config reader, so lifecycle knobs are env
    vars (matches fetcher.py / applier.py). A typo'd value (e.g. ``"abc"``)
    must NOT abort the pass — fall back to the documented default.
    """
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except (ValueError, TypeError):
            value = default
    if minimum is not None and value < minimum:
        value = minimum
    return value


class BindingStore:
    """Bidirectional local-id ↔ jira-key binding store.

    All mutations are in-memory until ``save()`` is called.
    ``save()`` uses tempfile + ``os.replace`` for atomic writes.
    """

    def __init__(self, tracker_dir: Path) -> None:
        self._path = tracker_dir / ".bridge_state" / "bindings.json"
        # Bug 1e08: retired (soft-deleted) bindings live in a sibling file so
        # the live store stays clean and retirement is reversible.
        self._retired_path = tracker_dir / ".bridge_state" / "bindings-retired.json"
        # repo_root is needed so lifecycle alerts (binding-retired,
        # retired-file-corrupt) reach bridge_alerts. The tracker_dir is
        # ``<repo_root>/.tickets-tracker``; the alert store keys off repo_root.
        self._repo_root = tracker_dir.parent
        self._data = self._load()
        self._retired: set[str] = self._load_retired()

    # -- persistence -------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError, OSError) as exc:
                # Fail CLOSED: corrupt or conflict-marked bindings.json must
                # never silently degrade to empty bindings.  An empty store
                # treats every local ticket as unbound → emits CREATE mutations
                # for all of them on the next pass → mass duplicate Jira issues.
                #
                # Recovery hint: resolve the git merge conflict in the file or
                # restore it from the most recent commit on the tickets branch.
                raise ValueError(
                    f"bindings.json is corrupt or contains git conflict markers "
                    f"and cannot be parsed — aborting reconcile pass to prevent "
                    f"duplicate Jira mutations. File: {self._path}. "  # tickets-boundary-ok
                    f"Original error: {exc}. "
                    f"Recovery: resolve the merge conflict or restore the file "  # tickets-boundary-ok
                    f"from the tickets branch with: "
                    f"git show tickets:.tickets-tracker/.bridge_state/bindings.json"  # tickets-boundary-ok
                ) from exc
        return json.loads(json.dumps(_EMPTY_STORE))  # deep copy

    def _load_retired(self) -> set[str]:
        """Load the retired-binding set. FAIL-OPEN (bug 1e08, I2).

        Contrast bindings.json (fail-closed): a retired binding wrongly treated
        as live costs exactly one wasted GET (it re-404s → re-retires after
        GRACE), never a re-emit (a 404 emits nothing). So a corrupt retired file
        degrades to an empty retired-set + a deduped alert rather than aborting
        the pass.
        """
        if not self._retired_path.exists():
            return set()
        try:
            with open(self._retired_path, encoding="utf-8") as f:
                data = json.load(f)
            retired = data.get("retired", {})
            if isinstance(retired, dict):
                return set(retired.keys())
            if isinstance(retired, list):
                return set(retired)
            return set()
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            self._alert(
                key="retired-file-corrupt",
                record={
                    "kind": "binding-retired-file-corrupt",
                    "path": str(self._retired_path),
                    "error": repr(exc),
                },
            )
            return set()

    def _retired_entries(self) -> dict[str, Any]:
        """Read the retired-file's full {jira_key: entry} map (fail-open)."""
        if not self._retired_path.exists():
            return {}
        try:
            with open(self._retired_path, encoding="utf-8") as f:
                data = json.load(f)
            retired = data.get("retired", {})
            return retired if isinstance(retired, dict) else {}
        except (json.JSONDecodeError, ValueError, OSError):
            return {}

    def _save_retired(self, entries: dict[str, Any]) -> None:
        """Atomically persist the retired-binding map (tempfile + os.replace)."""
        self._retired_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._retired_path.parent),
            prefix="bindings_retired_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    {"version": 1, "retired": entries}, f, indent=2, sort_keys=True
                )
                f.write("\n")
            os.replace(tmp, str(self._retired_path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _alert(self, key: str, record: dict[str, Any]) -> None:
        """Append a deduped alert to bridge_alerts (best-effort).

        Loaded lazily by file path so the binding store stays importable in
        isolation (the test tree loads it via spec_from_file_location). Any
        failure here is swallowed — alerting must never break a sync pass.
        """
        try:
            import importlib.util as _ilu

            alert_path = Path(__file__).parent / "alert_store.py"
            spec = _ilu.spec_from_file_location(
                "rebar_reconciler.alert_store", alert_path
            )
            if spec is None or spec.loader is None:
                return
            alert_mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(alert_mod)  # type: ignore[union-attr]
            full_record = {**record, "key": key, "resolved": False}
            if not alert_mod.is_deduped(key, self._repo_root):
                alert_mod.append(full_record, self._repo_root)
        except Exception:  # noqa: BLE001 — alerting is best-effort
            pass

    def save(self) -> None:
        """Atomic write via tempfile + os.replace."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix="bindings_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, str(self._path))
        except BaseException:
            # Clean up temp file on any failure
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- queries -----------------------------------------------------------

    def get_jira_key(self, local_id: str) -> str | None:
        entry = self._data["bindings"].get(local_id)
        if entry is None:
            return None
        return entry.get("jira_key")

    def get_local_id(self, jira_key: str) -> str | None:
        return self._data["reverse"].get(jira_key)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._data["bindings"]

    def is_pending(self, local_id: str) -> bool:
        entry = self._data["bindings"].get(local_id)
        return entry is not None and entry.get("state") == "pending"

    def all_bindings(self) -> dict[str, dict]:
        return dict(self._data["bindings"])

    def pending_bindings(self) -> list[str]:
        return [
            lid
            for lid, entry in self._data["bindings"].items()
            if entry.get("state") == "pending"
        ]

    def confirmed_count(self) -> int:
        return sum(
            1
            for entry in self._data["bindings"].values()
            if entry.get("state") == "confirmed"
        )

    # -- mutations ---------------------------------------------------------

    def bind_pending(self, local_id: str) -> None:
        """Mark a local ticket as pending outbound creation."""
        now = _now_iso()
        self._data["bindings"][local_id] = {
            "jira_key": None,
            "state": "pending",
            "created_at": now,
            "updated_at": now,
        }

    def bind_confirm(self, local_id: str, jira_key: str) -> None:
        """Confirm binding after Jira issue creation succeeds."""
        now = _now_iso()
        entry = self._data["bindings"].get(local_id)
        if entry is None:
            # Direct confirm without prior pending — allowed for recovery
            entry = {"created_at": now}
            self._data["bindings"][local_id] = entry
        entry["jira_key"] = jira_key
        entry["state"] = "confirmed"
        entry["updated_at"] = now
        # Maintain reverse index
        self._data["reverse"][jira_key] = local_id

    def unbind(self, local_id: str) -> None:
        """Remove binding (for cleanup/rollback)."""
        entry = self._data["bindings"].pop(local_id, None)
        if entry is not None and entry.get("jira_key"):
            self._data["reverse"].pop(entry["jira_key"], None)

    # -- absence lifecycle (bug 1e08) --------------------------------------

    def _entry_for_jira_key(self, jira_key: str) -> dict[str, Any] | None:
        """Resolve a binding entry by Jira key via the reverse index."""
        local_id = self._data["reverse"].get(jira_key)
        if local_id is None:
            return None
        return self._data["bindings"].get(local_id)

    def note_absent(self, jira_key: str) -> None:
        """Record a consecutive-404 GET against a bound key.

        Increments ``absent_404_count`` on the binding entry. When the count
        reaches ``RECONCILER_ABSENT_RETIRE_GRACE`` consecutive 404s, the
        binding is soft-deleted: moved to bindings-retired.json (reversible)
        and a deduped ``binding-retired`` alert is appended.
        """
        local_id = self._data["reverse"].get(jira_key)
        entry = self._entry_for_jira_key(jira_key)
        if entry is None:
            return
        entry["absent_404_count"] = int(entry.get("absent_404_count", 0)) + 1
        entry["updated_at"] = _now_iso()
        grace = _env_int(
            "RECONCILER_ABSENT_RETIRE_GRACE",
            _DEFAULT_ABSENT_RETIRE_GRACE,
            minimum=1,
        )
        if entry["absent_404_count"] >= grace:
            self._retire(local_id, jira_key, entry)

    def _retire(self, local_id: str, jira_key: str, entry: dict[str, Any]) -> None:
        """Soft-delete a binding: move it to the retired file + alert."""
        retired_entries = self._retired_entries()
        retired_entries[jira_key] = {
            "local_id": local_id,
            "retired_at": _now_iso(),
            "absent_404_count": int(entry.get("absent_404_count", 0)),
            "last_jira_key": jira_key,
        }
        self._save_retired(retired_entries)
        self._retired.add(jira_key)
        # Remove the live binding (reversible: the entry survives in the
        # retired file and the live binding can be re-created on recovery).
        self._data["bindings"].pop(local_id, None)
        self._data["reverse"].pop(jira_key, None)
        self.save()
        self._alert(
            key=f"binding-retired:{jira_key}",
            record={
                "kind": "binding-retired",
                "jira_key": jira_key,
                "local_id": local_id,
            },
        )

    def clear_absent(self, jira_key: str) -> None:
        """Reset the absence counter after a 200 GET (the issue is alive)."""
        entry = self._entry_for_jira_key(jira_key)
        if entry is None:
            return
        if entry.get("absent_404_count"):
            entry["absent_404_count"] = 0
            entry["updated_at"] = _now_iso()

    def set_last_get(self, jira_key: str, pass_id: str) -> None:
        """Record the pass_id of the most recent GET (rotation bookkeeping)."""
        entry = self._entry_for_jira_key(jira_key)
        if entry is None:
            return
        entry["last_get_pass"] = pass_id

    def last_get_pass(self, jira_key: str) -> str:
        """Return the pass_id of the last GET; ``""`` if never GET'd (sorts first)."""
        entry = self._entry_for_jira_key(jira_key)
        if entry is None:
            return ""
        return entry.get("last_get_pass", "") or ""

    def is_retired(self, jira_key: str) -> bool:
        """Return True if the key has been soft-deleted (retired)."""
        return jira_key in self._retired

    # -- recovery ----------------------------------------------------------

    def recover_pending_bindings(self, client: Any) -> int:
        """Scan for pending bindings and attempt to recover.

        For each pending binding:
        1. Search Jira for ``rebar-id:{local_id}`` label (canonical colon form —
           written by applier.py outbound_create and inbound_create since the
           identity-label write was introduced).
        2. If not found, fall back to ``rebar-id-{local_id}`` (legacy hyphen
           form — old issues created before the colon form was adopted may
           carry this label; differ.py:402-414 recognises both forms).
        3. If found via either search → confirm binding with discovered key.
        4. If not found by either → unbind (the create never reached Jira).

        Returns count of recovered bindings.
        """
        recovered = 0
        for local_id in list(self.pending_bindings()):
            # Primary: canonical colon-form label (applier.py:753, 1931).
            colon_label = f"rebar-id:{local_id}"
            results = client.search_issues(f'labels = "{colon_label}"')
            if not results:
                # Legacy fallback: hyphen-form label (pre-colon-migration issues).
                hyphen_label = f"rebar-id-{local_id}"
                results = client.search_issues(f'labels = "{hyphen_label}"')
            if results:
                jira_key = results[0]["key"]
                self.bind_confirm(local_id, jira_key)
            else:
                self.unbind(local_id)
            recovered += 1
        return recovered


def load_binding_store(repo_root: Path) -> BindingStore:
    """Entry point for the reconciler orchestrator — call at pass start."""
    tracker_dir = repo_root / ".tickets-tracker"  # tickets-boundary-ok
    return BindingStore(tracker_dir)
