"""Safe environment mappings for subprocesses started by tests.

Pytest includes function arguments in its default long traceback.  A plain
``dict`` therefore exposes every inherited environment value when subprocess
startup fails.  Keep the values available to the child while making the
mapping's diagnostic representation safe.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Self


class SubprocessEnv(dict[str, str]):
    """A normal mutable subprocess environment with a redacted repr."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<{len(self)} values redacted>)"

    def copy(self) -> Self:
        """Copy without dropping the representation-safety boundary."""
        return type(self)(self)

    def with_overrides(self, overrides: Mapping[str, str] | None = None, /, **kwargs: str) -> Self:
        """Return a boundary-preserving copy updated with supplied values."""
        result = self.copy()
        if overrides is not None:
            result.update(overrides)
        result.update(kwargs)
        return result


def subprocess_env(overrides: Mapping[str, str] | None = None, /, **kwargs: str) -> SubprocessEnv:
    """Copy the ambient environment into a representation-safe mapping."""
    return SubprocessEnv(os.environ).with_overrides(overrides, **kwargs)
