"""Require loadgroup scheduling when external Jira tests use xdist_group.

Other xdist schedulers ignore the marker and can interleave tests sharing one Jira
project. The guard is inert without Jira credentials and acli. Its pure predicate stays
separate from conftest for direct tests.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

LIVE_JIRA_GROUP = "live_reconcile_e2e"

# All three must be set to non-empty values for a live Jira pass to be possible.
_CREDENTIAL_VARS = ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN")


def live_jira_credentials_available() -> bool:
    """Return True when the environment could actually reach live Jira.

    Requires every credential variable set to a non-empty value AND an ``acli``
    binary resolvable on PATH — the same two conditions the live tests gate on.
    """
    if not all(os.environ.get(name, "").strip() for name in _CREDENTIAL_VARS):
        return False
    return shutil.which("acli") is not None


def _grouped_node_ids(items: list[Any]) -> list[str]:
    """Return the node ids of every item marked with the live-Jira xdist group."""
    grouped: list[str] = []
    for item in items:
        try:
            marker = item.get_closest_marker("xdist_group")
        except AttributeError:  # pragma: no cover — not a real pytest Item
            continue
        if marker is None:
            continue
        names = [*marker.args, marker.kwargs.get("name")]
        if LIVE_JIRA_GROUP in names:
            grouped.append(item.nodeid)
    return grouped


def unconfined_live_jira_reason(config: Any, items: list[Any]) -> str | None:
    """Return a failure message when live-Jira tests would run unconfined, else None.

    A message is returned only when ALL of the following hold:

    1. the run is parallel (``-n`` resolves to an int >= 1);
    2. the distribution mode is anything other than ``loadgroup``;
    3. live Jira credentials AND an ``acli`` binary are actually available;
    4. at least one collected item carries the ``live_reconcile_e2e`` group mark.

    Every other combination is a silent no-op (``None``) — notably a serial run,
    a ``--dist loadgroup`` run, and CI's credential-less ``--dist worksteal`` lane.
    """
    numprocesses = config.getoption("numprocesses", None)
    if not isinstance(numprocesses, int) or isinstance(numprocesses, bool):
        return None
    if numprocesses < 1:
        return None

    dist = config.getoption("dist", None)
    if dist == "loadgroup":
        return None

    if not live_jira_credentials_available():
        return None

    grouped = _grouped_node_ids(items)
    if not grouped:
        return None

    listing = "\n  ".join(grouped)
    return (
        f"Live-Jira confinement violation: {len(grouped)} collected test(s) carry "
        f'`@pytest.mark.xdist_group("{LIVE_JIRA_GROUP}")`, live Jira credentials '
        f"(JIRA_URL/JIRA_USER/JIRA_API_TOKEN + `acli`) ARE present, and this run is "
        f"parallel (-n {numprocesses}) under --dist {dist!r}. pytest-xdist honours "
        f"xdist_group ONLY under `--dist loadgroup`; every other scheduler parses the "
        f"mark and silently DISCARDS it, so these tests would scatter across workers and "
        f"race each other against the one shared live Jira project.\n"
        f"Remedy: re-run with `--dist loadgroup` (or serially, without -n).\n"
        f"Unconfined item(s):\n  " + listing
    )
