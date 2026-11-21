"""Configuration for the op-cert merge gate, read from the reviewed base branch.

``rebar verify-opcert`` decides which completion certificates satisfy the merge gate from
its ``[verify]`` keys (``require_environment``, ``opcert_enforce_since``,
``trusted_opcert_keys``, ADR 0125) and from ``tracker.dir``, the store it walks. When the gate
checks a change, the change's own project config must not decide them: otherwise a change
could add its own key to ``trusted_opcert_keys``, drop ``require_environment``, or point
``tracker.dir`` at an empty directory, and so loosen the gate that checks it (bug
golden-busied-nag). The gate therefore composes its configuration from the user config, the
project config COMMITTED on the reviewed base branch, the environment, and ``-c`` overrides.
The project config in the working tree is never read, and neither is a ``$REBAR_CONFIG``
pointer. An edit to ``rebar.toml`` still takes effect for gating, but only after it merges.

The base branch is ``refs/remotes/origin/main``, the ref the attested gates already treat as
reviewed main (:data:`rebar.llm.gate_source.DEFAULT_REF`). A checkout that has a remote but
no such ref fails closed with :class:`~rebar.config.ConfigError`, naming the fetch that fixes
it; so does any failure to read the committed file. A checkout with no remote has no
reviewed branch to defer to and no change under review, so it keeps the working-tree
configuration.

The environment layer is kept on purpose: ADR 0125 puts the trust list in the verifying
environment. Keeping that layer, and the gate's own code and workflow, out of the change's
reach is the CI lane's job, not this module's.

This is in-process, so the same rule holds under any CI provider, or none: a pipeline only
has to fetch the base branch, which any git client can do.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path, PurePosixPath

from rebar import config
from rebar._config_schema import _SECTIONS, Config, ConfigError
from rebar._config_sections import coerce_sparse, merge_sparse
from rebar._config_sources import (
    _read_toml_table,
    _strict_unknown_keys,
    env_overrides,
    repo_root,
    user_config_path,
)
from rebar._operation_config import (
    ENVELOPE_VERSION,
    OperationSnapshot,
    compose_operation_snapshot,
)

#: The remote whose default branch is the reviewed base, and the fallback branch name.
BASE_REMOTE = "origin"
BASE_REF = f"refs/remotes/{BASE_REMOTE}/main"

_GIT_TIMEOUT_S = 30


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a read-only git command in ``root``; a git failure surfaces as a ConfigError."""
    try:
        return subprocess.run(  # raw-git-ok: read-only remote/rev-parse/ls-tree/show
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigError(f"op-cert gate: git {args[0]} failed in {root}: {exc}") from None


def base_ref(root: Path) -> str | None:
    """The reviewed base commit's ref, or ``None`` when the checkout has no remote at all.

    Raises :class:`ConfigError` when a remote exists but neither base ref resolves, because
    falling back to the working tree there would let the change under review configure its
    own gate."""
    remotes = _git(root, "remote")
    if remotes.returncode != 0:
        # Not a repository, a safe.directory refusal, ...: never read as "no remote".
        raise ConfigError(f"op-cert gate: git remote failed in {root}: {remotes.stderr.strip()}")
    if not remotes.stdout.strip():
        return None
    if _git(root, "rev-parse", "--verify", "--quiet", f"{BASE_REF}^{{commit}}").returncode == 0:
        return BASE_REF
    raise ConfigError(
        f"op-cert gate: cannot find the reviewed base branch {BASE_REF}; the gate reads its "
        "trust configuration from there, never from the checkout under review. Fetch it "
        f"first: `git fetch {BASE_REMOTE} +refs/heads/main:{BASE_REF}`"
    )


def _committed_toml(root: Path, spec: str) -> dict | None:
    """Parse the TOML blob ``<ref>:<path>``; ``None`` when the path is absent at that ref.

    Presence is decided from the tree listing alone, which never touches blobs, so a blob a
    blobless clone cannot fetch raises instead of reading as "absent"."""
    import tomllib  # py3.11+ stdlib; imported here like the other config readers

    ref, _, path = spec.partition(":")
    listed = _git(root, "ls-tree", "--full-tree", "--name-only", ref, "--", path)
    if listed.returncode != 0:
        raise ConfigError(f"op-cert gate: could not list {spec}: {listed.stderr.strip()}")
    if not listed.stdout.strip():
        return None
    shown = _git(root, "show", spec)
    if shown.returncode != 0:
        raise ConfigError(f"op-cert gate: could not read {spec}: {shown.stderr.strip()}")
    try:
        return tomllib.loads(shown.stdout)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"op-cert gate: could not parse {spec}: {exc}") from None


def _committed_project_layer(root: Path, ref: str) -> tuple[str, dict]:
    """The project config committed at ``ref``, found the way the working-tree walk finds it.

    Walks from the project root up to the git top level, taking the nearest ``rebar.toml``,
    or a ``pyproject.toml`` with a ``[tool.rebar]`` table. Returns ``(spec, table)``, or
    ``("", {})`` when ``ref`` carries no project config."""
    top = _git(root, "rev-parse", "--show-toplevel").stdout.strip()
    rel = PurePosixPath(Path(root).resolve().relative_to(Path(top).resolve()).as_posix())
    for directory in (rel, *rel.parents):
        prefix = "" if str(directory) == "." else f"{directory}/"
        spec = f"{ref}:{prefix}rebar.toml"
        data = _committed_toml(root, spec)
        if data is not None:
            return spec, data
        spec = f"{ref}:{prefix}pyproject.toml"
        data = _committed_toml(root, spec)
        table = (data or {}).get("tool", {}).get("rebar")
        if isinstance(table, dict):
            return spec, table
    return "", {}


def _base_layers(root_path: Path, ref: str) -> tuple[list[tuple[str, dict]], str]:
    """The gate's labelled sparse layers (user, base-branch project, env, cli) and the
    project layer's source."""
    strict = _strict_unknown_keys()
    layers: list[tuple[str, dict]] = []
    user = user_config_path()
    if user.is_file():
        raw = _read_toml_table(user, pyproject=False)
        layers.append(("user", coerce_sparse(raw, source=str(user), strict=strict)))
    spec, project = _committed_project_layer(root_path, ref)
    layers.append(("project", coerce_sparse(project, source=spec or ref, strict=strict)))
    layers.append(("env", coerce_sparse(env_overrides(), source="env", strict=strict)))
    cli = config._CLI_OVERRIDES
    if cli:
        layers.append(("cli", coerce_sparse(cli, source="cli", strict=strict)))
    return layers, spec or f"{ref} (no committed project config)"


def gate_config(root: str | None = None) -> tuple[Config, str, OperationSnapshot]:
    """Compose the op-cert gate's configuration and say where its project layer came from.

    Returns ``(config, source, snapshot)``. ``source`` names the committed file
    (``refs/remotes/origin/main:rebar.toml``), says the base carries none, or says the
    working tree was used because the checkout has no remote. ``snapshot`` is the same
    configuration as an operation snapshot: bound with
    :func:`rebar._operation_config.bind_operation_snapshot`, it makes every config read in
    the run, including the ticket store's location (``tracker.dir``), use it too. Raises
    :class:`ConfigError` on an unreadable layer or a missing base branch."""
    root_path = repo_root(root)
    ref = base_ref(root_path)
    if ref is None:
        cfg = config.compose_config(root=root)
        snapshot = compose_operation_snapshot(repo_root=root)
        return cfg, "the working tree (this checkout has no git remote)", snapshot
    layers, source = _base_layers(root_path, ref)
    cfg = Config.from_mapping(merge_sparse(*(sparse for _label, sparse in layers)))
    sources = {
        sect: {key: config._winning_label(layers, sect, key) for key in keys}
        for sect, keys in _SECTIONS.items()
    }
    snapshot = OperationSnapshot.build(
        envelope_version=ENVELOPE_VERSION,
        repo_root=str(root_path),
        values=dataclasses.asdict(cfg),
        sources=sources,
    )
    return cfg, source, snapshot
