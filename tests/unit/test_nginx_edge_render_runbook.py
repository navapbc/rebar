from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


RUNBOOK = Path("infra/runbooks/nginx-edge-render.md")


def test_nginx_edge_render_sources_template_from_mirror() -> None:
    text = RUNBOOK.read_text()

    assert ("git -C /var/lib/rebar/mirror show origin/main:infra/nginx/rebar.conf.template") in text


def test_nginx_edge_render_rejects_opt_rebar_as_git_source() -> None:
    text = RUNBOOK.read_text()

    assert "`/opt/rebar` is an rsync copy, not a git checkout" in text


def test_nginx_edge_render_documents_current_review_bot_port_render() -> None:
    text = RUNBOOK.read_text()

    assert "grep -c '\\${REVIEW_BOT_PORT}' /etc/nginx/conf.d/rebar.conf  # expect 0" in text
    assert "Any remaining literal `${REVIEW_BOT_PORT}` means" in text


def test_nginx_edge_render_preserves_proxy_timeout_check() -> None:
    text = RUNBOOK.read_text()

    assert (
        "grep -c proxy_read_timeout /etc/nginx/conf.d/rebar.conf     # expect 4 "
        "(3x /mcp @3600s, 1x Gerrit @600s)"
    ) in text
