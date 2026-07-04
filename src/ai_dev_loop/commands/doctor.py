"""Non-mutating doctor command."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from ai_dev_loop import __version__
from ai_dev_loop.config import resolve_effective_config
from ai_dev_loop.paths import cache_dir, config_dir, ensure_app_dirs, schema_path, state_dir
from ai_dev_loop.process import run_process
from ai_dev_loop.runners.git import discover_repository


def render_doctor(*, repo_path: Path | None = None, output: str = "text") -> str:
    ensure_app_dirs()
    checks: list[dict[str, str | bool]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    py_ok = sys.version_info >= (3, 11)
    add(
        "python",
        py_ok,
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )

    for label, path in (
        ("config_dir", config_dir()),
        ("state_dir", state_dir()),
        ("cache_dir", cache_dir()),
    ):
        add(label, path.is_dir(), str(path))

    for schema_name in (
        "project-config-v1.json",
        "run-state-v1.json",
        "codex-review-result-v1.json",
    ):
        path = schema_path(schema_name)
        add(f"schema:{schema_name}", path.is_file(), str(path))

    git_path = shutil.which("git")
    add("git", git_path is not None, git_path or "not found")
    agent_path = shutil.which("agent")
    add("cursor_cli", agent_path is not None, agent_path or "not found")
    codex_path = shutil.which("codex")
    add("codex_cli", codex_path is not None, codex_path or "not found")

    if repo_path is not None:
        try:
            repo_info = discover_repository(repo_path)
            effective, _, repo_config_path = resolve_effective_config(repo_root=repo_info.root)
            add(
                "repo_config",
                repo_config_path.is_file(),
                f"{repo_config_path} ({effective.project.name})",
            )
        except Exception as exc:
            add("repo_config", False, str(exc))

    if agent_path:
        result = run_process(["agent", "--version"])
        add(
            "cursor_version", result.returncode == 0, result.stdout.strip() or result.stderr.strip()
        )
    if codex_path:
        result = run_process(["codex", "--version"])
        add("codex_version", result.returncode == 0, result.stdout.strip() or result.stderr.strip())

    add("package_version", True, __version__)

    if output == "json":
        return json.dumps({"schema_version": 1, "checks": checks}, indent=2) + "\n"

    lines = [f"ai_dev_loop doctor ({__version__})"]
    for check in checks:
        status = "ok" if check["ok"] else "fail"
        lines.append(f"[{status}] {check['name']}: {check['detail']}")
    return "\n".join(lines) + "\n"
