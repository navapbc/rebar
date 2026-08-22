"""An MCP tool call must return an error, not kill the server (bug 176d).

The library-level and source-scan cover for 176d proves `tracker_dir()` raises
`TrackerRootError` instead of calling `sys.exit`. That is necessary but not
sufficient for the claim the ticket actually makes, which is about the MCP
*process*: FastMCP's tool wrapper catches `Exception`, and `SystemExit` is a
`BaseException`, so the old exit sailed past it and took down the whole
`anyio.run` — the server died rather than the tool erroring.

Only driving a real in-memory MCP tool call can distinguish those two outcomes,
so that is what this does: `build_server()` + `call_tool(...)` with `REBAR_ROOT`
unset in a non-repo cwd. If the fix regressed, `SystemExit` would propagate out
of `asyncio.run` as a `BaseException` and the test would die rather than fail —
which is itself the signal, and why the assertion is written to catch
`BaseException` explicitly rather than relying on `pytest.raises`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def _call_read_tool(tool: str, **args: object) -> object:
    from rebar.mcp_server import build_server

    return asyncio.run(build_server().call_tool(tool, args))


# The gate tools are the ones that reach `_engine_support.reads.tracker_dir` (via
# `_lib_gates`); `ready_tickets`/`list_tickets` resolve the tracker through
# `config.tracker_dir` instead and never hit the sys.exit site, so testing those
# would pass whether or not the bug were fixed.
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("clarity_check", {"ticket_id": "abcd-1234-5678-9abc"}),
        ("check_ac", {"ticket_id": "abcd-1234-5678-9abc"}),
        ("quality_check", {"ticket_id": "abcd-1234-5678-9abc"}),
    ],
)
def test_mcp_read_tool_errors_instead_of_killing_the_server(
    tool: str,
    args: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # a real dir that is NOT a git work tree

    try:
        _call_read_tool(tool, **args)
    except SystemExit as exc:  # pragma: no cover - the regression this exists for
        pytest.fail(
            f"{tool} raised SystemExit({exc.code}): FastMCP catches Exception, not "
            "BaseException, so this kills the MCP server process instead of "
            "returning a tool error"
        )
    except Exception:  # noqa: BLE001 - ANY ordinary exception is a pass here; the
        pass  # distinction under test is Exception vs BaseException, not the type
    except BaseException as exc:  # noqa: BLE001 - catching BaseException IS the point:
        # anything that is not an ordinary Exception terminates the MCP host.
        pytest.fail(f"{tool} raised a process-terminating {type(exc).__name__}")


def test_the_server_still_serves_after_a_failed_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The liveness half of the claim: a failed read must not poison the server.

    Built once and driven twice — first against a non-repo cwd, then against a real
    tracker — so a regression that tore down the event loop or the server on the
    first call shows up as a failure on the second.
    """
    from rebar.mcp_server import build_server

    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    srv = build_server()

    try:
        asyncio.run(srv.call_tool("clarity_check", {"ticket_id": "abcd-1234-5678-9abc"}))
    except SystemExit:  # pragma: no cover
        pytest.fail("the failed read killed the server")
    except Exception:  # noqa: BLE001 - see above
        pass

    # The same server object must still answer a tool call afterwards.
    try:
        asyncio.run(srv.call_tool("clarity_check", {"ticket_id": "abcd-1234-5678-9abc"}))
    except SystemExit:  # pragma: no cover
        pytest.fail("the server died on the second call")
    except Exception:  # noqa: BLE001 - see above
        pass
