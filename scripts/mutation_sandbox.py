#!/usr/bin/env python3
"""OS sandbox for the mutation gate's test subprocesses [rebar:e668-b496-e264-4283].

``mutation_gate.execute_shard`` already isolates the *source tree* (``git archive``
into a scratch dir). That does nothing for *runtime effects*: on 2026-08-26 a mutation
removed a guard from a shell script performing real deletion, a test exec'd it with an
empty path variable, and the glob expanded to ``rm -rf /*`` — destroying
``/opt/homebrew`` and every Homebrew-installed app in ``/Applications`` before a
60-second timeout stopped it.

**Every mutation tool isolates the source tree; none isolates effects.** cargo-mutants
documents the hazard (https://mutants.rs/cautions.html) and recommends running "in an
isolated environment (container, CI, or VM)". Because the harness string-substitutes
the artifact under test, any guard written *inside* that artifact can be mutated away —
so only a layer outside it is load-bearing. That layer is this module.

Mechanisms, both OS facilities rather than CI features (``project.portability``):

* **macOS** — Seatbelt via ``sandbox-exec`` with a generated profile. Deprecated by
  Apple but functional; measured on macOS 26.5.2 denying a write outside the
  allow-list, permitting one inside, and denying ``rm -rf <protected>/*`` outright.
* **Linux** — ``bwrap`` (bubblewrap): ``--ro-bind / /`` makes the whole filesystem
  read-only, then each allow-list path is re-bound writable. ``unshare`` is
  deliberately NOT a fallback: ``unshare --mount`` only creates a private mount
  namespace and denies nothing without a read-only remount, and unprivileged
  ``CLONE_NEWUSER`` is disabled on many hardened hosts — so it can probe as present
  and silently fail to enforce, which is worse than no sandbox because it looks safe.

With no mechanism available the caller must ABORT. A silent unsandboxed fallback is
indistinguishable from a sandboxed run, which is the failure this module exists to
prevent. :data:`ALLOW_UNSANDBOXED_ENV` waives that, loudly.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

#: Operator opt-out. Setting this to a truthy value runs mutations UNSANDBOXED.
ALLOW_UNSANDBOXED_ENV = "REBAR_MUTATION_ALLOW_UNSANDBOXED"

#: A deliberately non-existent HOME, so a stray write to a real dotfile fails loudly
#: rather than landing in the operator's home directory (the Nix builder convention).
HOMELESS = "/nonexistent-rebar-mutation-home"

SEATBELT = "seatbelt"
BWRAP = "bwrap"


class SandboxUnavailable(RuntimeError):
    """No OS sandbox mechanism is available and the opt-out was not set."""


def probe() -> str | None:
    """Return the available mechanism name, or ``None`` when there is none."""
    if shutil.which("sandbox-exec"):
        return SEATBELT
    if shutil.which("bwrap"):
        return BWRAP
    return None


def opt_out_enabled(env: Mapping[str, str] | None = None) -> bool:
    raw = (env if env is not None else os.environ).get(ALLOW_UNSANDBOXED_ENV, "")
    return raw.strip().lower() not in {"", "0", "false", "no"}


def build_seatbelt_profile(allow: Sequence[Path]) -> str:
    """Seatbelt profile: deny all writes, then re-permit the allow-list subpaths."""
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write*",
    ]
    for path in allow:
        lines.append(f'  (subpath "{Path(path).resolve()}")')
    lines.extend(
        [
            '  (literal "/dev/null")',
            '  (literal "/dev/stdout")',
            '  (literal "/dev/stderr")',
            '  (subpath "/dev/fd")',
            ")",
        ]
    )
    return "\n".join(lines) + "\n"


def _bwrap_argv(argv: Sequence[str], allow: Sequence[Path]) -> list[str]:
    """Read-only root, then re-bind each allow-list path writable."""
    out = ["bwrap", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc"]
    for path in allow:
        resolved = str(Path(path).resolve())
        out += ["--bind", resolved, resolved]
    out.append("--")
    out.extend(argv)
    return out


def wrap(
    argv: Sequence[str],
    *,
    allow: Sequence[Path],
    profile_dir: Path,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Return ``argv`` wrapped in the platform sandbox.

    Raises :class:`SandboxUnavailable` when no mechanism exists and the opt-out is
    unset — the caller must abort rather than run unsandboxed.
    """
    mechanism = probe()
    if mechanism is None:
        if opt_out_enabled(env):
            logger.warning(
                "%s is set: running mutation tests UNSANDBOXED. A mutation that reaches "
                "a destructive code path can delete files outside the scratch tree.",
                ALLOW_UNSANDBOXED_ENV,
            )
            return list(argv)
        raise SandboxUnavailable(
            "no OS sandbox available (need `sandbox-exec` on macOS or `bwrap` on Linux); "
            f"refusing to run mutation tests unsandboxed. Set {ALLOW_UNSANDBOXED_ENV}=1 "
            "to override, accepting that a destructive mutant can delete real files."
        )
    if mechanism == SEATBELT:
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile = profile_dir / "mutation-sandbox.sb"
        profile.write_text(build_seatbelt_profile(allow), encoding="utf-8")
        return ["sandbox-exec", "-f", str(profile), *argv]
    return _bwrap_argv(argv, allow)


def sandbox_env(env: Mapping[str, str]) -> dict[str, str]:
    """Copy ``env`` with ``HOME`` pointed at a path that does not exist."""
    out = dict(env)
    out["HOME"] = HOMELESS
    return out
