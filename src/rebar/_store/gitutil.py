"""One shared ``git`` subprocess wrapper.

A leaf helper (stdlib only — ``os``/``subprocess``; imports nothing from
``rebar.*``) that consolidates the dozen hand-rolled ``_git()`` wrappers that had
drifted into a different signature/return/error contract each. Every wrapper ran
the identical shape underneath — ``subprocess.run(["git", "-C", cwd, *args],
capture_output=True, text=True, …)`` — so :func:`run_git` is that shape once, and
each call site keeps its OWN return/error contract by adapting the returned
:class:`subprocess.CompletedProcess` locally (inspect ``returncode``/``stdout``,
raise its own exception, translate a timeout, …).

NEVER ``shell=True`` — argv is a list, so a git argument can never be reinterpreted
by a shell. This helper does not redact: it returns the ``CompletedProcess``
verbatim, and any token/secret redaction stays where it already lives — in the
caller that formats ``stderr`` into a message.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping


def run_git(
    cwd: str | os.PathLike[str] | None,
    *args: str,
    check: bool = True,
    capture_output: bool = True,
    text: bool = True,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    input_data: str | bytes | None = None,
) -> subprocess.CompletedProcess:
    """Run ``git -C <cwd> <args…>`` and return the :class:`subprocess.CompletedProcess`.

    A thin, uniform wrapper over :func:`subprocess.run` for the tickets-store git
    plumbing. Defaults match the historical wrappers' common shape (capture stdout
    and stderr, decode as text). ``check=True`` raises
    :class:`subprocess.CalledProcessError` on a non-zero exit (call sites that
    inspect ``returncode`` or raise their own error pass ``check=False``);
    ``timeout`` (when set) lets :class:`subprocess.TimeoutExpired` propagate — a
    caller that wants a timeout folded into a synthetic failed result catches it
    itself. ``env=None`` inherits the current environment.

    ``cwd=None`` omits the ``-C <cwd>`` prefix entirely, running ``git`` in the
    process CWD (some callers verify commits relative to the caller's directory
    rather than a fixed repo). ``input_data`` (when set) is fed to git's stdin —
    forwarded to :func:`subprocess.run`'s ``input`` for e.g. ``git hash-object``.
    """
    argv = ["git", *args] if cwd is None else ["git", "-C", cwd, *args]
    return subprocess.run(
        argv,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
        input=input_data,
    )
