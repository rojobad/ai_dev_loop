"""Git staging runner after successful Cursor execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.iterations import (
    find_iteration,
    iteration_kind,
    iteration_label,
    upsert_iteration,
)
from ai_dev_loop.runners.git import (
    discover_repository,
    git_add_all,
    git_diff_cached_name_only,
    git_diff_cached_patch,
    git_diff_cached_stat,
    git_status_porcelain,
    normalize_patch_text,
    staged_paths_from_name_only,
    validate_no_preexisting_staged_paths,
    validate_plan_hash_unchanged,
    validate_prompt_source_unchanged,
    validate_stage_mode,
    validate_staged_paths_safe,
)
from ai_dev_loop.state import RunState, atomic_write_text, utc_now


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


def validate_pre_staging(
    state: RunState,
    repo_root: Path,
    *,
    iteration_number: int,
    run_directory: Path,
) -> None:
    validate_stage_mode(state.workflow.stage_mode)

    repo_info = discover_repository(repo_root)
    if iteration_number == 1:
        validate_no_preexisting_staged_paths(repo_info.staged_paths)
    else:
        previous = find_iteration(state, iteration_number - 1)
        if previous is None:
            raise ValidationError(
                f"previous iteration metadata missing before staging iteration "
                f"{iteration_label(iteration_number)}"
            )
        git_section = previous.get("git")
        if not isinstance(git_section, dict):
            raise ValidationError("previous iteration git metadata is missing")
        patch_rel = git_section.get("staged_diff_path")
        if not isinstance(patch_rel, str):
            raise ValidationError("previous iteration staged diff path is missing")
        patch_artifact = run_directory / patch_rel
        if not patch_artifact.is_file():
            raise ValidationError(f"previous staged patch artifact missing: {patch_rel}")
        recorded = normalize_patch_text(patch_artifact.read_text(encoding="utf-8"))
        current = normalize_patch_text(git_diff_cached_patch(repo_root))
        if current != recorded:
            raise ValidationError(
                "staged index changed before correction staging; "
                "expected the previous orchestrator-recorded staged patch"
            )

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
    iteration_number: int,
    cursor_started_at: datetime,
    cursor_exit_code: int,
    prompt_path: str,
) -> GitStagingResult:
    repo_root = Path(state.repository.root)
    validate_pre_staging(
        state,
        repo_root,
        iteration_number=iteration_number,
        run_directory=run_directory,
    )

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
        iteration_number=iteration_number,
        cursor_exit_code=cursor_exit_code,
        prompt_path=prompt_path,
        artifacts=artifacts,
        started_at=cursor_started_at,
        completed_at=utc_now(),
    )
    return GitStagingResult(artifacts=artifacts, staged_paths=staged_paths)


def _record_iteration(
    state: RunState,
    *,
    iteration: str,
    iteration_number: int,
    cursor_exit_code: int,
    prompt_path: str,
    artifacts: GitStagingArtifacts,
    started_at: datetime,
    completed_at: datetime,
) -> None:
    existing = find_iteration(state, iteration_number)
    cursor_section: dict[str, Any] = {
        "prompt_path": prompt_path,
        "events_path": f"cursor/iterations/{iteration}/events.jsonl",
        "stderr_path": f"cursor/iterations/{iteration}/stderr.txt",
        "metadata_path": f"cursor/iterations/{iteration}/metadata.json",
        "final_message_path": f"cursor/iterations/{iteration}/final.txt",
        "exit_code": cursor_exit_code,
    }
    if existing and isinstance(existing.get("cursor"), dict):
        cursor_section = {**existing["cursor"], **cursor_section}

    entry: dict[str, Any] = {
        "number": iteration_number,
        "kind": iteration_kind(iteration_number),
        "started_at": (
            existing.get("started_at")
            if existing and isinstance(existing.get("started_at"), str)
            else started_at.astimezone(UTC).isoformat()
        ),
        "completed_at": completed_at.astimezone(UTC).isoformat(),
        "cursor": cursor_section,
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
    upsert_iteration(state, entry)


def staging_complete(state: RunState, run_directory: Path, *, iteration: str | None = None) -> bool:
    if iteration is not None:
        return staging_complete_for_iteration(state, run_directory, iteration)
    if state.iterations:
        latest = max(
            entry["number"] for entry in state.iterations if isinstance(entry.get("number"), int)
        )
        return staging_complete_for_iteration(state, run_directory, iteration_label(latest))
    patch = run_directory / "git" / "diffs" / "01.patch"
    return patch.is_file()


def staging_complete_for_iteration(
    state: RunState,
    run_directory: Path,
    iteration: str,
) -> bool:
    number = int(iteration)
    entry = find_iteration(state, number)
    if entry:
        git_section = entry.get("git", {})
        if isinstance(git_section, dict):
            staged_diff_path = git_section.get("staged_diff_path")
            if isinstance(staged_diff_path, str) and (run_directory / staged_diff_path).is_file():
                return True
    patch = run_directory / f"git/diffs/{iteration}.patch"
    return patch.is_file()
