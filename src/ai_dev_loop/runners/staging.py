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
from ai_dev_loop.runners.cursor_output import (
    capture_staging_normalization_fingerprint,
    cursor_output_fingerprint_rel_path,
    staging_normalization_fingerprint_rel_path,
)
from ai_dev_loop.runners.git import (
    git_add_all,
    git_diff_cached_name_only,
    git_diff_cached_patch_bytes,
    git_diff_cached_stat,
    git_status_porcelain,
    staged_paths_from_name_only,
    validate_clean_after_stage_all,
    validate_plan_hash_unchanged,
    validate_prompt_source_unchanged,
    validate_repository_identity,
    validate_stage_mode,
    validate_staged_paths_safe,
)
from ai_dev_loop.state import RunState, atomic_write_bytes, atomic_write_text, utc_now


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
    cursor_changed_index: bool


def validate_pre_staging(
    state: RunState,
    repo_root: Path,
    *,
    iteration_number: int,
    run_directory: Path,
) -> None:
    """Validate post-Cursor repository contracts before orchestrator `git add -A`.

    Initial and correction turns allow Cursor to mutate the index after the agent
    starts. The empty-index / pre-existing staged check belongs only to the
    trusted pre-Cursor boundary (prepare and start baseline). Correction
    staged-patch equality belongs only to the pre-Cursor correction boundary.
    """

    del run_directory  # retained for call-site compatibility
    del iteration_number  # retained for call-site compatibility
    validate_stage_mode(state.workflow.stage_mode)

    validate_repository_identity(
        repo_root,
        expected_root=state.repository.root,
        expected_git_common_dir=state.repository.git_common_dir,
        expected_git_dir=state.repository.git_dir,
        expected_branch=state.repository.branch,
        expected_head=state.repository.initial_head,
        context="before staging",
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


def _index_signature_lines(status: str) -> frozenset[str]:
    """Return index-only signatures from porcelain v2 status.

    Compares staged membership and index blob identity without worktree-only
    fields (Y status and mW). Same-path staged content changes still differ via
    hI; worktree-only edits such as ``M.`` -> ``MM`` do not.
    """

    signatures: set[str] = set()
    for line in status.splitlines():
        signature = _index_only_signature(line)
        if signature is not None:
            signatures.add(signature)
    return frozenset(signatures)


def _index_only_signature(line: str) -> str | None:
    """Parse one porcelain v2 line into an index-only comparison key."""

    if line.startswith("1 "):
        # 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
        parts = line.split(" ", 8)
        if len(parts) < 9:
            return None
        xy = parts[1]
        if not xy or xy[0] == ".":
            return None
        return "\t".join(("1", xy[0], parts[4], parts[7], parts[8]))
    if line.startswith("2 "):
        # 2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <path><sep><origPath>
        parts = line.split(" ", 9)
        if len(parts) < 10:
            return None
        xy = parts[1]
        if not xy or xy[0] == ".":
            return None
        return "\t".join(("2", xy[0], parts[4], parts[7], parts[8], parts[9]))
    if line.startswith("u "):
        # u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>
        parts = line.split(" ", 10)
        if len(parts) < 11:
            return None
        return "\t".join(
            ("u", parts[1], parts[3], parts[4], parts[5], parts[7], parts[8], parts[9], parts[10])
        )
    return None


def _cursor_changed_index(repo_root: Path, run_directory: Path, iteration: str) -> bool:
    before_path = run_directory / f"git/status/{iteration}-before-cursor.txt"
    after_path = run_directory / f"git/status/{iteration}-after-cursor.txt"
    if not before_path.is_file() or not after_path.is_file():
        # Fall back to whether the current index already has entries beyond empty.
        return bool(_index_signature_lines(git_status_porcelain(repo_root)))
    before = before_path.read_text(encoding="utf-8")
    after = after_path.read_text(encoding="utf-8")
    return _index_signature_lines(before) != _index_signature_lines(after)


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

    cursor_changed_index = _cursor_changed_index(repo_root, run_directory, iteration)

    before_status = git_status_porcelain(repo_root)
    atomic_write_text(before_staging_path, before_status + "\n")

    # Always normalize with configured stage_mode=all, even if Cursor already staged.
    add_result = git_add_all(repo_root)
    if add_result.returncode != 0:
        detail = add_result.stderr.strip() or add_result.stdout.strip() or "git add -A failed"
        raise ValidationError(f"git add -A failed: {detail}")

    validate_repository_identity(
        repo_root,
        expected_root=state.repository.root,
        expected_git_common_dir=state.repository.git_common_dir,
        expected_git_dir=state.repository.git_dir,
        expected_branch=state.repository.branch,
        expected_head=state.repository.initial_head,
        context="after git add -A",
    )
    validate_clean_after_stage_all(repo_root)

    # Persist immediately after successful normalization so recovery can verify
    # post-add state even if later artifact writes fail.
    normalization = capture_staging_normalization_fingerprint(
        state,
        run_directory,
        iteration_number=iteration_number,
    )
    upsert_iteration(
        state,
        {
            "number": iteration_number,
            "git": {
                "staging_normalization_fingerprint_path": normalization.relative_path,
            },
        },
    )

    after_status = git_status_porcelain(repo_root)
    atomic_write_text(after_staging_path, after_status + "\n")

    stat_output = git_diff_cached_stat(repo_root)
    name_only_output = git_diff_cached_name_only(repo_root)
    patch_bytes = git_diff_cached_patch_bytes(repo_root)

    atomic_write_text(stat_path, stat_output + ("\n" if stat_output else ""))
    atomic_write_text(name_only_path, name_only_output + ("\n" if name_only_output else ""))
    atomic_write_bytes(patch_path, patch_bytes, sensitive=True)

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
        normalization_fingerprint_path=normalization.relative_path,
    )
    return GitStagingResult(
        artifacts=artifacts,
        staged_paths=staged_paths,
        cursor_changed_index=cursor_changed_index,
    )


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
    normalization_fingerprint_path: str | None = None,
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

    fingerprint_rel = cursor_output_fingerprint_rel_path(iteration_number)
    norm_rel = normalization_fingerprint_path or staging_normalization_fingerprint_rel_path(
        iteration_number
    )
    git_section: dict[str, Any] = {
        "status_before_cursor_path": f"git/status/{iteration}-before-cursor.txt",
        "status_after_cursor_path": f"git/status/{iteration}-after-cursor.txt",
        "cursor_output_fingerprint_path": fingerprint_rel,
        "staging_normalization_fingerprint_path": norm_rel,
        "status_before_staging_path": artifacts.before_staging_path,
        "status_after_staging_path": artifacts.after_staging_path,
        "staged_stat_path": artifacts.stat_path,
        "staged_name_only_path": artifacts.name_only_path,
        "staged_diff_path": artifacts.patch_path,
    }
    if existing and isinstance(existing.get("git"), dict):
        # Preserve earlier git keys (e.g. fingerprint recorded before staging).
        git_section = {**existing["git"], **git_section}

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
        "git": git_section,
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
