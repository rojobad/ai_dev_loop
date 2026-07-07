"""Integration status, install, uninstall, and session bridge commands."""

from __future__ import annotations

from ai_dev_loop.integrations.codex.desktop_bridge import (
    collect_bridge_status,
    install_desktop_bridge,
    list_desktop_sessions,
    remove_desktop_bridge,
    render_bridge_status,
    render_session_list,
)
from ai_dev_loop.integrations.codex.install import (
    install_integrations,
    render_install_output,
    render_integrations_status,
    render_uninstall_output,
    uninstall_integrations,
)
from ai_dev_loop.integrations.codex.target import CodexIntegrationTarget, parse_target

__all__ = [
    "CodexIntegrationTarget",
    "collect_bridge_status",
    "install_desktop_bridge",
    "install_integrations",
    "list_desktop_sessions",
    "parse_target",
    "remove_desktop_bridge",
    "render_bridge_status",
    "render_install_output",
    "render_integrations_status",
    "render_session_list",
    "render_uninstall_output",
    "uninstall_integrations",
]
