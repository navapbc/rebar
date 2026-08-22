"""Read-only live-Jira probe that suggests a ``[mapping]`` config block (epic 3d7a, S8).

``rebar bridge suggest-mapping <PROJECT> [--write]`` observes a live Jira project through
a narrow, READ-ONLY probe PORT and emits a suggested ``[mapping.projects.<KEY>]`` block —
the reserved section parsed by :mod:`rebar_reconciler.mapping_config` (S1) — seeded with
the project's real vocabulary (``issue_types`` / ``statuses`` / ``link_types`` /
``hierarchy``) and IDENTITY-SEED axis maps (``status_map`` / ``type_map`` / ``link_map`` /
``priority_map`` / ``create_defaults``). Hand-authoring that block is transcription-error
prone; this seeds it from the instance itself, leaving the operator to edit only where a
local name must diverge from Jira's.

Design of record
----------------
* The verb is ``suggest-mapping``, NOT ``probe`` — ``bridge probe`` collides with the
  retired throwaway-issue probe alias whose handler CREATES + DELETES a throwaway issue. This
  command is strictly read-only: the port exposes ONLY getters, so it can never mutate the
  instance the way ``check-access`` does.
* Every axis is grounded in a HIGH-LEVEL ``jira.JIRA`` method (there is no private
  ``_get_json`` on the Jira adapter — that lives only on the Gerrit client). The default
  port wraps a real ``jira.JIRA`` built from resolved DC settings via
  :func:`...adapters.jira_datacenter.transport.build_client_from_settings`; tests
  monkeypatch :data:`build_probe` (a MODULE ATTRIBUTE) to inject a FAKE port, so the whole
  builder runs offline with no live Jira and no CI credential (the portability rule).
* Fail-soft on EVERY external getter — a Jira instance that 403s or omits an attribute
  yields an empty axis + an honest note, never a fabricated value or a crash. In
  particular ``hierarchyLevel`` is Cloud-only: when absent, ``hierarchy`` is dropped
  (empty) rather than invented.
* ``statuses()`` is the GLOBAL status set — pycontribs exposes no per-project status list
  publicly — and the emitted block documents that honest limitation. Transition hints are
  best-effort: Jira returns transitions only from each sample issue's CURRENT status, so
  the set is inherently partial; it is emitted under a clearly-named advisory key and
  ``status_map`` stays identity-seeded, never derived from it.

The pure builder :func:`build_mapping_layer` calls the port's read methods and returns
``{"projects": {<KEY>: {<layer>}}}``. Serialization to TOML (stdout, or a deep-merged
``--write`` into a rebar-owned ``rebar.toml``) is orchestrated by
:func:`rebar._config_writer._emit_config_toml`.
"""

from __future__ import annotations

from typing import Any, Protocol

# The outbound create path (adapters/jira/outbound_fields.py:map_local_to_remote) already
# supplies these fields on every create, so a required createmeta field already in this
# baseline needs NO ``create_defaults`` stub — only a required field OUTSIDE it does.
_BASELINE_CREATE_FIELDS: frozenset[str] = frozenset(
    {"summary", "description", "issuetype", "priority", "assignee", "status", "parent"}
)

_STUB_VALUE = "TODO: required by Jira but not set by rebar — provide a default or remove"


class ProbeError(RuntimeError):
    """A SYSTEMIC probe failure — distinct from a legitimately-empty axis.

    Raised when the probe cannot be trusted to have observed the project at all: a core
    getter failed (bad credentials / 403 on every call / network outage), or every getter
    succeeded but returned nothing (anonymous access or a nonexistent project answering
    200-with-nothing). The CLI handler surfaces this as a non-zero exit with a clear
    message rather than emitting an empty — and misleading — suggestion. Individual
    OPTIONAL axes (priorities, link types, createmeta, transition hints) stay fail-soft:
    an empty one of those is a valid observation, not a systemic failure."""


class ProbePort(Protocol):
    """The read-only surface the layer builder depends on. The default implementation
    (:class:`_JiraProbePort`) normalizes raw ``jira.JIRA`` resources to plain data so the
    builder — and the offline test fakes — stay simple. There is deliberately NO
    create / transition / delete method: read-only is the whole point."""

    def issue_types(self) -> list[dict]: ...
    def priorities(self) -> list[str]: ...
    def issue_link_types(self) -> list[str]: ...
    def statuses(self) -> list[str]: ...
    def createmeta_issuetypes(self, key: str) -> list[dict]: ...
    def createmeta_fieldtypes(self, key: str, issue_type_id: str) -> list[dict]: ...
    def search_issues(self, jql: str) -> list[dict]: ...
    def transitions(self, issue_key: str) -> list[dict]: ...


# ---------------------------------------------------------------------------
# The default port: a thin, fail-soft adapter over a real ``jira.JIRA`` client
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a ``jira.JIRA`` resource whether it exposes the value as an
    attribute (the resource form) or a mapping key (a raw dict), falling back softly."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class _JiraProbePort:
    """Fail-soft, read-only adapter over a ``jira.JIRA`` client.

    Every method is a thin call to ONE high-level ``jira.JIRA`` getter, normalized to
    plain data. A CORE getter (``issue_types`` / ``statuses``) that fails surfaces the
    failure as a :class:`ProbeError` — a systemic fault must never masquerade as an empty
    result. An OPTIONAL axis getter degrades to an empty result (the builder then emits an
    honest note), because an empty optional axis is a legitimate observation."""

    def __init__(self, jira_client: Any) -> None:
        self._client = jira_client

    @staticmethod
    def _soft(fn: Any, default: Any, *, optional: bool = True) -> Any:
        try:
            return fn()
        except Exception as exc:  # external getter; classify by criticality below
            if not optional:
                raise ProbeError(str(exc)) from exc
            return default

    def issue_types(self) -> list[dict]:
        def _do() -> list[dict]:
            out: list[dict] = []
            for t in self._client.issue_types():
                entry: dict[str, Any] = {"name": _attr(t, "name", ""), "id": _attr(t, "id", "")}
                level = _attr(t, "hierarchyLevel")
                if level is not None:
                    entry["hierarchyLevel"] = level
                out.append(entry)
            return out

        # CORE vocabulary: a failure here is systemic, not an empty axis.
        return self._soft(_do, [], optional=False)

    def priorities(self) -> list[str]:
        return self._soft(lambda: [_attr(p, "name", "") for p in self._client.priorities()], [])

    def issue_link_types(self) -> list[str]:
        return self._soft(
            lambda: [_attr(lt, "name", "") for lt in self._client.issue_link_types()], []
        )

    def statuses(self) -> list[str]:
        # CORE vocabulary: a failure here is systemic, not an empty axis.
        return self._soft(
            lambda: [_attr(s, "name", "") for s in self._client.statuses()], [], optional=False
        )

    def createmeta_issuetypes(self, key: str) -> list[dict]:
        def _do() -> list[dict]:
            result = self._client.createmeta_issuetypes(key)
            values = result.get("values", result) if isinstance(result, dict) else result
            return [{"id": _attr(t, "id", ""), "name": _attr(t, "name", "")} for t in values]

        return self._soft(_do, [])

    def createmeta_fieldtypes(self, key: str, issue_type_id: str) -> list[dict]:
        def _do() -> list[dict]:
            # createmeta_fieldtypes is deprecated / non-paginating on jira>=3.8,<4; guard
            # the raw shape (list, or a ``{"values": [...]}`` envelope) either way.
            result = self._client.createmeta_fieldtypes(key, issue_type_id)
            values = result.get("values", result) if isinstance(result, dict) else result
            return [
                {
                    "fieldId": _attr(f, "fieldId", ""),
                    "name": _attr(f, "name", ""),
                    "required": bool(_attr(f, "required", False)),
                }
                for f in values
            ]

        return self._soft(_do, [])

    def search_issues(self, jql: str) -> list[dict]:
        def _do() -> list[dict]:
            out: list[dict] = []
            for issue in self._client.search_issues(jql):
                fields = _attr(issue, "fields")
                issue_type = _attr(_attr(fields, "issuetype"), "name", "")
                status = _attr(_attr(fields, "status"), "name", "")
                out.append(
                    {"key": _attr(issue, "key", ""), "issue_type": issue_type, "status": status}
                )
            return out

        return self._soft(_do, [])

    def transitions(self, issue_key: str) -> list[dict]:
        def _do() -> list[dict]:
            out: list[dict] = []
            for tr in self._client.transitions(issue_key):
                out.append(
                    {"name": _attr(tr, "name", ""), "to": _attr(_attr(tr, "to"), "name", "")}
                )
            return out

        return self._soft(_do, [])


def build_probe(*_args: Any, **_kwargs: Any) -> ProbePort:
    """Construct the default probe port from resolved Data Center settings.

    A MODULE-ATTRIBUTE factory (not an inline construction) so tests can
    ``monkeypatch.setattr(mapping_probe, "build_probe", ...)`` to inject an offline fake;
    the ``suggest-mapping`` handler MUST obtain its port through this attribute for that
    patch to take effect."""
    from rebar_reconciler.adapters.jira_datacenter.settings import (
        resolve_jira_datacenter_settings,
    )
    from rebar_reconciler.adapters.jira_datacenter.transport import (
        build_client_from_settings,
    )

    settings = resolve_jira_datacenter_settings()
    client = build_client_from_settings(settings)
    return _JiraProbePort(client)


# ---------------------------------------------------------------------------
# The pure layer builder
# ---------------------------------------------------------------------------


def _hierarchy(issue_types: list[dict]) -> dict[str, int]:
    """The ``name -> hierarchyLevel`` map for the types that DECLARE a level. Cloud-only:
    on Data Center every type lacks ``hierarchyLevel`` so this is empty and the caller
    drops ``hierarchy`` rather than fabricate ranks."""
    out: dict[str, int] = {}
    for t in issue_types:
        level = t.get("hierarchyLevel")
        name = t.get("name")
        if level is not None and name:
            out[str(name)] = int(level)
    return out


def _create_defaults(port: ProbePort, key: str) -> dict[str, str]:
    """Stub ``create_defaults`` entries for required createmeta fields OUTSIDE the outbound
    create baseline. A required field already supplied by ``map_local_to_remote``
    (summary/description/issuetype/priority/assignee/status/parent) needs no stub; a
    non-required field needs no stub; only a required, non-baseline field does."""
    stubs: dict[str, str] = {}
    for issue_type in port.createmeta_issuetypes(key):
        type_id = issue_type.get("id", "")
        for field in port.createmeta_fieldtypes(key, type_id):
            if not field.get("required"):
                continue
            field_id = str(field.get("fieldId", ""))
            if field_id.lower() in _BASELINE_CREATE_FIELDS:
                continue
            name = str(field.get("name") or field_id)
            stubs[name] = _STUB_VALUE
    return stubs


def _jql_quote(value: str) -> str:
    """Return ``value`` as a JQL string literal with embedded specials escaped.

    JQL string literals are double-quoted and escape a backslash as ``\\\\`` and a
    double-quote as ``\\"``. A raw issue-type or project-key value containing a quote —
    interpolated unescaped — produces malformed JQL that Jira rejects; combined with the
    fail-soft catch on ``search_issues`` that silently yields no hints. Escaping keeps the
    query well-formed."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _transition_hints(
    port: ProbePort, project_key: str, issue_types: list[str]
) -> dict[str, list[str]]:
    """Best-effort, PARTIAL transition hints: for one sample issue per discovered type,
    the transitions Jira offers from that sample's CURRENT status. Jira never returns the
    full transition graph (only edges out of the current status), so this is advisory
    only — emitted under a clearly best-effort key, never used to derive ``status_map``.

    The sampling JQL is scoped to ``project_key`` so hints come from the TARGET project,
    not the whole instance, and both interpolated values are quote-escaped."""
    hints: dict[str, list[str]] = {}
    seen: set[str] = set()
    for issue_type in issue_types:
        jql = (
            f"project = {_jql_quote(project_key)} AND "
            f"issuetype = {_jql_quote(issue_type)} ORDER BY created DESC"
        )
        for sample in port.search_issues(jql):
            src = str(sample.get("status") or "")
            issue_key = str(sample.get("key") or "")
            if not src or not issue_key or src in seen:
                continue
            seen.add(src)
            edges = [
                f"{t.get('name', '')} -> {t.get('to', '')}"
                for t in port.transitions(issue_key)
                if t.get("to")
            ]
            if edges:
                hints[src] = edges
    return hints


def build_mapping_layer(port: ProbePort, project_key: str) -> dict[str, Any]:
    """Build ``{"projects": {project_key: {<layer>}}}`` from the port's read-only getters.

    The layer keys match S1's ``MappingLayer``: vocabulary declarations (``statuses`` /
    ``issue_types`` / ``link_types`` / ``hierarchy``) plus IDENTITY-SEED axis maps
    (``status_map`` / ``type_map`` / ``link_map`` / ``priority_map`` / ``create_defaults``).
    An advisory ``transitions_best_effort`` marker records the partial transition hints
    without touching ``status_map``. Empty vocabularies carry an honest ``_notes`` entry
    rather than a fabricated value."""
    raw_types = port.issue_types()
    issue_types = [t["name"] for t in raw_types if t.get("name")]
    statuses = [s for s in port.statuses() if s]
    # Systemic guard: a probe whose CORE vocabulary is wholly empty (no issue types AND no
    # statuses) succeeded-but-observed-nothing — anonymous access or a nonexistent project
    # answering 200-with-nothing. That is indistinguishable from a broken probe, so it is
    # surfaced as an error rather than emitted as an empty (and misleading) suggestion.
    if not issue_types and not statuses:
        raise ProbeError(
            f"probe of project {project_key!r} returned no issue types and no statuses; "
            "the project key may not exist, or the credentials may lack access "
            "(an anonymous or unauthorized client often answers with an empty result)"
        )
    link_types = [lt for lt in port.issue_link_types() if lt]
    priorities = [p for p in port.priorities() if p]
    hierarchy = _hierarchy(raw_types)

    layer: dict[str, Any] = {
        "issue_types": issue_types,
        "statuses": statuses,
        "link_types": link_types,
        # Identity-seed axis maps: every local key maps to the same remote value; the
        # operator edits only where a name must diverge.
        "status_map": {s: s for s in statuses},
        "type_map": {t: t for t in issue_types},
        "link_map": {lt: lt for lt in link_types},
        "priority_map": {p: p for p in priorities},
        "create_defaults": _create_defaults(port, project_key),
    }
    if hierarchy:
        layer["hierarchy"] = hierarchy

    hints = _transition_hints(port, project_key, issue_types)
    if hints:
        layer["transitions_best_effort"] = hints

    notes = _honesty_notes(hierarchy, hints)
    if notes:
        layer["_notes"] = notes

    return {"projects": {project_key: layer}}


def _honesty_notes(hierarchy: dict[str, int], hints: dict[str, list[str]]) -> list[str]:
    """The honest-limitation footnotes carried in-band with the suggestion."""
    notes = [
        "statuses is the GLOBAL status set (Jira exposes no per-project status list "
        "publicly); prune any that this project does not use.",
        "axis maps are identity-seeded (local == remote); edit only where a local name "
        "must differ from Jira's.",
    ]
    if not hierarchy:
        notes.append(
            "hierarchy omitted: no issue type reported a hierarchyLevel (Data Center / "
            "older Jira). Add ranks by hand if this project has a type hierarchy."
        )
    if hints:
        notes.append(
            "transitions_best_effort is PARTIAL — only transitions out of each sampled "
            "issue's current status. Not a complete workflow; advisory only."
        )
    return notes
