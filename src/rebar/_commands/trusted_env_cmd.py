"""``rebar trusted-env add|revoke`` — maintain ``.rebar/trusted_environments.yaml`` (story 4214).

The out-of-band trusted-environment config lives on the CODE branch (Gerrit-gated +
CODEOWNERS-protected), NOT the auto-pushed tickets branch, so this helper edits the YAML file in
place. Under Option B a key's era boundaries are TICKETS-BRANCH log positions: ``add`` stamps the
CURRENT tickets-branch tip position as ``added_at_log_position`` (with ``revoked_at_log_position:
null``); ``revoke`` stamps the current tip as ``revoked_at_log_position``. The tip is obtained by
READING the tickets store (never writing it) — scanning active event files under the tracker and
taking the lexicographically-greatest ``{timestamp}-{uuid}`` prefix.

Verb dispatch mirrors :func:`rebar._commands.identity.identity_cli`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

from rebar.attest.trusted_env import TRUSTED_ENV_FILENAME

_USAGE = (
    "rebar trusted-env add <env_id> <public_key> [--root <path>]\n"
    "rebar trusted-env revoke <env_id> <public_key-or-index> [--root <path>]"
)


def _tip_position(repo_root) -> str | None:
    """The CURRENT tickets-branch tip log position: the lexicographically-greatest
    ``{timestamp}-{uuid}`` prefix over the store's ACTIVE event files, or ``None`` when the store
    holds no active events. A pure READ of the tickets store (positions sort by HLC timestamp, so
    the greatest prefix is the newest event)."""
    from rebar._commands._seam import tracker_dir
    from rebar.reducer._cache import is_active_event

    tracker = tracker_dir(repo_root)
    best: str | None = None
    try:
        entries = os.listdir(tracker)
    except OSError:
        return None
    for d in entries:
        dp = os.path.join(str(tracker), d)
        if d.startswith(".") or not os.path.isdir(dp):
            continue
        try:
            names = os.listdir(dp)
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".json") or fn.startswith(".") or not is_active_event(fn):
                continue
            # filename = "{ts}-{uuid}-{TYPE}.json"; the position is "{ts}-{uuid}".
            pos = fn[:-5].rsplit("-", 1)[0]
            if best is None or pos > best:
                best = pos
    return best


def _config_file(repo_root) -> Path:
    return Path(repo_root or ".") / ".rebar" / TRUSTED_ENV_FILENAME


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _find_env(data: dict, env_id: str) -> dict | None:
    for env in data.get("environments") or []:
        if isinstance(env, dict) and env.get("env_id") == env_id:
            return env
    return None


def _add(env_id: str, public_key: str, *, repo_root) -> int:
    tip = _tip_position(repo_root)
    if tip is None:
        print(
            "trusted-env: cannot determine the tickets-branch tip position (empty/unmounted store)",
            file=sys.stderr,
        )
        return 2
    path = _config_file(repo_root)
    data = _load(path)
    envs = data.setdefault("environments", [])
    if not isinstance(envs, list):
        print(f"trusted-env: {path} 'environments' is not a list", file=sys.stderr)
        return 2
    record = {
        "public_key": public_key,
        "added_at_log_position": tip,
        "revoked_at_log_position": None,
    }
    env = _find_env(data, env_id)
    if env is None:
        envs.append({"env_id": env_id, "keys": [record]})
    else:
        env.setdefault("keys", []).append(record)
    _dump(path, data)
    print(f"trusted-env: added key to {env_id} (added_at_log_position={tip})")
    return 0


def _revoke(env_id: str, key_ref: str, *, repo_root) -> int:
    tip = _tip_position(repo_root)
    if tip is None:
        print(
            "trusted-env: cannot determine the tickets-branch tip position (empty/unmounted store)",
            file=sys.stderr,
        )
        return 2
    path = _config_file(repo_root)
    data = _load(path)
    env = _find_env(data, env_id)
    if env is None:
        print(f"trusted-env: environment {env_id!r} is not pinned in {path}", file=sys.stderr)
        return 1
    keys = env.get("keys") or []
    target = None
    # Select by numeric index first, else by exact public-key match.
    if key_ref.isdigit():
        idx = int(key_ref)
        if 0 <= idx < len(keys):
            target = keys[idx]
    if target is None:
        for key in keys:
            if isinstance(key, dict) and key.get("public_key") == key_ref:
                target = key
                break
    if target is None:
        print(
            f"trusted-env: no key matching {key_ref!r} for environment {env_id!r} in {path}",
            file=sys.stderr,
        )
        return 1
    target["revoked_at_log_position"] = tip
    _dump(path, data)
    print(f"trusted-env: revoked key of {env_id} (revoked_at_log_position={tip})")
    return 0


def cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar trusted-env add <env_id> <public_key>`` / ``revoke <env_id> <key-or-index>``."""
    if not argv or argv[0] in ("--help", "-h", "help"):
        print(_USAGE)
        return 0 if argv else 1
    p = argparse.ArgumentParser(prog="rebar trusted-env", usage=_USAGE, add_help=False)
    p.add_argument("verb", choices=("add", "revoke"))
    p.add_argument("env_id")
    p.add_argument("target", help="<public_key> for add; <public_key-or-index> for revoke")
    p.add_argument("--root", help="repo root (default: cwd); resolves the ticket store")
    try:
        args = p.parse_args(argv)
    except SystemExit:
        print(_USAGE, file=sys.stderr)
        return 2
    root = args.root if args.root is not None else repo_root
    if args.verb == "add":
        return _add(args.env_id, args.target, repo_root=root)
    return _revoke(args.env_id, args.target, repo_root=root)
