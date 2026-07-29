"""Read-only GitHub CLI readiness checks for optional PR review."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ai_dev_loop.config import resolve_effective_config
from ai_dev_loop.paths import schema_path
from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.runners.github import check_gh_auth


def github_doctor(*, repo_path: Path | None = None, output: str = "text") -> str:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    gh_path = shutil.which("gh")
    add("gh_cli", gh_path is not None, gh_path or "not found")
    for schema_name in (
        "pr-review-v2-external-adjudication-v1.json",
        "pr-review-v2-publication-generation-v1.json",
    ):
        path = schema_path(schema_name)
        add(f"schema:{schema_name}", path.is_file(), str(path))

    command = "gh"
    github_enabled = False
    if repo_path is not None:
        try:
            info = discover_repository(repo_path)
            effective, _, _ = resolve_effective_config(repo_root=info.root)
            github_enabled = effective.github_enabled()
            if effective.github is not None:
                command = effective.github.command
            add("github_enabled", github_enabled, "true" if github_enabled else "false")
        except Exception as exc:
            add("repo_config", False, str(exc))

    if gh_path or command != "gh":
        auth = check_gh_auth(command)
        add("gh_auth", auth.authenticated, auth.detail)
        if auth.login_redacted:
            add("gh_login", True, auth.login_redacted)

    if repo_path is not None and github_enabled:
        try:
            info = discover_repository(repo_path)
            from ai_dev_loop.runners.publish import resolve_upstream, verify_ssh_push_ready

            remote, _ = resolve_upstream(info.root, info.branch)
            verify_ssh_push_ready(info.root, remote)
            add("ssh_push_ready", True, f"remote={remote}")
        except Exception as exc:
            add("ssh_push_ready", False, str(exc))

    if output == "json":
        return json.dumps({"schema_version": 1, "checks": checks}, indent=2) + "\n"
    lines = ["ai_dev_loop github doctor"]
    for check in checks:
        status = "ok" if check["ok"] else "fail"
        lines.append(f"[{status}] {check['name']}: {check['detail']}")
    return "\n".join(lines) + "\n"
