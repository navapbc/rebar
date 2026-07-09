"""Local binding store for Jira bidirectional sync.

Maps local ticket IDs ↔ Jira issue keys.  Persisted as JSON at
`.tickets-tracker/.bridge_state/bindings.json` on the tickets branch.  # tickets-boundary-ok

Write-ahead protocol
--------------------
1. bind_pending(local_id)          — mark outbound create in-flight; save()
2. Jira client.create_issue(...)   — obtain DIG-NNNN
3. record_pending_key(local_id, jira_key) — record the key on the STILL-pending
   entry; save() — BEFORE the rebar-id label is attached (story 9622)
4. Jira client.add_label / set_entity_property — plant rebar-id marker
5. bind_confirm(local_id, jira_key) — finalise binding; save()

The step-3 keyed-pending write is what makes recovery deterministic: if the
process is hard-killed between create (step 2) and label (step 4), the pending
entry already carries the ``jira_key``, so recovery re-attaches the label and
confirms WITHOUT any Jira search (no duplicate). ``jira_key`` on a ``pending``
entry is an additive SUB-state of the ADR-0027 ``pending`` state, not a new
enumerated state.

Recovery (next pass startup): recover_pending_bindings(client, failure_sink=…):
- keyed-pending (has ``jira_key``) → retro-attach the rebar-id label/property
  (idempotent) and confirm, NO search.
- keyless-pending → search Jira for the rebar-id label; confirm if found, else
  unbind (the create never reached Jira).
- any per-entry error → append ``{local_id, reason}`` to ``failure_sink`` and
  continue (loud, non-fatal).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from rebar_reconciler.timeutil import utc_now_iso


class BindingPersistError(RuntimeError):
    """A write-ahead binding persist (``save()``) failed before ``create_issue``.

    Raised by the outbound-create write-ahead path (dispatch_one) when the
    durable pending record cannot be persisted. A durable pending record is a
    PRECONDITION for a safe create (it is what recovery keys on), so the create
    is skipped rather than run without one — the mutation is recorded failed and
    an alert fires, and the pass continues with the remaining mutations.
    """


def _now_iso() -> str:
    # Canonical Z-suffix UTC (twin of rebar.timeutils.utc_now_iso); retained as the
    # local spelling used across this module's call sites.
    return utc_now_iso()


_EMPTY_STORE: dict[str, Any] = {
    "version": 2,
    "bindings": {},
    "reverse": {},
}

# ADR 0026 — the per-binding three-way-merge baseline: the last-synced Jira-side
# values for the five inbound-mirrored scalar fields. Stored on the binding entry
# as ``baseline`` (+ ``baseline_advanced_at``). An ABSENT baseline is VALID and
# degrades to local-wins (safe/lossy); a version-1 store (no baselines) reads fine.
# These are the Jira field names as they appear in prev_snapshot (``summary`` is
# the Jira term for the local ``title``).
_BASELINE_FIELDS: tuple[str, ...] = (
    "summary",
    "description",
    "priority",
    "status",
    "assignee",
)

# Bug 1e08-1a35-0267-4ca6 — binding lifecycle (GC) defaults. These are the
# reconciler's only int-valued binding env vars; parsed defensively below so a
# typo'd ops value degrades to the default rather than aborting the pass.
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
                    f"Recovery: resolve the merge conflict or restore the file "  # tickets-boundary-ok  # noqa: E501
                    f"from the tickets branch with: "
                    f"git show tickets:.tickets-tracker/.bridge_state/bindings.json"  # tickets-boundary-ok  # noqa: E501
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
                json.dump({"version": 1, "retired": entries}, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, str(self._retired_path))
        except BaseException:  # noqa: BLE001 — retired-store atomic-write cleanup on ANY exit (incl. interrupts): unlink the temp then re-raise — never swallowed
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
            spec = _ilu.spec_from_file_location("rebar_reconciler.alert_store", alert_path)
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
        except BaseException:  # noqa: BLE001 — binding-store atomic-write cleanup on ANY exit (incl. interrupts): unlink the temp then re-raise — never swallowed
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
            lid for lid, entry in self._data["bindings"].items() if entry.get("state") == "pending"
        ]

    def confirmed_count(self) -> int:
        return sum(
            1 for entry in self._data["bindings"].values() if entry.get("state") == "confirmed"
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

    def record_pending_key(self, local_id: str, jira_key: str) -> None:
        """Record the Jira key on a STILL-pending entry (write-ahead step 3).

        Called the instant ``create_issue`` returns a key, BEFORE the rebar-id
        label is attached, so a hard crash in the create->label window leaves a
        pending entry that recovery can confirm deterministically (no search).
        The entry stays ``state='pending'`` — only the ``jira_key`` is filled in.
        If no pending entry exists yet (defensive), one is created.
        """
        now = _now_iso()
        entry = self._data["bindings"].get(local_id)
        if entry is None:
            entry = {"state": "pending", "created_at": now}
            self._data["bindings"][local_id] = entry
        entry["jira_key"] = jira_key
        entry["state"] = "pending"
        entry["updated_at"] = now

    def bind_confirm(self, local_id: str, jira_key: str) -> None:
        """Confirm binding after Jira issue creation succeeds."""
        now = _now_iso()
        entry = self._data["bindings"].get(local_id)
        if entry is None:
            # Direct confirm without prior pending — allowed for recovery
            entry = {"created_at": now}
            self._data["bindings"][local_id] = entry
        # Read the OLD key BEFORE overwriting so a rebind (e.g. hard-delete ->
        # re-create binds local_id to a NEW jira_key) drops the stale reverse entry
        # in the SAME save — otherwise reverse[old_key] dangles at this local_id
        # forever (c244; there is no dedicated rebind method, only unbind cleaned it).
        old_key = entry.get("jira_key")
        entry["jira_key"] = jira_key
        entry["state"] = "confirmed"
        entry["updated_at"] = now
        if old_key and old_key != jira_key:
            self._data["reverse"].pop(old_key, None)
        # Maintain reverse index
        self._data["reverse"][jira_key] = local_id

    def unbind(self, local_id: str) -> None:
        """Remove binding (for cleanup/rollback)."""
        entry = self._data["bindings"].pop(local_id, None)
        if entry is not None and entry.get("jira_key"):
            self._data["reverse"].pop(entry["jira_key"], None)

    # -- per-binding baseline (ADR 0026 — three-way-merge direction arbitration) --

    def get_baseline(self, local_id: str) -> dict[str, Any] | None:
        """Return the last-synced Jira-side field values for a binding, or None.

        None (an absent baseline) is VALID and means "no last-synced ancestor
        yet" — the consumer degrades to local-wins (ADR 0026 §2). A version-1
        store, or an entry that predates baselines, simply has no ``baseline``
        key and returns None here.
        """
        entry = self._data["bindings"].get(local_id)
        if entry is None:
            return None
        baseline = entry.get("baseline")
        if not isinstance(baseline, dict):
            return None
        return dict(baseline)

    def set_baseline(self, local_id: str, fields: dict[str, Any]) -> None:
        """Record the last-synced Jira-side values for a binding's 5 mirrored fields.

        Filters ``fields`` to ``_BASELINE_FIELDS`` (so a whole prev_snapshot entry
        can be passed directly) and stamps ``baseline_advanced_at``. A no-op if the
        local id is not bound (you cannot baseline an unbound pair). In-memory until
        ``save()`` — persisted by the existing binding-store commit path, no new
        commit surface (ADR 0026 §Consequences).
        """
        entry = self._data["bindings"].get(local_id)
        if entry is None:
            return
        entry["baseline"] = {k: fields.get(k) for k in _BASELINE_FIELDS if k in fields}
        entry["baseline_advanced_at"] = _now_iso()

    def seed_baselines_from_snapshot(self, prev_snapshot: dict[str, Any]) -> int:
        """One-shot: seed a baseline for every bound key present in a Jira snapshot.

        ``prev_snapshot`` is ``{jira_key: {summary, description, priority, status,
        assignee, ...}}``. Derisk X4 proved all 613 bound+present keys carry all 5
        mirrored fields, so already-bound pairs need no cold-start local-wins window.
        Does NOT delete prev_snapshot or change its consumers (that is the rollout
        task's swap). Returns the number of baselines seeded.
        """
        seeded = 0
        for local_id, entry in self._data["bindings"].items():
            jira_key = entry.get("jira_key")
            if jira_key and jira_key in prev_snapshot:
                self.set_baseline(local_id, prev_snapshot[jira_key])
                seeded += 1
        return seeded

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

    def recover_pending_bindings(
        self, client: Any, *, failure_sink: list[dict[str, Any]] | None = None
    ) -> int:
        """Scan for pending bindings and attempt to recover (story 9622).

        For each pending binding:

        - **Keyed-pending** (the entry already carries a ``jira_key`` — the
          write-ahead recorded it the instant ``create_issue`` returned, BEFORE
          the rebar-id label was attached): the create landed and the key is
          known, so retro-attach the rebar-id label + ``local_id`` entity
          property (idempotent — harmless if a prior partial attach already
          landed them) and confirm. NO Jira search — deterministic, so a hard
          crash in the create->label window yields NO duplicate.
        - **Keyless-pending** (no ``jira_key`` — the crash was before/during
          create): search Jira for the ``rebar-id:{local_id}`` label (canonical
          colon form), falling back to the legacy ``rebar-id-{local_id}`` hyphen
          form. Confirm if found; unbind if not (the create never reached Jira).

        Any per-entry error (a failed search or a failed retro-attach) is
        appended to ``failure_sink`` as ``{local_id, reason}`` and skipped
        (the entry stays pending for the next pass) — the recovery is loud but
        non-fatal and a single bad entry never aborts the rest.

        Returns the count of RESOLVED bindings (confirmed or unbound); failed
        entries are NOT counted (they remain pending). ``client`` must expose
        ``search_issues`` / ``add_label`` / ``set_entity_property``.
        """
        recovered = 0
        for local_id in list(self.pending_bindings()):
            try:
                entry = self._data["bindings"].get(local_id) or {}
                keyed = entry.get("jira_key")
                if keyed:
                    # Deterministic: the key is known — retro-attach the identity
                    # marker (idempotent) so future JQL dedup can find the issue,
                    # then confirm. No search.
                    client.add_label(keyed, f"rebar-id:{local_id}")
                    client.set_entity_property(keyed, "local_id", local_id)
                    self.bind_confirm(local_id, keyed)
                    recovered += 1
                    continue
                # Keyless: canonical colon-form label (applier.py outbound/inbound).
                colon_label = f"rebar-id:{local_id}"
                results = client.search_issues(f'labels = "{colon_label}"')
                if not results:
                    # Legacy fallback: hyphen-form (pre-colon-migration issues).
                    hyphen_label = f"rebar-id-{local_id}"
                    results = client.search_issues(f'labels = "{hyphen_label}"')
                if results:
                    self.bind_confirm(local_id, results[0]["key"])
                else:
                    self.unbind(local_id)
                recovered += 1
            except Exception as exc:  # noqa: BLE001 — loud-but-non-fatal: record and continue
                if failure_sink is not None:
                    failure_sink.append({"local_id": local_id, "reason": repr(exc)})
        return recovered


def load_binding_store(repo_root: Path) -> BindingStore:
    """Entry point for the reconciler orchestrator — call at pass start."""
    tracker_dir = repo_root / ".tickets-tracker"  # tickets-boundary-ok
    return BindingStore(tracker_dir)
