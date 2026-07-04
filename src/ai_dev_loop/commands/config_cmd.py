"""Config validation command."""

from __future__ import annotations

import json
from pathlib import Path

from ai_dev_loop.config import load_project_config, resolve_effective_config
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import discover_repository


def validate_config(
    *,
    repo_path: Path | None = None,
    config_path: Path | None = None,
    output: str = "text",
) -> str:
    repo_candidate = repo_path or Path.cwd()
    repo_info = discover_repository(repo_candidate)
    resolved_config_path = config_path
    if resolved_config_path is not None:
        resolved_config_path = (
            resolved_config_path
            if resolved_config_path.is_absolute()
            else (repo_info.root / resolved_config_path).resolve()
        )
    effective, source, repo_config_path = resolve_effective_config(
        repo_root=repo_info.root,
        config_path=resolved_config_path,
    )
    _ = load_project_config(repo_config_path)
    payload = {
        "schema_version": 1,
        "status": "valid",
        "project": effective.project.name,
        "config_path": str(repo_config_path),
        "source_project_name": source.project.name,
        "effective_project_name": effective.project.name,
    }
    if output == "json":
        return json.dumps(payload, indent=2) + "\n"
    return (
        f"Configuration is valid for project '{effective.project.name}'.\n"
        f"Config path: {repo_config_path}\n"
    )


def run_validate_config(
    *,
    repo_path: Path | None = None,
    config_path: Path | None = None,
    output: str = "text",
) -> str:
    try:
        return validate_config(repo_path=repo_path, config_path=config_path, output=output)
    except ValidationError:
        raise
