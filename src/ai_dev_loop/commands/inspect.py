"""Read-only inspect command."""

from __future__ import annotations

import json
from pathlib import Path

from ai_dev_loop.run_discovery import load_run


def render_inspect(run_id: str, *, output: str = "text", show_prompts: bool = False) -> str:
    run_path, state = load_run(run_id)
    artifact_paths = _collect_artifacts(run_path)
    if output == "json":
        payload = {
            "schema_version": 1,
            "run_id": state.run_id,
            "status": state.status.value,
            "run_directory": str(run_path),
            "artifacts": artifact_paths,
            "plan": state.plan.model_dump(),
            "prompt": state.prompt.model_dump(),
            "workflow": state.workflow.model_dump(),
        }
        if show_prompts:
            payload["prompt_preview"] = (run_path / state.prompt.snapshot_path).read_text(
                encoding="utf-8"
            )
        return json.dumps(payload, indent=2) + "\n"

    lines = [
        f"Run: {state.run_id}",
        f"Status: {state.status.value}",
        f"Directory: {run_path}",
        "",
        "Plan:",
        f"  repository_path: {state.plan.repository_path}",
        f"  snapshot_path: {state.plan.snapshot_path}",
        f"  sha256: {state.plan.sha256}",
        "",
        "Prompt:",
        f"  source_repository_path: {state.prompt.source_repository_path}",
        f"  snapshot_path: {state.prompt.snapshot_path}",
        f"  sha256: {state.prompt.sha256}",
        "",
    ]
    if state.iterations:
        lines.append("Iterations:")
        for entry in state.iterations:
            number = entry.get("number")
            kind = entry.get("kind")
            lines.append(f"  - number: {number}, kind: {kind}")
            review = entry.get("review")
            if isinstance(review, dict):
                lines.append(
                    "    review: "
                    f"findings={review.get('has_actionable_findings')}, "
                    f"count={review.get('findings_count')}, "
                    f"tests={review.get('tests_status')}"
                )
            codex = entry.get("codex")
            if isinstance(codex, dict):
                report_path = codex.get("report_path")
                if isinstance(report_path, str):
                    lines.append(f"    codex report: {report_path}")
        lines.append("")
    lines.append("Artifacts:")
    for path in artifact_paths:
        lines.append(f"  - {path}")
    if show_prompts:
        lines.extend(
            [
                "",
                "Prompt preview:",
                (run_path / state.prompt.snapshot_path).read_text(encoding="utf-8"),
            ]
        )
    else:
        lines.append("")
        lines.append("Use --show-prompts to print prompt contents.")
    return "\n".join(lines) + "\n"


def _collect_artifacts(run_path: Path) -> list[str]:
    artifacts: list[str] = []
    for path in sorted(run_path.rglob("*")):
        if path.is_file():
            artifacts.append(str(path.relative_to(run_path)))
    return artifacts
