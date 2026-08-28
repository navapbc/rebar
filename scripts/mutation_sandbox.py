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

import functools
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

#: Operator opt-out. Setting this to a truthy value runs mutations UNSANDBOXED.
ALLOW_UNSANDBOXED_ENV = "REBAR_MUTATION_ALLOW_UNSANDBOXED"

#: A deliberately non-existent HOME, so a stray write to a real dotfile fails loudly
#: rather than landing in the operator's home directory (the Nix builder convention).
HOMELESS = "/nonexistent-rebar-mutation-home"
# Emitted by the Seatbelt probe child so the probe can tell "denied the write"
# from "never ran". Named so tests can assert both branches without a literal.
_PROBE_MARKER = "seatbelt-probe-ran"

SEATBELT = "seatbelt"
BWRAP = "bwrap"


class SandboxUnavailable(RuntimeError):
    """No OS sandbox mechanism is available and the opt-out was not set."""


@functools.lru_cache(maxsize=1)
def probe() -> str | None:
    """Return the available mechanism name, or ``None`` when there is none.

    `bwrap` is probed FUNCTIONALLY, not by presence on PATH. Presence is not the same
    as capability: Ubuntu 23.10+ restricts unprivileged user namespaces through
    AppArmor, and hardened kernels disable `CLONE_NEWUSER` outright, so an installed
    `bwrap` can fail with "Creating new namespace failed" while `shutil.which` happily
    reports it. Trusting PATH there is the exact flaw that disqualified `unshare` as a
    fallback — it applies to `bwrap` too, and a mechanism that reports available but
    cannot enforce is worse than none, because callers stop looking.

    Cached: the probe spawns a subprocess and the answer cannot change mid-run.
    """
    if shutil.which("sandbox-exec") and _seatbelt_works():
        return SEATBELT
    if shutil.which("bwrap") and _bwrap_works():
        return BWRAP
    return None


def _seatbelt_works() -> bool:
    """True when sandbox-exec actually DENIES a write — not merely when it runs.

    `sandbox-exec` is Apple-deprecated, so its enforcement is an external capability
    that can disappear under us. A non-enforcing stub satisfies `shutil.which` and
    exits 0 while denying nothing, and a PATH-only check would report the sandbox
    available and run every mutation UNSANDBOXED — the same flaw that disqualified
    `unshare` and that `_bwrap_works` exists to avoid on Linux.

    The probe requires BOTH signals: the child printed its liveness marker (so it
    really ran and reached the write) AND the file is absent (so the write was
    denied). Absence alone is not denial — a sandbox that failed to launch leaves the
    file absent too, and reading that as success is exactly the false pass this probe
    must not have.
    """
    exe = shutil.which("sandbox-exec")
    if exe is None:
        return False
    marker = _PROBE_MARKER
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "probe.txt"
            profile = Path(tmp) / "probe.sb"
            # An empty allow-list: every write is denied except the /dev literals the
            # builder always re-permits, which is what carries the marker to stdout.
            profile.write_text(build_seatbelt_profile(()), encoding="utf-8")
            proc = subprocess.run(
                # Quote the target: an unquoted path containing a space makes the
                # redirect fail for a reason unrelated to sandboxing, which would leave
                # the file absent while the marker still printed — the probe would then
                # report enforcement that never happened.
                [
                    exe,
                    "-f",
                    str(profile),
                    "/bin/sh",
                    "-c",
                    f"echo {marker}; echo x > {shlex.quote(str(target))}",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            ran = marker in (proc.stdout or "")
            denied = not target.exists()
    except (OSError, subprocess.SubprocessError):
        return False
    if not ran:
        logger.warning(
            "sandbox-exec is installed but the probe child never ran (%s); treating "
            "the sandbox as unavailable rather than trusting an unverified mechanism.",
            (proc.stderr or "").strip() or f"exit {proc.returncode}",
        )
        return False
    if not denied:
        logger.warning(
            "sandbox-exec is installed but did NOT deny a write; treating the sandbox "
            "as unavailable rather than trusting a mechanism that does not enforce."
        )
        return False
    return True


def _bwrap_works() -> bool:
    """True when bwrap can actually create the namespace it needs."""
    try:
        proc = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--", "/bin/true"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        logger.warning(
            "bwrap is installed but cannot create a namespace (%s); treating the "
            "sandbox as unavailable rather than trusting a mechanism that does not "
            "enforce.",
            (proc.stderr or "").strip() or f"exit {proc.returncode}",
        )
        return False
    return True


def _ci_environment(env: Mapping[str, str] | None = None) -> bool:
    """True on a CI runner.

    `CI=true` is the de-facto cross-provider convention (GitHub Actions, GitLab,
    CircleCI, Travis, Jenkins all set it), so this is NOT a CI-provider-specific
    branch of the kind `project.portability` forbids — it does not name a provider
    and needs no provider feature.

    GitHub's ubuntu runners ship bubblewrap but deny unprivileged user namespaces
    ("bwrap: setting up uid map: Permission denied"), so the functional probe
    correctly reports no sandbox and the gate would otherwise fail closed on every
    CI run. Accepting unsandboxed HERE is sound because the runner IS the isolated
    environment — the posture cargo-mutants recommends ("run in an isolated
    environment (container, CI, or VM)"). The 2026-08-26 `rm -rf /*` landed on a
    developer workstation, which is what the abort path still protects.
    """
    raw = (env if env is not None else os.environ).get("CI", "")
    return raw.strip().lower() not in {"", "0", "false", "no"}


def opt_out_enabled(env: Mapping[str, str] | None = None) -> bool:
    raw = (env if env is not None else os.environ).get(ALLOW_UNSANDBOXED_ENV, "")
    return raw.strip().lower() not in {"", "0", "false", "no"}


def _sb_quote(path: Path) -> str:
    """Escape a path for a Seatbelt double-quoted string literal.

    Unescaped, a path containing a quote or backslash produces a malformed profile,
    which `sandbox-exec` rejects or misparses — either way the write-deny is lost.
    """
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def build_seatbelt_profile(allow: Sequence[Path]) -> str:
    """Seatbelt profile: deny all writes, then re-permit the allow-list subpaths."""
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write*",
    ]
    for path in allow:
        lines.append(f'  (subpath "{_sb_quote(Path(path).resolve())}")')
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
    # `--dev` mounts a MINIMAL devtmpfs rather than bind-mounting the host's /dev
    # writable. `--dev-bind /dev /dev` would leave Linux materially looser than the
    # macOS profile, which re-permits only null/stdout/stderr/fd.
    out = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for path in allow:
        resolved = Path(path).resolve()
        # bwrap's --bind requires SRC to exist on the host; a missing path aborts the
        # whole sandbox. Skip rather than abort — the deny-by-default floor still holds.
        # SAY SO: a silently dropped bind is indistinguishable from one that was never
        # requested, so a caller that forgot to create a directory sees only a confusing
        # write failure from inside the sandbox instead of the cause.
        if not resolved.exists():
            logger.warning(
                "sandbox allow-list path %s does not exist; it will NOT be writable "
                "inside the sandbox. Create it before wrapping if the child needs it.",
                resolved,
            )
            continue
        out += ["--bind", str(resolved), str(resolved)]
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
        if _ci_environment(env):
            logger.warning(
                "No OS sandbox available and CI=%r: running mutation tests UNSANDBOXED "
                "— nothing constrains a destructive mutant on this run. "
                "A CI runner is already the disposable environment this sandbox "
                "substitutes for — ephemeral, destroyed after the job, holding nothing "
                "of a developer's. The sandbox requirement protects WORKSTATIONS. On a "
                "workstation this path aborts instead.",
                (env if env is not None else os.environ).get("CI"),
            )
            return list(argv)
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
    # The venv is deliberately NOT writable (see execute_shard's allow-list), so a
    # mutant cannot drop executable code into site-packages for a later un-sandboxed
    # phase to run. Suppress bytecode writes so that read-only venv is not itself a
    # failure mode.
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    return out
