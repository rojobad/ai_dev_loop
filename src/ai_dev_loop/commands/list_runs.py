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
                    "is_recovery_successor": state.recovery is not None,
                    "recovery_source_run_id": (
                        None if state.recovery is None else state.recovery.source_run_id
                    ),
                }
                for _, state in runs
            ],
        }
        return json.dumps(payload, indent=2) + "\n"

    if not runs:
        return "No runs found.\n"
    lines = ["run_id\tstatus\tproject\tcreated_at\trecovery"]
    for _, state in runs:
        recovery = "successor" if state.recovery is not None else "-"
        lines.append(
            f"{state.run_id}\t{state.status.value}\t{state.project.name}\t"
            f"{state.created_at.isoformat()}\t{recovery}"
        )
    return "\n".join(lines) + "\n"
