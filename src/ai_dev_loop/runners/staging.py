"""Git staging runner after successful Cursor execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import (
    discover_repository,
    git_add_all,
    git_diff_cached_name_only,
    git_diff_cached_patch,
    git_diff_cached_stat,
    git_status_porcelain,
    staged_paths_from_name_only,
    validate_no_preexisting_staged_paths,
    validate_plan_hash_unchanged,
    validate_prompt_source_unchanged,
    validate_stage_mode,
    validate_staged_paths_safe,
)
from ai_dev_loop.state import RunState, atomic_write_text, utc_now

PHASE_3_BOUNDARY_MESSAGE = (
    "Git staging is complete. Codex review, corrections, and completion are not implemented yet."
)

# Retained for historical references in tests/docs; Phase 4 start continues past staging.


@dataclass(frozen=True)
class GitStagingArtifacts:
    before_staging_path: str
    after_staging_path: str
    stat_path: str
    name_only_path: str
    patch_path: str


@dataclass(frozen=True)
class GitStagingResult:
    artifacts: GitStagingArtifacts
    staged_paths: tuple[str, ...]


def validate_pre_staging(state: RunState, repo_root: Path) -> None:
    validate_stage_mode(state.workflow.stage_mode)

    repo_info = discover_repository(repo_root)
    validate_no_preexisting_staged_paths(repo_info.staged_paths)

    validate_plan_hash_unchanged(repo_root, state.plan.repository_path, state.plan.sha256)

    status = git_status_porcelain(repo_root)
    prompt_source = repo_root / state.prompt.source_repository_path
    validate_prompt_source_unchanged(
        status,
        state.prompt.source_repository_path,
        repo_root=repo_root,
        prompt_source_path=prompt_source,
    )


def run_git_staging(
    state: RunState,
    run_directory: Path,
    *,
    iteration: str,
    cursor_started_at: datetime,
    cursor_exit_code: int,
) -> GitStagingResult:
    repo_root = Path(state.repository.root)
    validate_pre_staging(state, repo_root)

    before_staging_rel = f"git/status/{iteration}-before-staging.txt"
    after_staging_rel = f"git/status/{iteration}-after-staging.txt"
    stat_rel = f"git/diffs/{iteration}.stat"
    name_only_rel = f"git/diffs/{iteration}.name-only.txt"
    patch_rel = f"git/diffs/{iteration}.patch"

    before_staging_path = run_directory / before_staging_rel
    after_staging_path = run_directory / after_staging_rel
    stat_path = run_directory / stat_rel
    name_only_path = run_directory / name_only_rel
    patch_path = run_directory / patch_rel

    before_status = git_status_porcelain(repo_root)
    atomic_write_text(before_staging_path, before_status + "\n")

    add_result = git_add_all(repo_root)
    if add_result.returncode != 0:
        detail = add_result.stderr.strip() or add_result.stdout.strip() or "git add -A failed"
        raise ValidationError(f"git add -A failed: {detail}")

    after_status = git_status_porcelain(repo_root)
    atomic_write_text(after_staging_path, after_status + "\n")

    stat_output = git_diff_cached_stat(repo_root)
    name_only_output = git_diff_cached_name_only(repo_root)
    patch_output = git_diff_cached_patch(repo_root)

    atomic_write_text(stat_path, stat_output + ("\n" if stat_output else ""))
    atomic_write_text(name_only_path, name_only_output + ("\n" if name_only_output else ""))
    atomic_write_text(patch_path, patch_output, sensitive=True)

    staged_paths = staged_paths_from_name_only(name_only_output)
    validate_staged_paths_safe(
        staged_paths,
        plan_repo_path=state.plan.repository_path,
        prompt_repo_path=state.prompt.source_repository_path,
        plan_hash=state.plan.sha256,
        repo_root=repo_root,
    )

    artifacts = GitStagingArtifacts(
        before_staging_path=before_staging_rel,
        after_staging_path=after_staging_rel,
        stat_path=stat_rel,
        name_only_path=name_only_rel,
        patch_path=patch_rel,
    )
    _record_iteration(
        state,
        iteration=iteration,
        cursor_exit_code=cursor_exit_code,
        artifacts=artifacts,
        started_at=cursor_started_at,
        completed_at=utc_now(),
    )
    return GitStagingResult(artifacts=artifacts, staged_paths=staged_paths)


def _record_iteration(
    state: RunState,
    *,
    iteration: str,
    cursor_exit_code: int,
    artifacts: GitStagingArtifacts,
    started_at: datetime,
    completed_at: datetime,
) -> None:
    entry: dict[str, Any] = {
        "number": int(iteration),
        "kind": "initial_implementation",
        "started_at": started_at.astimezone(UTC).isoformat(),
        "completed_at": completed_at.astimezone(UTC).isoformat(),
        "cursor": {
            "prompt_path": state.prompt.snapshot_path,
            "events_path": f"cursor/iterations/{iteration}/events.jsonl",
            "stderr_path": f"cursor/iterations/{iteration}/stderr.txt",
            "metadata_path": f"cursor/iterations/{iteration}/metadata.json",
            "final_message_path": f"cursor/iterations/{iteration}/final.txt",
            "exit_code": cursor_exit_code,
        },
        "git": {
            "status_before_cursor_path": f"git/status/{iteration}-before-cursor.txt",
            "status_after_cursor_path": f"git/status/{iteration}-after-cursor.txt",
            "status_before_staging_path": artifacts.before_staging_path,
            "status_after_staging_path": artifacts.after_staging_path,
            "staged_stat_path": artifacts.stat_path,
            "staged_name_only_path": artifacts.name_only_path,
            "staged_diff_path": artifacts.patch_path,
        },
    }
    if state.iterations:
        state.iterations[0] = entry
    else:
        state.iterations.append(entry)


def staging_complete(state: RunState, run_directory: Path) -> bool:
    if state.iterations:
        git_section = state.iterations[0].get("git", {})
        staged_diff_path = git_section.get("staged_diff_path")
        if isinstance(staged_diff_path, str):
            return (run_directory / staged_diff_path).is_file()
    patch = run_directory / "git" / "diffs" / "01.patch"
    return patch.is_file()
