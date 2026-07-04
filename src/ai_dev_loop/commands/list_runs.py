"""Read-only list command."""

from __future__ import annotations

import json

from ai_dev_loop.run_discovery import list_run_directories


def render_list(
    *,
    project: str | None = None,
    status: str | None = None,
    output: str = "text",
) -> str:
    runs = list_run_directories(project=project, status=status)
    if output == "json":
        payload = {
            "schema_version": 1,
            "runs": [
                {
                    "run_id": state.run_id,
                    "project": state.project.name,
                    "status": state.status.value,
                    "created_at": state.created_at.isoformat(),
                    "repository": state.repository.root,
                }
                for _, state in runs
            ],
        }
        return json.dumps(payload, indent=2) + "\n"

    if not runs:
        return "No runs found.\n"
    lines = ["run_id\tstatus\tproject\tcreated_at"]
    for _, state in runs:
        lines.append(
            f"{state.run_id}\t{state.status.value}\t{state.project.name}\t{state.created_at.isoformat()}"
        )
    return "\n".join(lines) + "\n"
