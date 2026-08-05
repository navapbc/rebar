"""Raw-value coercion for the typed config schema.

One concern: turn an untyped TOML/env value into a typed Python value, or raise
:class:`ConfigError` naming the offending key. Extracted from
:mod:`rebar._config_schema` — whose own docstring lists "the value coercers" as a
distinct thing it held, alongside the section dataclasses and the coercion tables.
Splitting them apart leaves each module with one job and lets the schema read as
the shape of the config rather than as the parsing of it.

Pure stdlib, no ``rebar.*`` imports beyond the shared error type, preserving the
low-level-leaf property :mod:`rebar._config_schema` documents (so ``rebar.config``
can import either with no cycle).
"""

from __future__ import annotations

from typing import Any


class ConfigError(ValueError):
    """A config value is invalid. Raised at load time so problems fail fast at one
    site rather than surfacing deep in unrelated logic.

    Defined here rather than in :mod:`rebar._config_schema` because this module is
    where it is overwhelmingly raised (26 of its 30 raise sites) — the coercers ARE
    the validation layer. :mod:`rebar._config_schema` re-exports it, so
    ``from rebar.config import ConfigError`` is unchanged for callers.
    """


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off", ""}


def _src(source: str) -> str:
    return f" ({source})" if source else ""


def _as_bool(v: Any, key: str) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ConfigError(f"{key}: expected a boolean, got {v!r}")


def _as_int(v: Any, key: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(v, bool):  # bool is an int subclass — reject to catch e.g. true→1
        raise ConfigError(f"{key}: expected an integer, got boolean {v!r}")
    try:
        i = int(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}: expected an integer, got {v!r}") from None
    if minimum is not None and i < minimum:
        raise ConfigError(f"{key}: must be >= {minimum}, got {i}")
    if maximum is not None and i > maximum:
        raise ConfigError(f"{key}: must be <= {maximum}, got {i}")
    return i


def _as_float(
    v: Any, key: str, *, minimum: float | None = None, maximum: float | None = None
) -> float:
    if isinstance(v, bool):
        raise ConfigError(f"{key}: expected a number, got boolean {v!r}")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}: expected a number, got {v!r}") from None
    if minimum is not None and f < minimum:
        raise ConfigError(f"{key}: must be >= {minimum}, got {f}")
    if maximum is not None and f > maximum:
        raise ConfigError(f"{key}: must be <= {maximum}, got {f}")
    return f


def _as_str(v: Any, key: str) -> str:
    if isinstance(v, (dict, list)):
        raise ConfigError(f"{key}: expected a string, got {type(v).__name__}")
    return str(v)


def _as_str_tuple(v: Any, key: str) -> tuple[str, ...]:
    """A tuple of non-empty, trimmed strings from either a TOML array or a comma-separated
    string, so both ``key = ["T5c", "T10"]`` and ``key = "T5c, T10"`` parse. Empty entries are
    dropped; a non-list/non-str value is rejected. Used for config-backed id sets (e.g.
    ``verify.completion_preserve_criteria``)."""
    if isinstance(v, (list, tuple)):
        items = [str(x).strip() for x in v]
    elif isinstance(v, str):
        items = [p.strip() for p in v.split(",")]
    else:
        raise ConfigError(
            f"{key}: expected a list or comma-separated string, got {type(v).__name__}"
        )
    return tuple(x for x in items if x)


def _as_str_list(v: Any, key: str) -> list[str]:
    """A list of strings from a TOML array or comma-separated CLI override."""
    return list(_as_str_tuple(v, key))


def _as_choice(v: Any, key: str, choices: set[str]) -> str:
    s = str(v).strip().lower()
    if s not in choices:
        raise ConfigError(f"{key}: expected one of {sorted(choices)}, got {v!r}")
    return s


# Characters git's check-ref-format forbids anywhere in a ref component.
_BAD_REF_CHARS = set(" ~^:?*[\\\x7f") | {chr(c) for c in range(0x20)}


def _as_git_ref(v: Any, key: str) -> str:
    """Validate a single-level git branch name against a `git check-ref-format`-style
    rule set (the subset that matters for a branch): reject empty, whitespace, `..`,
    a leading `-` or `.`, any of ``~^:?*[\\`` / control / DEL chars, an ``@{`` sequence,
    a bare ``@``, a trailing ``/`` / ``.lock`` / ``.``, a leading/trailing/double slash,
    and a component beginning with ``.``. Keeps the tracker branch a valid, pushable ref."""
    s = _as_str(v, key).strip()
    if not s:
        raise ConfigError(f"{key}: branch name must not be empty")
    if s == "@" or "@{" in s or ".." in s:
        raise ConfigError(f"{key}: invalid branch name {s!r} (contains '@', '@{{', or '..')")
    if s.startswith("-") or s.startswith("/") or s.endswith("/") or "//" in s:
        raise ConfigError(f"{key}: invalid branch name {s!r} (bad slash placement or leading '-')")
    if s.endswith("."):  # per-component '.lock' is caught by the loop below
        raise ConfigError(f"{key}: invalid branch name {s!r} (ends with '.')")
    bad = sorted(_BAD_REF_CHARS & set(s))
    if bad:
        raise ConfigError(f"{key}: invalid branch name {s!r} (forbidden char(s) {bad})")
    for comp in s.split("/"):
        if not comp or comp.startswith(".") or comp.endswith(".lock"):
            raise ConfigError(f"{key}: invalid branch name {s!r} (bad path component {comp!r})")
    return s


def _as_git_remote(v: Any, key: str) -> str:
    """Validate a git REMOTE NAME (e.g. ``origin``, ``gerrit``, ``github``). Distinct from
    :func:`_as_git_ref` (a branch name): a remote name is a single-level token that becomes
    a path component under ``refs/remotes/<name>/`` and is passed as a positional to
    ``git push``/``fetch``. Reject empty/whitespace, a leading ``-`` (would parse as a
    flag), any ``/`` (remote names are single-level), ``..``, and the
    check-ref-format-forbidden chars (space, ``~^:?*[\\``, control, DEL). Dots and
    (non-leading) hyphens are allowed, so ``my-remote`` / ``gerrit.example`` pass."""
    s = _as_str(v, key).strip()
    if not s:
        raise ConfigError(f"{key}: git remote name must not be empty")
    if s.startswith("-") or "/" in s or ".." in s:
        raise ConfigError(f"{key}: invalid git remote name {s!r} (leading '-', '/', or '..')")
    bad = sorted(_BAD_REF_CHARS & set(s))
    if bad:
        raise ConfigError(f"{key}: invalid git remote name {s!r} (forbidden char(s) {bad})")
    return s


def _as_tracker_dir(v: Any, key: str) -> str:
    """Validate the tracker store dir. Allows a bare relative name (the common case,
    e.g. ``.tickets-tracker`` — used as the repo-root symlink name + gitignore entry)
    AND an absolute path (the supported relocated/decoupled store, EV-3b, set via
    ``REBAR_TRACKER_DIR``). Rejects empty/whitespace, any ``..`` traversal component,
    and control chars — values that would break the symlink/exclude or escape the repo."""
    s = _as_str(v, key).strip()
    if not s:
        raise ConfigError(f"{key}: tracker dir must not be empty")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in s):
        raise ConfigError(f"{key}: tracker dir {s!r} contains control characters")
    parts = s.replace("\\", "/").split("/")
    if ".." in parts:
        raise ConfigError(f"{key}: tracker dir {s!r} must not contain a '..' traversal component")
    return s
