#!/usr/bin/env python3
"""rebar-id label-write authorization guard.

The rebar-id-* label is the identity primitive binding a local ticket to its
Jira issue. Only three leaves may ever emit a rebar-id label mutation
(``outbound_create``/``inbound_create`` create; ``inbound_clean_label`` delete).
This module owns that contract: the authorized-writer/action tables, the guard
(``_audit_rebar_id_label_writes``) invoked before typed dispatch and on each
legacy-batch leaf, and the ``_BatchAuditView`` adapter that lets the guard see
legacy dict-shaped batch mutations.

``_rebar_env``/``_load_errors_module`` are kept module-local (mirroring the
applier pattern) so this guard resolves under the by-path test load without
reaching back into the applier facade.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _rebar_env(name: str, default: str | None = None) -> str | None:
    """Read ``REBAR_<name>`` from the environment (module-local; see applier)."""
    return os.environ.get(f"REBAR_{name}", default)


def _load_errors_module():
    """Lazy-load the sibling _errors module under the canonical key.

    Uses the same ``rebar_reconciler_errors`` sys.modules key as
    applier._load_errors_module so ``RebarIdLabelWriteError`` keeps a single
    class identity across the reconciler.
    """
    key = "rebar_reconciler_errors"
    if key in sys.modules:
        return sys.modules[key]
    err_path = Path(__file__).parent / "_errors.py"
    spec = importlib.util.spec_from_file_location(key, err_path)
    if spec is None:
        raise FileNotFoundError(f"_errors.py not found at {err_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(key, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Justification for the F841 suppression below: this constant is read by
# tests/unit/rebar_reconciler/test_errors.py::test_authorized_writers_docstring
# _documents_full_contract via getattr — static analyzers cannot trace the
# usage. Do NOT remove; it is the contract artifact for story 4496 dd-1.
_AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC: str = """  # noqa: F841
rebar-id label write authorization contract for applier.py
=========================================================

The applier dispatches mutations through exactly 9 leaf handlers, listed below
with their authorization status for rebar-id label mutations:

  1. outbound_create       — AUTHORIZED for {create}: adds "rebar-id:<local_id>"
                             label when a new Jira issue is created outbound.
  2. outbound_update       — UNAUTHORIZED for rebar-id label mutations.
  3. outbound_delete       — UNAUTHORIZED for rebar-id label mutations.
  4. outbound_probe        — UNAUTHORIZED for rebar-id label mutations.
  5. outbound_conflict     — UNAUTHORIZED for rebar-id label mutations.
  6. inbound_create        — AUTHORIZED for {create}: adds "rebar-id:<local_id>"
                             label when a new local ticket is created inbound
                             (dedup write-back so the differ recognizes the
                             issue as mirrored on subsequent passes).
  7. inbound_update        — UNAUTHORIZED for rebar-id label mutations.
  8. inbound_clean_label   — AUTHORIZED for {delete}: removes stale or
                             duplicated "rebar-id-*" labels from the Jira side.
  9. inbound_repair_property — UNAUTHORIZED for rebar-id label mutations.
                              This leaf writes the local_id entity PROPERTY
                              FIELD via set_issue_property(), NOT the label.

Only inbound_clean_label (delete), outbound_create (create), and
inbound_create (create) may emit rebar-id label mutations. Any other leaf that
emits such a mutation is a bug and should raise RebarIdLabelWriteError from
_errors.py.

conflict_resolver per-element provenance MUST skip rebar-id fields. The
conflict_resolver must not write, modify, or emit rebar-id label mutations;
rebar-id is the identity primitive and its provenance is governed solely by the
two authorized leaves above, not by the per-field provenance resolution path.

inbound_repair_property writes the local_id property field (entity
properties, not labels). It MUST NOT touch the label surface.
"""

_AUTHORIZED_REBAR_ID_LABEL_WRITERS: frozenset[str] = frozenset(
    {"inbound_clean_label", "outbound_create", "inbound_create"}
)
"""Leaf names authorized to emit rebar-id label mutations.

See _AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC for the full authorization contract.
"""

# Per-leaf authorized-action map: enforced by _audit_rebar_id_label_writes.
# Each authorized leaf is permitted ONLY the action(s) listed here; any other
# action on a rebar-id-* label by the same leaf raises RebarIdLabelWriteError. The
# pair set is the single source of truth referenced by
# _AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC above.
_AUTHORIZED_REBAR_ID_LABEL_ACTIONS: dict[str, frozenset[str]] = {
    "outbound_create": frozenset({"create"}),
    "inbound_create": frozenset({"create"}),
    "inbound_clean_label": frozenset({"delete"}),
}

# ---------------------------------------------------------------------------
# rebar-id label write guard
#
# _audit_rebar_id_label_writes is called after every leaf returns its mutation
# list (or before dispatching the typed-mutation leaf) to ensure no unauthorized
# leaf emits a rebar-id-* label mutation.
#
# Guard mode is controlled by REBAR_ID_GUARD_MODE (env) or rebar_id_guard_mode
# (.rebar/config.conf key). Precedence: env > config > default ('raise').
# ---------------------------------------------------------------------------


def _get_rebar_id_guard_mode_from_config() -> str | None:
    """Read rebar_id_guard_mode from the rebar config file, if present.

    Returns the value string (e.g. 'raise', 'warn') or None when the key
    is absent or the file cannot be read.

    Resolution order for the guard mode (env wins):
      1. os.environ['REBAR_ID_GUARD_MODE']  — checked in _audit_rebar_id_label_writes
      2. This function (.rebar/config.conf fallback)
      3. Default: 'raise'
    """
    try:
        _root = os.environ.get("REBAR_ROOT")
        if os.environ.get("REBAR_CONFIG"):
            config_path = Path(os.environ["REBAR_CONFIG"])
        elif _root:
            config_path = Path(_root) / ".rebar" / "config.conf"
        else:
            config_path = (
                Path(
                    os.environ.get("REBAR_ROOT")
                    or Path(__file__).resolve().parents[4]
                )
                / ".rebar"
                / "config.conf"
            )
        if not config_path.exists():
            return None
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("rebar_id_guard_mode"):
                parts = line.split("=", 1)
                if len(parts) == 2:
                    return parts[1].strip().strip('"').strip("'")
    except OSError:
        # Best-effort config read: filesystem-level failures (permission denied,
        # missing parent dir on race, etc.) fall through to the default 'raise'
        # guard mode. Programming errors (AttributeError, TypeError) intentionally
        # propagate so they surface during test runs.
        return None
    return None


def _is_rebar_id_label_write_mutation(mutation) -> bool:
    """Return True when *mutation* represents a rebar-id-* label write.

    Checks two shapes:
    - String payload (direct audit call): mutation.target == 'label' AND
      mutation.payload.startswith('rebar-id-') AND action in {create,update,delete}.
    - Dict payload (full Mutation from apply()): payload contains 'target'=='label'
      AND 'label' value starts with 'rebar-id-' AND action in {create,update,delete}.
    """
    action = str(getattr(mutation, "action", ""))
    if action not in {"create", "update", "delete"}:
        return False
    payload = getattr(mutation, "payload", None)
    if isinstance(payload, str):
        # String payload: check target field and payload value
        target = getattr(mutation, "target", "")
        return target == "label" and payload.startswith("rebar-id-")
    elif isinstance(payload, dict):
        # Dict payload: check embedded 'target'=='label' and 'label' value
        embedded_target = payload.get("target", "")
        label_val = payload.get("label", "")
        if (
            embedded_target == "label"
            and isinstance(label_val, str)
            and label_val.startswith("rebar-id-")
        ):
            return True
    return False


def _audit_rebar_id_label_writes(leaf_name: str, mutations: list) -> None:
    """Guard: raise (or warn) when an unauthorized leaf emits a rebar-id-* label mutation.

    Called before leaf dispatch (`_apply_typed`) AND on each leaf invocation in
    the legacy batch path (`_apply_batch`) to enforce the two-authorized-leaves
    contract documented in `_AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC`.

    Per-action enforcement (wired via `_AUTHORIZED_REBAR_ID_LABEL_ACTIONS`):
      - When `leaf_name` is in `_AUTHORIZED_REBAR_ID_LABEL_WRITERS` but emits an
        action OUTSIDE its permitted action set (e.g., outbound_create
        attempting a `delete` on a rebar-id label), the guard still raises. The
        contract is per-action; defeating it would leave a security gap by
        allowing an authorized leaf to perform any action.

    Guard mode (REBAR_ID_GUARD_MODE env var, .rebar/config.conf key rebar_id_guard_mode,
    default 'raise'):
      - 'raise': RebarIdLabelWriteError raised on violation (default, production-safe).
      - 'warn': WARNING logged with tag REBAR_ID_GUARD; no exception raised (staged rollout).

    Precedence: env var > .rebar/config.conf key > default 'raise'.
    """
    is_authorized_leaf = leaf_name in _AUTHORIZED_REBAR_ID_LABEL_WRITERS
    allowed_actions = _AUTHORIZED_REBAR_ID_LABEL_ACTIONS.get(leaf_name, frozenset())

    offending = None
    offending_payload = None
    offending_action = None
    for mutation in mutations:
        if not _is_rebar_id_label_write_mutation(mutation):
            continue
        action_str = str(getattr(mutation, "action", ""))
        if is_authorized_leaf and action_str in allowed_actions:
            # Permitted (leaf, action) pair — skip without raising.
            continue
        offending = mutation
        offending_action = action_str
        # Extract the label payload for the error message
        payload = getattr(mutation, "payload", "")
        if isinstance(payload, str):
            offending_payload = payload
        elif isinstance(payload, dict):
            offending_payload = payload.get("label", str(payload))
        else:
            offending_payload = str(payload)
        break

    if offending is None:
        return

    # Determine guard mode: env > config > default 'raise'
    guard_mode = _rebar_env("ID_GUARD_MODE")
    if guard_mode is None:
        guard_mode = _get_rebar_id_guard_mode_from_config()
    if guard_mode is None:
        guard_mode = "raise"

    msg = (
        f"REBAR_ID_GUARD: unauthorized rebar-id label write from leaf '{leaf_name}' "
        f"(action={offending_action!r}); offending payload: {offending_payload!r}"
    )

    if guard_mode == "warn":
        logger.warning(msg)
        return

    errs = _load_errors_module()
    raise errs.RebarIdLabelWriteError(msg)


class _BatchAuditView:
    """Adapter exposing a legacy dict-shaped batch mutation to the audit guard.

    The audit (`_is_rebar_id_label_write_mutation`) expects an object with
    ``target``, ``payload`` (str OR dict), and ``action`` attributes. Legacy
    batch mutations are dicts of shape ``{"action": ..., "key": ..., "fields":
    {"labels": [...], ...}}`` — this view surfaces any rebar-id-* label values
    sitting under ``fields["labels"]`` as a synthetic label-write mutation so
    the guard fires on unauthorized batch paths (e.g., an outbound_update
    trying to push a rebar-id-* label).

    ``target`` is set to 'label' iff the batch mutation includes a rebar-id-*
    label in its fields; otherwise an empty string makes the audit pass-through.
    """

    __slots__ = ("target", "payload", "action")

    def __init__(self, batch_mutation: dict) -> None:
        self.action = batch_mutation.get("action", "")
        fields = batch_mutation.get("fields") or {}
        labels = fields.get("labels") if isinstance(fields, dict) else None
        rebar_id_label = None
        if isinstance(labels, (list, tuple)):
            for lbl in labels:
                if isinstance(lbl, str) and lbl.startswith("rebar-id-"):
                    rebar_id_label = lbl
                    break
        if rebar_id_label is not None:
            self.target = "label"
            self.payload = rebar_id_label
        else:
            # Synthesise an explicit non-label target so the guard's
            # _is_rebar_id_label_write_mutation returns False on benign batches.
            self.target = ""
            self.payload = ""
