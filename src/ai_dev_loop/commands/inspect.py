"""Read-only inspect command."""

from __future__ import annotations

import json
from pathlib import Path

from ai_dev_loop.abort_control import (
    ABORT_REQUEST_REL_PATH,
    ACTIVE_PROCESS_REL_PATH,
    abort_control_summary,
)
from ai_dev_loop.config import format_codex_override
from ai_dev_loop.run_discovery import load_run


def render_inspect(run_id: str, *, output: str = "text", show_prompts: bool = False) -> str:
    run_path, state = load_run(run_id)
    artifact_paths = _collect_artifacts(run_path)
    control = abort_control_summary(run_path)
    if output == "json":
        payload = {
            "schema_version": 1,
            "run_id": state.run_id,
            "status": state.status.value,
            "run_directory": str(run_path),
            "artifacts": artifact_paths,
            "abort_control": control,
            "abort_control_paths": [
                str(ABORT_REQUEST_REL_PATH),
                str(ACTIVE_PROCESS_REL_PATH),
            ],
            "plan": state.plan.model_dump(),
            "prompt": state.prompt.model_dump(),
            "codex": {
                "session_model": state.codex.session_model,
                "session_reasoning_effort": state.codex.session_reasoning_effort,
                "review_model": state.codex.review_model,
                "review_reasoning_effort": state.codex.review_reasoning_effort,
                "review_model_source": state.codex.review_model_source,
                "review_reasoning_source": state.codex.review_reasoning_source,
                "model_family_warning": state.codex.model_family_warning,
                "review_skill": state.codex.review_skill,
                "sandbox": state.codex.sandbox,
                "command": state.codex.command,
            },
            "workflow": state.workflow.model_dump(),
            "recovery": None
            if state.recovery is None
            else {
                "source_run_id": state.recovery.source_run_id,
                "source_status": state.recovery.source_status,
                "source_iteration": state.recovery.source_iteration,
                "recovered_checkpoint": state.recovery.recovered_checkpoint,
                "source_staged_patch_sha256": state.recovery.source_staged_patch_sha256,
                "cursor_output_fingerprint_sha256": (
                    state.recovery.cursor_output_fingerprint_sha256
                ),
                "previous_staged_patch_sha256": state.recovery.previous_staged_patch_sha256,
                "legacy_cursor_output_adopted": state.recovery.legacy_cursor_output_adopted,
                "source_cursor_model": state.recovery.source_cursor_model,
                "cursor_model_fallback": state.recovery.cursor_model_fallback,
                "source_prompt_path": state.recovery.source_prompt_path,
                "source_prompt_sha256": state.recovery.source_prompt_sha256,
                "usage_limit_fingerprint_sha256": state.recovery.usage_limit_fingerprint_sha256,
                "usage_limit_fingerprint_path": state.recovery.usage_limit_fingerprint_path,
                "continuation_envelope_path": state.recovery.continuation_envelope_path,
                "continuation_envelope_sha256": state.recovery.continuation_envelope_sha256,
                "created_at": state.recovery.created_at.isoformat(),
                "runtime_migration": state.recovery.runtime_migration,
                "reason_code": state.recovery.reason_code,
            },
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
        "Codex:",
        f"  session_model: {state.codex.session_model or '(unset)'}",
        f"  session_reasoning_effort: {state.codex.session_reasoning_effort or '(unset)'}",
        (
            f"  review_model: {format_codex_override(state.codex.review_model)}"
            + (f" ({state.codex.review_model_source})" if state.codex.review_model_source else "")
        ),
        (
            f"  review_reasoning_effort: "
            f"{format_codex_override(state.codex.review_reasoning_effort)}"
            + (
                f" ({state.codex.review_reasoning_source})"
                if state.codex.review_reasoning_source
                else ""
            )
        ),
        f"  review_skill: {state.codex.review_skill}",
        f"  sandbox: {state.codex.sandbox}",
        "",
    ]
    if state.recovery is not None:
        lines[3:3] = [
            "Recovery lineage:",
            f"  source_run_id: {state.recovery.source_run_id}",
            f"  source_status: {state.recovery.source_status}",
            f"  source_iteration: {state.recovery.source_iteration}",
            f"  recovered_checkpoint: {state.recovery.recovered_checkpoint}",
            f"  runtime_migration: {state.recovery.runtime_migration}",
            f"  reason_code: {state.recovery.reason_code}",
            f"  source_staged_patch_sha256: {state.recovery.source_staged_patch_sha256}",
            (
                f"  cursor_output_fingerprint_sha256: "
                f"{state.recovery.cursor_output_fingerprint_sha256 or '(none)'}"
            ),
            (f"  legacy_cursor_output_adopted: {state.recovery.legacy_cursor_output_adopted}"),
            f"  source_cursor_model: {state.recovery.source_cursor_model or '(none)'}",
            f"  cursor_model_fallback: {state.recovery.cursor_model_fallback or '(none)'}",
            (
                f"  continuation_envelope_path: "
                f"{state.recovery.continuation_envelope_path or '(none)'}"
            ),
            (
                f"  usage_limit_fingerprint_sha256: "
                f"{state.recovery.usage_limit_fingerprint_sha256 or '(none)'}"
            ),
            "",
        ]
    if state.codex.model_family_warning:
        lines.insert(-1, f"  model_family_warning: {state.codex.model_family_warning}")
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
    lines.append("Abort control:")
    lines.append(f"  abort_request_path: {ABORT_REQUEST_REL_PATH}")
    lines.append(f"  active_process_path: {ACTIVE_PROCESS_REL_PATH}")
    lines.append(f"  abort_requested: {control['abort_requested']}")
    lines.append(f"  active_process_registered: {control['active_process_registered']}")
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
