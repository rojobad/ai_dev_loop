"""Explicit GitHub PR review cycle commands (Phase 15)."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_dev_loop.abort_control import is_abort_requested, write_abort_request
from ai_dev_loop.commands.start_preflight import (
    begin_running_cursor,
    mark_aborted,
    mark_completed,
    mark_failed,
    mark_interrupted,
)
from ai_dev_loop.config import ProjectConfig, resolve_effective_config
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.event_log import append_orchestrator_event
from ai_dev_loop.locking import LockMetadata, RunLocks
from ai_dev_loop.paths import ensure_dir, run_dir, runs_dir, set_sensitive_file_mode
from ai_dev_loop.process import require_success, run_process
from ai_dev_loop.redaction import redact_text
from ai_dev_loop.resume_planner import TERMINAL_STATUSES
from ai_dev_loop.run_discovery import list_run_directories, load_run
from ai_dev_loop.runners.codex_github import (
    run_codex_github_review,
    run_codex_publication_text,
)
from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.runners.github import (
    GithubErrorKind,
    check_gh_auth,
    continue_comment_authorized,
    create_issue_comment,
    create_or_update_pull_request,
    filter_eligible_threads,
    find_issue_comment_with_marker,
    get_pull_request,
    list_review_threads,
    reply_to_review_thread,
    resolve_review_thread,
)
from ai_dev_loop.runners.publish import (
    PublicationText,
    publish_accepted_staged_patch,
    validate_clean_except_staged,
)
from ai_dev_loop.state import (
    GithubPrReviewState,
    RunState,
    RunStatus,
    atomic_write_json,
    atomic_write_text,
    generate_run_id,
    save_run_state,
    sha256_text,
    transition_status,
    utc_now,
)

WORKER_LAUNCHER_REL = Path("locks/pr-review-worker.json")
_PUBLICATION_LIFECYCLES = frozenset({"publishing_initial", "publishing_external_fix"})
_PUBLICATION_PHASES = frozenset({"pre_commit", "committed", "pushed", "pr_bound"})


def _publication_in_progress(gpr: GithubPrReviewState | None) -> bool:
    if gpr is None:
        return False
    if gpr.lifecycle in _PUBLICATION_LIFECYCLES:
        return True
    return gpr.publication_phase in _PUBLICATION_PHASES


def _resolve_publication_resume_lifecycle(gpr: GithubPrReviewState) -> str | None:
    """Return publishing_* lifecycle when checkpoint evidence allows resume."""

    if gpr.publication_phase not in _PUBLICATION_PHASES:
        if gpr.lifecycle in _PUBLICATION_LIFECYCLES:
            return gpr.lifecycle
        return None
    if gpr.lifecycle in _PUBLICATION_LIFECYCLES:
        return gpr.lifecycle
    if gpr.pr_number is None:
        return "publishing_initial"
    return "publishing_external_fix"


def _request_marker(*, run_id: str, cycle_number: int, commit_sha: str) -> str:
    return f"ai_dev_loop-pr-review:{run_id}:cycle:{cycle_number}:{commit_sha}"


def _residual_risk_note_for_publication(gpr: GithubPrReviewState) -> str | None:
    """Load residual-risk note from the immutable source run when applicable."""

    if gpr.lifecycle != "publishing_initial":
        return None
    try:
        _, source = load_run(gpr.source_run_id)
    except Exception:
        return None
    if source.status != RunStatus.COMPLETED_WITH_RESIDUAL_RISK:
        return None
    return source.result or "residual risk recorded by local Codex review"


@dataclass(frozen=True)
class PrReviewCreateResult:
    source_run_id: str
    run_id: str
    pr_number: int
    status: str
    message: str


def _lock_metadata(run_id: str, repository_path: str) -> LockMetadata:
    return LockMetadata(
        pid=os.getpid(),
        run_id=run_id,
        repository_path=repository_path,
        started_at=datetime.now(tz=UTC),
    )


def _run_locks(run_directory: Path, *, run_id: str, repository_path: str) -> RunLocks:
    return RunLocks(run_directory, _lock_metadata(run_id, repository_path))


def _require_github_config(repo_root: Path) -> ProjectConfig:
    effective, _, _ = resolve_effective_config(repo_root=repo_root)
    if not effective.github_enabled() or effective.github is None:
        raise ValidationError(
            "GitHub PR review is disabled; set github.enabled: true in ai_dev_loop.yaml"
        )
    return effective


def _copy_identity_artifacts(source_dir: Path, dest_dir: Path) -> None:
    for relative in (
        "plan/plan.md",
        "prompts/cursor-initial.txt",
        "effective-config.yaml",
        "source-config.yaml",
        "codex/session-runtime.json",
        "cursor/chat.json",
    ):
        src = source_dir / relative
        if src.is_file():
            target = dest_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            set_sensitive_file_mode(target)


def _create_successor_layout(temp_dir: Path) -> None:
    for relative in (
        "plan",
        "prompts",
        "prompts/fixes",
        "cursor",
        "codex",
        "git",
        "github/cycles",
        "logs",
        "locks",
    ):
        ensure_dir(temp_dir / relative)


def _find_active_cycle_for_source(source_run_id: str) -> RunState | None:
    for _, state in list_run_directories():
        gpr = state.github_pr_review
        if gpr is None:
            continue
        if gpr.source_run_id != source_run_id:
            continue
        if state.status in TERMINAL_STATUSES:
            continue
        if gpr.lifecycle in {"completed", "failed", "aborted", "max_external_cycles_reached"}:
            continue
        return state
    return None


def _mark_status(state: RunState, new_status: RunStatus) -> None:
    transition_status(state.status, new_status)
    state.status = new_status


def create_pr_review_cycle(source_run_id: str) -> PrReviewCreateResult:
    source_dir, source = load_run(source_run_id)
    if source.status not in {
        RunStatus.COMPLETED,
        RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
    }:
        raise ValidationError(
            "pr-review create requires a source run with status completed or "
            "completed_with_residual_risk"
        )
    if not source.cursor.chat_id:
        raise ValidationError("source run is missing cursor.chat_id")
    if not source.codex.session_id:
        raise ValidationError("source run is missing codex.session_id")
    if _find_active_cycle_for_source(source_run_id) is not None:
        raise ValidationError("an active PR-review cycle already exists for this source run")

    repo_root = Path(source.repository.root)
    config = _require_github_config(repo_root)
    assert config.github is not None
    github = config.github

    auth = check_gh_auth(github.command)
    if not auth.authenticated:
        raise ValidationError(f"gh authentication failed: {auth.detail}")

    # Source must still have the accepted staged patch.
    info = discover_repository(repo_root)
    if info.branch != source.repository.branch:
        raise ValidationError("repository branch drifted from the prepared branch")
    patch = validate_clean_except_staged(repo_root)
    patch_sha = sha256_text(patch)

    locks = _run_locks(source_dir, run_id=source.run_id, repository_path=str(repo_root))
    locks.acquire()
    try:
        if is_abort_requested(source_dir):
            raise ValidationError("abort requested; refusing pr-review create")

        # Durable successor before Codex publication-text or any Git mutation so
        # failures remain resumable without creating a second successor.
        now = utc_now()
        project = source.project.name
        successor_id = generate_run_id(project, now=now)
        final_dir = run_dir(project, successor_id)
        project_root = runs_dir() / project
        ensure_dir(project_root)
        temp_dir = project_root / f".pr-review-{successor_id}-{secrets.token_hex(4)}"
        _create_successor_layout(temp_dir)
        _copy_identity_artifacts(source_dir, temp_dir)

        baseline_head = discover_repository(repo_root).head
        successor = source.model_copy(deep=True)
        successor.run_id = successor_id
        successor.created_at = now
        successor.updated_at = now
        successor.status = RunStatus.PUBLISHING_EXTERNAL_FIX
        successor.result = "Publishing accepted staged patch for initial PR binding"
        successor.last_error = None
        successor.iterations = []
        successor.recovery = None
        successor.workflow = successor.workflow.model_copy(
            update={
                "max_review_iterations": github.max_local_review_iterations,
                "current_review_iteration": 0,
            }
        )
        successor.github_pr_review = GithubPrReviewState(
            source_run_id=source.run_id,
            lifecycle="publishing_initial",
            cycle_number=1,
            max_external_cycles=github.max_external_cycles,
            pr_number=None,
            head_branch=source.repository.branch,
            base_branch=github.pr_base,
            bound_head_sha=baseline_head,
            staged_patch_sha256=patch_sha,
            publication_phase="pre_commit",
        )
        assert successor.cursor.chat_id == source.cursor.chat_id
        assert successor.codex.session_id == source.codex.session_id
        atomic_write_text(temp_dir / "git/baseline-status.txt", "", sensitive=True)
        save_run_state(temp_dir, successor)
        atomic_write_json(
            temp_dir / "manifest.json",
            {
                "schema_version": 1,
                "run_id": successor_id,
                "project": project,
                "created_at": now.isoformat(),
                "artifacts": [],
            },
            sensitive=True,
        )
        if final_dir.exists():
            raise ValidationError(f"successor run directory already exists: {final_dir}")
        temp_dir.rename(final_dir)
    finally:
        locks.release()

    # Publication text + commit/push/PR use the shared worker path under successor
    # locks (not source locks). Failures become interrupted publication checkpoints.
    try:
        state = load_run_state_fresh(final_dir)
        _publish_external_fix(final_dir, state, config)
        published_state = load_run_state_fresh(final_dir)
        assert published_state.github_pr_review is not None
        assert published_state.github_pr_review.pr_number is not None
        append_orchestrator_event(
            final_dir,
            run_id=successor_id,
            component="orchestrator",
            event="pr_review_cycle_created",
            status=published_state.status.value,
            detail={
                "source_run_id": source.run_id,
                "pr_number": published_state.github_pr_review.pr_number,
                "bound_head_sha_prefix": published_state.github_pr_review.bound_head_sha[:12],
            },
        )
        return PrReviewCreateResult(
            source_run_id=source.run_id,
            run_id=successor_id,
            pr_number=published_state.github_pr_review.pr_number,
            status=published_state.status.value,
            message=(
                f"Created PR-review cycle {successor_id} for PR "
                f"#{published_state.github_pr_review.pr_number}; "
                "detached worker is polling for the Codex-bot review."
            ),
        )
    except Exception as exc:
        _persist_worker_failure(final_dir, exc)
        raise


def _spawn_pr_review_worker(run_directory: Path, run_id: str) -> None:
    launcher_path = run_directory / WORKER_LAUNCHER_REL
    if launcher_path.is_file():
        try:
            existing = json.loads(launcher_path.read_text(encoding="utf-8"))
            pid = int(existing.get("pid") or 0)
            if pid > 0:
                try:
                    os.kill(pid, 0)
                except OSError:
                    pass
                else:
                    return
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            pass
    worker_token = secrets.token_hex(8)
    stdout_path = run_directory / "logs" / "pr-review-worker.stdout.txt"
    stderr_path = run_directory / "logs" / "pr-review-worker.stderr.txt"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_handle = stdout_path.open("a", encoding="utf-8")
    stderr_handle = stderr_path.open("a", encoding="utf-8")
    set_sensitive_file_mode(stdout_path)
    set_sensitive_file_mode(stderr_path)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ai_dev_loop.pr_review_worker",
            run_id,
            worker_token,
        ],
        stdout=stdout_handle,
        stderr=stderr_handle,
        start_new_session=True,
        cwd=str(run_directory),
    )
    stdout_handle.close()
    stderr_handle.close()
    atomic_write_json(
        launcher_path,
        {
            "schema_version": 1,
            "run_id": run_id,
            "worker_token": worker_token,
            "pid": proc.pid,
            "started_at": utc_now().isoformat(),
            "argv_redacted": [
                sys.executable,
                "-m",
                "ai_dev_loop.pr_review_worker",
                run_id,
                "<worker-token>",
            ],
        },
        sensitive=True,
    )


def continue_pr_review_cycle(run_id: str) -> str:
    """Resume after waiting_for_user_attention when GitHub continue command is present."""

    run_directory, state = load_run(run_id)
    gpr = state.github_pr_review
    if gpr is None:
        raise ValidationError("run is not a GitHub PR-review cycle")
    if state.status != RunStatus.WAITING_FOR_USER_ATTENTION:
        raise ValidationError("continue is only valid from waiting_for_user_attention")
    if gpr.pr_number is None:
        raise ValidationError("PR-review cycle is missing a bound PR number")
    config = _require_github_config(Path(state.repository.root))
    assert config.github is not None
    matched = _find_continue_comment(
        config.github.command,
        cwd=state.repository.root,
        pr_number=gpr.pr_number,
        continue_command=config.github.continue_command,
        authorized_login=config.github.user_mention,
        after_comment_id=gpr.continue_comment_id or gpr.request_comment_id,
    )
    if matched is None:
        raise ValidationError(
            "no authorizing GitHub comment from the configured user with the exact "
            "continue command was found"
        )
    locks = _run_locks(run_directory, run_id=state.run_id, repository_path=state.repository.root)
    locks.acquire()
    try:
        state = load_run_state_fresh(run_directory)
        assert state.github_pr_review is not None
        state.github_pr_review = state.github_pr_review.model_copy(
            update={
                "continue_comment_id": matched,
                "lifecycle": "awaiting_bot_review",
            }
        )
        _mark_status(state, RunStatus.AWAITING_BOT_REVIEW)
        state.result = "Continue authorized; awaiting next Codex-bot review window"
        state.last_error = None
        save_run_state(run_directory, state)
        _spawn_pr_review_worker(run_directory, state.run_id)
        return f"Continue accepted for {state.run_id}; worker resumed."
    finally:
        locks.release()


def load_run_state_fresh(run_directory: Path) -> RunState:
    from ai_dev_loop.state import load_run_state

    return load_run_state(run_directory / "state.json")


def _sanitize_worker_error(exc: BaseException) -> str:
    message = redact_text(str(exc)).strip() or "PR-review worker failed"
    # Keep last_error concise and free of raw comment/prompt bodies.
    first_line = message.splitlines()[0][:300]
    return first_line


def _classify_worker_outcome(
    exc: BaseException,
    *,
    gpr: GithubPrReviewState | None = None,
) -> tuple[str, str]:
    """Return (status_target, worker_outcome) for a worker failure.

    ``status_target`` is ``interrupted`` or ``failed``. Publication lifecycles are
    preserved separately so ``pr-review resume`` can restart from checkpoints.
    """

    text = str(exc).lower()
    if isinstance(exc, ValidationError):
        return "failed", "validation_error"
    if "rate limit" in text:
        return "interrupted", GithubErrorKind.RATE_LIMIT.value
    if "auth" in text or "not authenticated" in text or "permission" in text:
        return "interrupted", GithubErrorKind.AUTH.value
    if "timed out" in text or "timeout" in text:
        return "interrupted", GithubErrorKind.TIMEOUT.value
    if "head changed" in text or "head drift" in text:
        return "interrupted", "head_drift"
    if "git push failed" in text or "push failed" in text:
        return "interrupted", "push_failed"
    if "pr create" in text or "github pr" in text:
        return "interrupted", "pr_operation_failed"
    if "review trigger" in text or "failed to post" in text:
        return "interrupted", "trigger_write_failed"
    if "publication-text" in text or "publication text" in text:
        return "interrupted", "publication_text_failed"
    if "ls-remote" in text or "network" in text or "connection" in text:
        return "interrupted", "network_error"
    if _publication_in_progress(gpr) and isinstance(exc, AiDevLoopError):
        return "interrupted", "publication_error"
    return "failed", "worker_error"


def _apply_worker_failure(run_directory: Path, exc: BaseException) -> None:
    """Persist durable interrupted/failed status. Caller must hold run locks.

    Recoverable publication failures keep ``publishing_*`` lifecycle and phase
    checkpoints so the public ``pr-review resume`` path can continue.
    """

    safe = _sanitize_worker_error(exc)
    state = load_run_state_fresh(run_directory)
    if state.status in TERMINAL_STATUSES:
        return
    gpr = state.github_pr_review
    status_target, outcome = _classify_worker_outcome(exc, gpr=gpr)
    if status_target == "interrupted":
        mark_interrupted(state, safe)
    else:
        mark_failed(state, safe)
    if state.github_pr_review is not None:
        gpr = state.github_pr_review
        updates: dict[str, Any] = {"worker_outcome": outcome}
        if status_target == "interrupted" and _publication_in_progress(gpr):
            if gpr.lifecycle not in _PUBLICATION_LIFECYCLES:
                updates["lifecycle"] = (
                    "publishing_initial" if gpr.pr_number is None else "publishing_external_fix"
                )
        else:
            updates["lifecycle"] = status_target
        state.github_pr_review = gpr.model_copy(update=updates)
    save_run_state(run_directory, state)
    append_orchestrator_event(
        run_directory,
        run_id=state.run_id,
        component="orchestrator",
        event="pr_review_worker_failed",
        status=state.status.value,
        detail={"worker_outcome": outcome},
    )


def _persist_worker_failure(run_directory: Path, exc: BaseException) -> None:
    state = load_run_state_fresh(run_directory)
    locks = _run_locks(
        run_directory,
        run_id=state.run_id,
        repository_path=state.repository.root,
    )
    locks.acquire()
    try:
        _apply_worker_failure(run_directory, exc)
    finally:
        locks.release()


def _find_continue_comment(
    command: str,
    *,
    cwd: str,
    pr_number: int,
    continue_command: str,
    authorized_login: str,
    after_comment_id: str | None,
) -> str | None:
    """Return comment id when author and exact continue command both match config."""

    from ai_dev_loop.runners.github import _parse_json, run_gh

    result = run_gh(
        command,
        [
            "api",
            f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
            "--paginate",
        ],
        cwd=cwd,
        timeout=60.0,
    )
    items = _parse_json(result)
    if not isinstance(items, list):
        text = result.stdout.strip()
        items = []
        for line in text.splitlines():
            if line.strip():
                items.append(json.loads(line))
    after_int = (
        int(after_comment_id) if after_comment_id and str(after_comment_id).isdigit() else None
    )
    for item in items:
        cid = str(item.get("id"))
        if after_int is not None and cid.isdigit() and int(cid) <= after_int:
            continue
        user = item.get("user") or {}
        author = str(user.get("login") or "") if isinstance(user, dict) else str(user or "")
        body = str(item.get("body") or "")
        if continue_comment_authorized(
            body=body,
            author_login=author,
            continue_command=continue_command,
            authorized_login=authorized_login,
        ):
            return cid
    return None


def resume_pr_review_cycle(run_id: str) -> str:
    """Explicit controller resume for an interrupted PR-review cycle."""

    run_directory, state = load_run(run_id)
    if state.github_pr_review is None:
        raise ValidationError("run is not a GitHub PR-review cycle")
    if state.status != RunStatus.INTERRUPTED:
        raise ValidationError("pr-review resume requires status interrupted")
    resume_workflow_run_id: str | None = None
    locks = _run_locks(run_directory, run_id=state.run_id, repository_path=state.repository.root)
    locks.acquire()
    try:
        state = load_run_state_fresh(run_directory)
        assert state.github_pr_review is not None
        gpr = state.github_pr_review
        lifecycle = gpr.lifecycle
        publication_lifecycle = _resolve_publication_resume_lifecycle(gpr)
        if publication_lifecycle is not None:
            _mark_status(state, RunStatus.PUBLISHING_EXTERNAL_FIX)
            state.github_pr_review = gpr.model_copy(
                update={
                    "lifecycle": publication_lifecycle,
                    "worker_outcome": None,
                }
            )
            state.last_error = None
            save_run_state(run_directory, state)
            _spawn_pr_review_worker(run_directory, state.run_id)
            return f"Resumed publication for {state.run_id}"
        if lifecycle in {"awaiting_bot_review", "interrupted"}:
            _mark_status(state, RunStatus.AWAITING_BOT_REVIEW)
            state.github_pr_review = state.github_pr_review.model_copy(
                update={"lifecycle": "awaiting_bot_review", "worker_outcome": None}
            )
            state.last_error = None
            save_run_state(run_directory, state)
            _spawn_pr_review_worker(run_directory, state.run_id)
            return f"Resumed PR-review polling for {state.run_id}"
        if lifecycle == "fixing_external_feedback":
            _mark_status(state, RunStatus.RUNNING_CURSOR)
            state.github_pr_review = state.github_pr_review.model_copy(
                update={"lifecycle": "fixing_external_feedback"}
            )
            save_run_state(run_directory, state)
            resume_workflow_run_id = state.run_id
        else:
            raise ValidationError(f"cannot resume lifecycle {lifecycle}")
    finally:
        locks.release()

    if resume_workflow_run_id is not None:
        from ai_dev_loop.workflow_engine import resume_run

        resume_run(resume_workflow_run_id)
        return f"Resumed local fix loop for {resume_workflow_run_id}"
    raise ValidationError("pr-review resume produced no next action")


def abort_pr_review_cycle(run_id: str) -> str:
    run_directory, state = load_run(run_id)
    if state.github_pr_review is None:
        raise ValidationError("run is not a GitHub PR-review cycle")
    write_abort_request(run_directory, run_id=run_id)
    if state.status not in TERMINAL_STATUSES:
        locks = _run_locks(
            run_directory, run_id=state.run_id, repository_path=state.repository.root
        )
        try:
            locks.acquire()
            state = load_run_state_fresh(run_directory)
            if state.status not in TERMINAL_STATUSES:
                mark_aborted(state, "PR-review cycle aborted by user request")
                if state.github_pr_review is not None:
                    state.github_pr_review = state.github_pr_review.model_copy(
                        update={"lifecycle": "aborted"}
                    )
                save_run_state(run_directory, state)
        except Exception:
            # Leave abort request for the worker if locks are held.
            pass
        finally:
            with contextlib.suppress(Exception):
                locks.release()
    return f"Abort requested for PR-review cycle {run_id}"


def render_pr_review_status(run_id: str, *, output: str = "text") -> str:
    run_directory, state = load_run(run_id)
    gpr = state.github_pr_review
    payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": state.run_id,
        "status": state.status.value,
        "result": state.result,
        "last_error": state.last_error,
        "github_pr_review": None,
    }
    if gpr is not None:
        payload["github_pr_review"] = {
            "source_run_id": gpr.source_run_id,
            "lifecycle": gpr.lifecycle,
            "cycle_number": gpr.cycle_number,
            "max_external_cycles": gpr.max_external_cycles,
            "pr_number": gpr.pr_number,
            "bound_head_sha_prefix": gpr.bound_head_sha[:12],
            "eligible_thread_count": len(gpr.eligible_thread_ids),
            "processed_thread_count": len(gpr.processed_thread_ids),
            "replied_thread_count": len(gpr.replied_thread_ids),
            "resolved_thread_count": len(gpr.resolved_thread_ids),
            "request_comment_id": gpr.request_comment_id,
            "last_external_result_path": gpr.last_external_result_path,
            "last_snapshot_path": gpr.last_snapshot_path,
        }
    if output == "json":
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Run {state.run_id}",
        f"Status: {state.status.value}",
    ]
    if gpr is not None:
        lines.extend(
            [
                f"Lifecycle: {gpr.lifecycle}",
                f"PR: #{gpr.pr_number}",
                f"Cycle: {gpr.cycle_number}/{gpr.max_external_cycles}",
                f"Bound head: {gpr.bound_head_sha[:12]}…",
                f"Eligible threads: {len(gpr.eligible_thread_ids)}",
                f"Replied threads: {len(gpr.replied_thread_ids)}",
                f"Resolved threads: {len(gpr.resolved_thread_ids)}",
            ]
        )
    if state.result:
        lines.append(state.result)
    if state.last_error:
        lines.append(f"Last error: {state.last_error}")
    return "\n".join(lines) + "\n"


def github_doctor(*, repo_path: Path | None = None, output: str = "text") -> str:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    import shutil

    from ai_dev_loop.paths import schema_path

    gh_path = shutil.which("gh")
    add("gh_cli", gh_path is not None, gh_path or "not found")
    for schema_name in (
        "github-pr-review-result-v1.json",
        "github-publication-text-v1.json",
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


def run_pr_review_worker_loop(run_id: str) -> None:
    """Polling / adjudication / publication worker body."""

    run_directory, state = load_run(run_id)
    if state.github_pr_review is None:
        raise ValidationError("not a PR-review cycle")
    try:
        _run_pr_review_worker_loop_inner(run_id, run_directory)
    except Exception as exc:
        _persist_worker_failure(run_directory, exc)
        raise


def _run_pr_review_worker_loop_inner(run_id: str, run_directory: Path) -> None:
    state = load_run_state_fresh(run_directory)
    if state.github_pr_review is None:
        raise ValidationError("not a PR-review cycle")
    config = _require_github_config(Path(state.repository.root))
    assert config.github is not None
    github = config.github
    deadline = time.monotonic() + github.poll_timeout_hours * 3600

    while time.monotonic() < deadline:
        if is_abort_requested(run_directory):
            locks = _run_locks(run_directory, run_id=run_id, repository_path=state.repository.root)
            locks.acquire()
            try:
                state = load_run_state_fresh(run_directory)
                mark_aborted(state, "PR-review worker observed abort request")
                if state.github_pr_review:
                    state.github_pr_review = state.github_pr_review.model_copy(
                        update={"lifecycle": "aborted"}
                    )
                save_run_state(run_directory, state)
            finally:
                locks.release()
            return

        state = load_run_state_fresh(run_directory)
        gpr = state.github_pr_review
        if gpr is None:
            return
        if state.status in TERMINAL_STATUSES:
            return
        if state.status == RunStatus.WAITING_FOR_USER_ATTENTION:
            return
        if state.status == RunStatus.PUBLISHING_EXTERNAL_FIX or gpr.lifecycle in {
            "publishing_external_fix",
            "publishing_initial",
        }:
            _publish_external_fix(run_directory, state, config)
            return
        if state.status != RunStatus.AWAITING_BOT_REVIEW:
            return
        if gpr.pr_number is None:
            raise ValidationError("bound PR number is required while awaiting bot review")

        try:
            pr = get_pull_request(
                github.command, cwd=state.repository.root, pr_number=gpr.pr_number
            )
        except AiDevLoopError as exc:
            raise AiDevLoopError(_sanitize_worker_error(exc)) from exc

        if pr.state != "OPEN":
            raise ValidationError("bound pull request is no longer open")
        if pr.head_sha != gpr.bound_head_sha:
            raise ValidationError("PR head changed during await; resolve drift before continuing")

        threads = list_review_threads(
            github.command, cwd=state.repository.root, pr_number=gpr.pr_number
        )
        eligible = filter_eligible_threads(
            threads,
            reviewer_logins=github.reviewer_logins,
            bound_head_sha=gpr.bound_head_sha,
            already_processed_thread_ids=set(gpr.processed_thread_ids),
            request_created_at=gpr.request_created_at,
        )
        if not eligible:
            time.sleep(github.poll_interval_seconds)
            continue

        locks = _run_locks(run_directory, run_id=run_id, repository_path=state.repository.root)
        locks.acquire()
        try:
            state = load_run_state_fresh(run_directory)
            assert state.github_pr_review is not None
            _mark_status(state, RunStatus.EVALUATING_BOT_FEEDBACK)
            state.github_pr_review = state.github_pr_review.model_copy(
                update={
                    "lifecycle": "evaluating_bot_feedback",
                    "eligible_thread_ids": [t.thread_id for t in eligible],
                }
            )
            save_run_state(run_directory, state)
        finally:
            locks.release()

        thread_payload = _load_thread_bodies(
            github.command,
            cwd=state.repository.root,
            pr_number=gpr.pr_number,
            thread_ids={t.thread_id for t in eligible},
        )
        review, artifacts = run_codex_github_review(
            state,
            run_directory,
            cycle_number=gpr.cycle_number,
            external_review_skill=github.external_review_skill,
            eligible_thread_payload=thread_payload,
            bound_head_sha=gpr.bound_head_sha,
            pr_number=gpr.pr_number,
        )

        resume_workflow = False
        locks.acquire()
        try:
            state = load_run_state_fresh(run_directory)
            assert state.github_pr_review is not None
            state.github_pr_review = state.github_pr_review.model_copy(
                update={
                    "last_external_result_path": artifacts.result_path,
                    "last_snapshot_path": artifacts.snapshot_path,
                }
            )
            if not review.all_actionable:
                for decision in review.thread_decisions:
                    if decision.decision == "actionable":
                        continue
                    assert decision.inline_reply is not None
                    if decision.thread_id in state.github_pr_review.replied_thread_ids:
                        continue
                    reply = reply_to_review_thread(
                        github.command,
                        cwd=state.repository.root,
                        pull_request_review_thread_id=decision.thread_id,
                        body=decision.inline_reply,
                    )
                    if not reply.ok:
                        raise AiDevLoopError(
                            reply.error.message if reply.error else "inline reply failed"
                        )
                    replied = list(state.github_pr_review.replied_thread_ids)
                    replied.append(decision.thread_id)
                    state.github_pr_review = state.github_pr_review.model_copy(
                        update={"replied_thread_ids": replied}
                    )
                _mark_status(state, RunStatus.WAITING_FOR_USER_ATTENTION)
                state.github_pr_review = state.github_pr_review.model_copy(
                    update={"lifecycle": "waiting_for_user_attention"}
                )
                state.result = (
                    "Non-actionable or uncertain bot finding(s) replied inline; "
                    f"waiting for exact continue command from {github.user_mention}"
                )
                save_run_state(run_directory, state)
                append_orchestrator_event(
                    run_directory,
                    run_id=state.run_id,
                    component="orchestrator",
                    event="pr_review_user_attention",
                    status=state.status.value,
                    detail={"replied_thread_count": len(state.github_pr_review.replied_thread_ids)},
                )
                return

            assert artifacts.fix_prompt_path is not None
            assert state.cursor.chat_id
            state.github_pr_review = state.github_pr_review.model_copy(
                update={
                    "lifecycle": "fixing_external_feedback",
                    "external_fix_prompt_path": artifacts.fix_prompt_path,
                }
            )
            state.workflow = state.workflow.model_copy(update={"current_review_iteration": 0})
            begin_running_cursor(state)
            save_run_state(run_directory, state)
            resume_workflow = True
        finally:
            locks.release()

        if resume_workflow:
            from ai_dev_loop.workflow_engine import resume_run

            resume_run(run_id)
        return

    locks = _run_locks(run_directory, run_id=run_id, repository_path=state.repository.root)
    locks.acquire()
    try:
        state = load_run_state_fresh(run_directory)
        mark_interrupted(state, "PR-review polling timed out waiting for bot review")
        if state.github_pr_review:
            state.github_pr_review = state.github_pr_review.model_copy(
                update={"lifecycle": "interrupted", "worker_outcome": "timeout"}
            )
        save_run_state(run_directory, state)
    finally:
        locks.release()


def _fail_cycle(run_directory: Path, state: RunState, message: str) -> None:
    locks = _run_locks(run_directory, run_id=state.run_id, repository_path=state.repository.root)
    locks.acquire()
    try:
        state = load_run_state_fresh(run_directory)
        mark_failed(state, message)
        if state.github_pr_review:
            state.github_pr_review = state.github_pr_review.model_copy(
                update={"lifecycle": "failed"}
            )
        save_run_state(run_directory, state)
    finally:
        locks.release()


def _load_thread_bodies(
    command: str,
    *,
    cwd: str,
    pr_number: int,
    thread_ids: set[str],
) -> list[dict[str, Any]]:
    """Reload eligible threads including bodies for Codex stdin (sensitive)."""

    from ai_dev_loop.runners.github import resolve_repository_nwo, run_process_streaming_json

    query = """
    query($owner:String!,$name:String!,$number:Int!,$cursor:String){
      repository(owner:$owner,name:$name){
        pullRequest(number:$number){
          reviewThreads(first:50, after:$cursor){
            pageInfo{hasNextPage endCursor}
            nodes{
              id
              isResolved
              comments(first:1){
                nodes{
                  id
                  body
                  createdAt
                  author{login}
                  commit{oid}
                  path
                  line
                }
              }
            }
          }
        }
      }
    }
    """
    nwo = resolve_repository_nwo(command, cwd=cwd)
    owner, name = nwo.split("/", 1)
    payload: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        args = [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr_number}",
        ]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        else:
            args.extend(["-F", "cursor=null"])
        parsed = run_process_streaming_json(command, args, cwd=cwd, timeout=120.0)
        if not isinstance(parsed, dict):
            raise AiDevLoopError(parsed.message)
        connection = parsed["data"]["repository"]["pullRequest"]["reviewThreads"]
        for node in connection.get("nodes") or []:
            if node["id"] not in thread_ids:
                continue
            root = (node.get("comments") or {}).get("nodes") or []
            if not root:
                continue
            comment = root[0]
            payload.append(
                {
                    "thread_id": node["id"],
                    "author_login": (comment.get("author") or {}).get("login"),
                    "path": comment.get("path"),
                    "line": comment.get("line"),
                    "commit_sha": (comment.get("commit") or {}).get("oid"),
                    "body": comment.get("body") or "",
                    "created_at": comment.get("createdAt"),
                    "root_comment_id": comment.get("id"),
                }
            )
        page = connection["pageInfo"]
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            break
    if {item["thread_id"] for item in payload} != thread_ids:
        raise ValidationError("failed to load bodies for all eligible threads")
    return payload


def _load_publication_text(run_directory: Path, relative_path: str) -> PublicationText:
    path = run_directory / relative_path
    if not path.is_file():
        raise ValidationError(f"publication text artifact missing: {relative_path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return PublicationText(
        commit_subject=str(payload["commit_subject"]),
        commit_body=str(payload.get("commit_body") or ""),
        pr_title=str(payload["pr_title"]),
        pr_body=str(payload.get("pr_body") or ""),
    )


def _update_publication_checkpoint(
    run_directory: Path,
    **updates: Any,
) -> RunState:
    state = load_run_state_fresh(run_directory)
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(update=updates)
    save_run_state(run_directory, state)
    return state


def _ensure_review_trigger_comment(
    run_directory: Path,
    *,
    command: str,
    cwd: str,
    pr_number: int,
    review_trigger_body: str,
    run_id: str,
    cycle_number: int,
    commit_sha: str,
) -> tuple[str, str]:
    """Post or bind the review-trigger comment crash-idempotently.

    Persists the request marker as write intent before creating a comment, then
    searches for an existing marker-bearing comment before posting a new one.
    """

    marker = _request_marker(run_id=run_id, cycle_number=cycle_number, commit_sha=commit_sha)
    needle = f"<!-- {marker} -->"
    state = load_run_state_fresh(run_directory)
    assert state.github_pr_review is not None
    gpr = state.github_pr_review
    intent_updates: dict[str, Any] = {"request_marker": marker}
    if gpr.request_marker != marker:
        intent_updates["request_comment_id"] = None
        intent_updates["request_created_at"] = None
    state = _update_publication_checkpoint(run_directory, **intent_updates)
    assert state.github_pr_review is not None
    gpr = state.github_pr_review

    if gpr.request_comment_id and gpr.request_marker == marker:
        return gpr.request_comment_id, gpr.request_created_at or utc_now().isoformat()

    found = find_issue_comment_with_marker(
        command,
        cwd=cwd,
        pr_number=pr_number,
        marker=marker,
    )
    if found is not None:
        comment_id, created_at = found
        bound_at = created_at or utc_now().isoformat()
        _update_publication_checkpoint(
            run_directory,
            request_marker=marker,
            request_comment_id=comment_id,
            request_created_at=bound_at,
        )
        return comment_id, bound_at

    trigger_body = f"{review_trigger_body}\n\n{needle}"
    write = create_issue_comment(
        command,
        cwd=cwd,
        pr_number=pr_number,
        body=trigger_body,
    )
    if not write.ok or not write.resource_id:
        raise AiDevLoopError(
            write.error.message if write.error else "failed to post review trigger comment"
        )
    # Crash window: comment may exist on GitHub before local bind. Retry searches first.
    created_at = utc_now().isoformat()
    _update_publication_checkpoint(
        run_directory,
        request_marker=marker,
        request_comment_id=write.resource_id,
        request_created_at=created_at,
    )
    return write.resource_id, created_at


def _run_publication_pipeline(
    run_directory: Path,
    *,
    config: ProjectConfig,
    text: PublicationText,
    branch: str,
    create_pr: bool,
) -> RunState:
    """Crash-safe commit/push/PR/trigger pipeline with durable phase checkpoints."""

    assert config.github is not None
    github = config.github
    state = load_run_state_fresh(run_directory)
    assert state.github_pr_review is not None
    gpr = state.github_pr_review
    repo_root = Path(state.repository.root)
    phase = gpr.publication_phase

    def after_commit(
        commit_sha: str,
        expected_remote: str | None,
        remote: str,
        remote_branch: str,
    ) -> None:
        _update_publication_checkpoint(
            run_directory,
            publication_phase="committed",
            local_commit_sha=commit_sha,
            expected_remote_sha_before_push=expected_remote,
            publication_remote=remote,
            publication_remote_branch=remote_branch,
        )

    if phase in {None, "pre_commit", "committed"}:
        resume_sha = gpr.local_commit_sha if phase == "committed" else None
        published = publish_accepted_staged_patch(
            repo_root,
            branch=branch,
            text=text,
            resume_from_commit=resume_sha,
            expected_remote_sha_before_push=gpr.expected_remote_sha_before_push
            if resume_sha
            else None,
            staged_patch_sha256=gpr.staged_patch_sha256 if resume_sha else None,
            after_commit=None if resume_sha else after_commit,
        )
        if gpr.staged_patch_sha256 and published.staged_patch_sha256 != gpr.staged_patch_sha256:
            raise ValidationError("staged patch changed during publication")
        state = _update_publication_checkpoint(
            run_directory,
            publication_phase="pushed",
            local_commit_sha=published.commit_sha,
            publication_commit_sha=published.commit_sha,
            staged_patch_sha256=published.staged_patch_sha256,
            expected_remote_sha_before_push=published.expected_remote_sha_before_push,
            publication_remote=published.remote_name,
            publication_remote_branch=published.remote_ref.split("/", 1)[-1],
        )
        updated_gpr = state.github_pr_review
        if updated_gpr is None:
            raise ValidationError("github_pr_review missing after push checkpoint")
        gpr = updated_gpr
        phase = "pushed"

    commit_sha = gpr.local_commit_sha or gpr.publication_commit_sha
    if not commit_sha:
        raise ValidationError("publication checkpoint is missing local commit SHA")

    if create_pr and phase in {"pushed", "pr_bound"}:
        if phase == "pushed" or gpr.pr_number is None:
            pr = create_or_update_pull_request(
                github.command,
                cwd=str(repo_root),
                head_branch=branch,
                base=github.pr_base,
                title=text.pr_title,
                body=text.pr_body,
            )
            if pr.is_cross_repository:
                raise ValidationError("cross-repository PRs are not supported")
            if pr.base_ref != github.pr_base:
                raise ValidationError(f"PR base must be {github.pr_base}")
            if pr.head_ref != branch:
                raise ValidationError("PR head branch does not match prepared branch")
            if pr.head_sha != commit_sha:
                raise ValidationError("PR head SHA does not match published commit")
            if pr.state != "OPEN":
                raise ValidationError("bound PR is not open")
            state = _update_publication_checkpoint(
                run_directory,
                publication_phase="pr_bound",
                pr_number=pr.number,
                pr_url=pr.url or None,
                bound_head_sha=commit_sha,
            )
            updated_gpr = state.github_pr_review
            if updated_gpr is None:
                raise ValidationError("github_pr_review missing after PR bind checkpoint")
            gpr = updated_gpr
        else:
            if gpr.pr_number is None:
                raise ValidationError("PR number is required when resuming a bound publication")
            pr = get_pull_request(github.command, cwd=str(repo_root), pr_number=gpr.pr_number)
            if pr.head_sha != commit_sha:
                raise ValidationError("PR head SHA does not match published commit")

        assert gpr.pr_number is not None
        request_marker = _request_marker(
            run_id=state.run_id,
            cycle_number=gpr.cycle_number,
            commit_sha=commit_sha,
        )
        request_comment_id, request_created_at = _ensure_review_trigger_comment(
            run_directory,
            command=github.command,
            cwd=str(repo_root),
            pr_number=gpr.pr_number,
            review_trigger_body=github.review_trigger_body,
            run_id=state.run_id,
            cycle_number=gpr.cycle_number,
            commit_sha=commit_sha,
        )

        repo_after = discover_repository(repo_root)
        state = load_run_state_fresh(run_directory)
        assert state.github_pr_review is not None
        state.repository = state.repository.model_copy(
            update={
                "initial_head": commit_sha,
                "branch": branch,
                "baseline_status_path": "git/baseline-status.txt",
            }
        )
        atomic_write_text(
            run_directory / "git/baseline-status.txt",
            repo_after.status_porcelain,
            sensitive=True,
        )
        state.github_pr_review = state.github_pr_review.model_copy(
            update={
                "lifecycle": "awaiting_bot_review",
                "publication_phase": None,
                "bound_head_sha": commit_sha,
                "publication_commit_sha": commit_sha,
                "request_comment_id": request_comment_id,
                "request_marker": request_marker,
                "request_created_at": request_created_at,
            }
        )
        if state.status != RunStatus.AWAITING_BOT_REVIEW:
            _mark_status(state, RunStatus.AWAITING_BOT_REVIEW)
        state.result = (
            f"PR #{state.github_pr_review.pr_number} bound at {commit_sha[:12]}; "
            "awaiting Codex-bot review"
        )
        state.last_error = None
        save_run_state(run_directory, state)
        return state

    # External-fix path: PR already exists; verify head, resolve threads, next trigger.
    assert gpr.pr_number is not None
    pr = get_pull_request(github.command, cwd=str(repo_root), pr_number=gpr.pr_number)
    if pr.head_sha != commit_sha:
        raise ValidationError("PR head did not advance to the published commit")

    state = load_run_state_fresh(run_directory)
    assert state.github_pr_review is not None
    for thread_id in list(state.github_pr_review.eligible_thread_ids):
        if thread_id in state.github_pr_review.resolved_thread_ids:
            continue
        if thread_id in state.github_pr_review.replied_thread_ids:
            continue
        result = resolve_review_thread(
            github.command,
            cwd=str(repo_root),
            pull_request_review_thread_id=thread_id,
        )
        if not result.ok:
            raise AiDevLoopError(result.error.message if result.error else "resolve thread failed")
        resolved = list(state.github_pr_review.resolved_thread_ids)
        resolved.append(thread_id)
        state.github_pr_review = state.github_pr_review.model_copy(
            update={"resolved_thread_ids": resolved}
        )
        save_run_state(run_directory, state)

    next_cycle = state.github_pr_review.cycle_number + 1
    if next_cycle > state.github_pr_review.max_external_cycles:
        state.github_pr_review = state.github_pr_review.model_copy(
            update={
                "lifecycle": "max_external_cycles_reached",
                "publication_phase": None,
                "bound_head_sha": commit_sha,
                "publication_commit_sha": commit_sha,
                "processed_thread_ids": list(
                    dict.fromkeys(
                        list(state.github_pr_review.processed_thread_ids)
                        + list(state.github_pr_review.eligible_thread_ids)
                    )
                ),
            }
        )
        mark_completed(
            state,
            f"Maximum external review cycles ({state.github_pr_review.max_external_cycles}) reached",
        )
        save_run_state(run_directory, state)
        return state

    pr_number = state.github_pr_review.pr_number
    if pr_number is None:
        raise ValidationError("PR number is required before posting the next review trigger")
    request_marker = _request_marker(
        run_id=state.run_id,
        cycle_number=next_cycle,
        commit_sha=commit_sha,
    )
    request_comment_id, request_created_at = _ensure_review_trigger_comment(
        run_directory,
        command=github.command,
        cwd=str(repo_root),
        pr_number=pr_number,
        review_trigger_body=github.review_trigger_body,
        run_id=state.run_id,
        cycle_number=next_cycle,
        commit_sha=commit_sha,
    )
    state = load_run_state_fresh(run_directory)
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={
            "lifecycle": "awaiting_bot_review",
            "publication_phase": None,
            "cycle_number": next_cycle,
            "bound_head_sha": commit_sha,
            "publication_commit_sha": commit_sha,
            "request_comment_id": request_comment_id,
            "request_marker": request_marker,
            "request_created_at": request_created_at,
            "eligible_thread_ids": [],
            "processed_thread_ids": list(
                dict.fromkeys(
                    list(state.github_pr_review.processed_thread_ids)
                    + list(state.github_pr_review.eligible_thread_ids)
                )
            ),
        }
    )
    state.repository = state.repository.model_copy(update={"initial_head": commit_sha})
    if state.status != RunStatus.AWAITING_BOT_REVIEW:
        _mark_status(state, RunStatus.AWAITING_BOT_REVIEW)
    state.result = f"Published fix; awaiting cycle {next_cycle} Codex-bot review"
    state.last_error = None
    save_run_state(run_directory, state)
    return state


def _publish_external_fix(
    run_directory: Path,
    state: RunState,
    config: ProjectConfig,
) -> None:
    assert config.github is not None
    assert state.github_pr_review is not None
    gpr = state.github_pr_review
    repo_root = Path(state.repository.root)
    locks = _run_locks(run_directory, run_id=state.run_id, repository_path=str(repo_root))
    locks.acquire()
    try:
        state = load_run_state_fresh(run_directory)
        assert state.github_pr_review is not None
        gpr = state.github_pr_review
        create_pr = gpr.lifecycle == "publishing_initial" or gpr.pr_number is None
        text_rel = (
            gpr.publication_text_path
            or f"github/cycles/{gpr.cycle_number:02d}/publication-text.json"
        )
        text_path = run_directory / text_rel
        if text_path.is_file():
            # Adopt a durable publication-text artifact from a prior crash window.
            text = _load_publication_text(run_directory, text_rel)
            if gpr.publication_text_path != text_rel:
                state = _update_publication_checkpoint(
                    run_directory,
                    publication_phase=gpr.publication_phase or "pre_commit",
                    publication_text_path=text_rel,
                    lifecycle=gpr.lifecycle
                    if gpr.lifecycle in _PUBLICATION_LIFECYCLES
                    else (
                        "publishing_external_fix"
                        if gpr.pr_number is not None
                        else "publishing_initial"
                    ),
                )
        else:
            name_only = require_success(
                run_process(["git", "diff", "--cached", "--name-only"], cwd=str(repo_root)),
                context="staged name-only",
            )
            stat = require_success(
                run_process(["git", "diff", "--cached", "--stat"], cwd=str(repo_root)),
                context="staged stat",
            )
            residual = _residual_risk_note_for_publication(gpr)
            text = run_codex_publication_text(
                state,
                run_directory,
                cycle_number=gpr.cycle_number,
                staged_name_only=name_only,
                staged_stat=stat,
                residual_risk_note=residual,
            )
            # Codex may have already written result_rel; persist a canonical copy/bind.
            if not text_path.is_file():
                atomic_write_json(
                    text_path,
                    {
                        "commit_subject": text.commit_subject,
                        "commit_body": text.commit_body,
                        "pr_title": text.pr_title,
                        "pr_body": text.pr_body,
                    },
                    sensitive=True,
                )
            state = _update_publication_checkpoint(
                run_directory,
                publication_phase=gpr.publication_phase or "pre_commit",
                publication_text_path=text_rel,
                lifecycle="publishing_external_fix"
                if gpr.lifecycle != "publishing_initial"
                else gpr.lifecycle,
            )
        state = _run_publication_pipeline(
            run_directory,
            config=config,
            text=text,
            branch=gpr.head_branch,
            create_pr=create_pr,
        )
    finally:
        locks.release()

    if state.status == RunStatus.AWAITING_BOT_REVIEW:
        _spawn_pr_review_worker(run_directory, state.run_id)


def render_create_result(result: PrReviewCreateResult, *, output: str = "text") -> str:
    if output == "json":
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "source_run_id": result.source_run_id,
                    "run_id": result.run_id,
                    "pr_number": result.pr_number,
                    "status": result.status,
                    "message": result.message,
                },
                indent=2,
            )
            + "\n"
        )
    return (
        f"{result.message}\n"
        f"Source: {result.source_run_id}\n"
        f"Cycle run: {result.run_id}\n"
        f"PR: #{result.pr_number}\n"
        f"Status: {result.status}\n"
    )
