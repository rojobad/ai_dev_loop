"""CLI command implementations for temporary ``pr-review-v2`` namespace."""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.config import ProjectConfig, PrReviewV2Section, resolve_effective_config
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex.session_runtime import (
    read_codex_session_runtime,
    require_codex_session_id,
)
from ai_dev_loop.pr_review_v2.application.control import ControlPlaneService
from ai_dev_loop.pr_review_v2.application.control_contracts import (
    ControlError,
    OriginKind,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextArtifact,
    ExecutionContextCodex,
    ExecutionContextCursor,
    ExecutionContextPlanPrompt,
    ExecutionContextPrReviewV2,
    ExecutionContextRunBinding,
    ExecutionContextWorker,
    ExecutionContextWorkflow,
)
from ai_dev_loop.pr_review_v2.application.preparation import (
    PreparationService,
    SourceRunSnapshot,
)
from ai_dev_loop.pr_review_v2.infrastructure.existing_pr_discovery import ExistingPrDiscoverer
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    default_engine_db_path,
    pr_review_v2_state_dir,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.workers.supervisor import SupervisorLauncherStore
from ai_dev_loop.review_runtime import resolve_effective_review_runtime
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import RunState, sha256_file


def _owner_repo_from_remote(url: str) -> str:
    """Resolve owner/repo only from strict SSH remotes (reject HTTPS/malformed)."""

    from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import extract_remote_nwo

    nwo = extract_remote_nwo(url)
    if nwo is None:
        raise ValidationError(
            "publication remote must be an SSH GitHub remote "
            "(https and malformed remotes are rejected)"
        )
    return nwo


def _artifact_root() -> Path:
    return pr_review_v2_state_dir() / "artifacts"


def _open_engine(db_path: Path | None = None) -> PrReviewEngine:
    return PrReviewEngine.open(db_path or default_engine_db_path())


def _require_v2_config(config: ProjectConfig) -> PrReviewV2Section:
    if config.pr_review_v2 is None or not config.pr_review_v2.enabled:
        raise ValidationError(
            "pr_review_v2 is disabled or absent; set pr_review_v2.enabled: true "
            "(temporary pre-cutover namespace; legacy pr-review is unchanged)"
        )
    return config.pr_review_v2


def _execution_context_from_source(
    source: RunState,
    *,
    v2: PrReviewV2Section,
    repository: str,
    head_branch: str,
    base_branch: str,
    expected_head_sha: str,
    plan_path: str,
    prompt_path: str,
    plan_sha256: str,
    prompt_sha256: str,
    accepted_patch_sha256: str,
    repository_root: str,
) -> ExecutionContextArtifact:
    if not source.cursor.chat_id:
        raise ValidationError("source run is missing cursor.chat_id")
    if not source.codex.session_id:
        raise ValidationError("source run is missing codex.session_id")
    review_model = source.codex.review_model or source.codex.session_model
    review_effort = (
        source.codex.review_reasoning_effort or source.codex.session_reasoning_effort or "medium"
    )
    if not review_model:
        raise ValidationError("source run is missing effective review model")
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from=OriginKind.SOURCE_RUN,
            source_run_id=source.run_id,
            repository=repository,
            head_branch=head_branch,
            base_branch=base_branch,
            expected_head_sha=expected_head_sha,
        ),
        cursor=ExecutionContextCursor(
            chat_id=source.cursor.chat_id,
            model=source.cursor.model,
            command=source.cursor.command,
            output_format=source.cursor.output_format,
            force=source.cursor.force,
            trust_workspace=source.cursor.trust_workspace,
            sandbox=source.cursor.sandbox,
        ),
        codex=ExecutionContextCodex(
            session_id=source.codex.session_id,
            review_model=review_model,
            review_reasoning_effort=review_effort,
            command=source.codex.command,
            sandbox=source.codex.sandbox,
            review_skill=source.codex.review_skill,
            external_review_skill=v2.external_review_skill,
        ),
        workflow=ExecutionContextWorkflow(
            max_local_iterations=v2.max_local_iterations,
            cursor_timeout_minutes=source.workflow.cursor_timeout_minutes,
            codex_timeout_minutes=source.workflow.codex_timeout_minutes,
        ),
        pr_review_v2=ExecutionContextPrReviewV2(
            gh_command=v2.gh_command,
            git_command=v2.git_command,
            ssh_command=v2.ssh_command,
            remote_name=v2.remote_name,
            reviewer_logins=tuple(v2.reviewer_logins),
            review_trigger_body=v2.review_trigger_body,
            user_mention=v2.user_mention,
            poll_interval_seconds=v2.poll_interval_seconds,
            max_external_cycles=v2.max_external_cycles,
            per_call_timeout_seconds=v2.per_call_timeout_seconds,
            overall_timeout_seconds=v2.overall_timeout_seconds,
            max_pages=v2.max_pages,
            max_items=v2.max_items,
            max_server_directed_wait_seconds=v2.max_server_directed_wait_seconds,
            no_findings_enabled=v2.no_findings.enabled,
            no_findings_prefixes=tuple(v2.no_findings.accepted_comment_prefixes),
            accept_bot_thumbs_up=v2.no_findings.accept_bot_thumbs_up,
            no_findings_prefix_length=v2.no_findings.reviewed_commit_prefix_length,
            worker=ExecutionContextWorker(
                lease_ttl_seconds=v2.worker.lease_ttl_seconds,
                heartbeat_interval_seconds=v2.worker.heartbeat_interval_seconds,
                idle_poll_seconds=v2.worker.idle_poll_seconds,
            ),
        ),
        plan_prompt=ExecutionContextPlanPrompt(
            plan_path=plan_path,
            plan_sha256=plan_sha256,
            prompt_path=prompt_path,
            prompt_sha256=prompt_sha256,
            accepted_patch_sha256=accepted_patch_sha256,
        ),
        repository_root=repository_root,
    )


def create_from_source_run(source_run_id: str, *, config_path: Path | None = None) -> str:
    """Create or reuse a PreparedState v2 run from a completed A/B source run."""

    from ai_dev_loop.process import run_process
    from ai_dev_loop.runners.git import discover_repository, resolve_repo_relative_path

    source_dir, source = load_run(source_run_id)
    if source.status.value not in {"completed", "completed_with_residual_risk"}:
        raise ValidationError(
            "source run must be completed or completed_with_residual_risk "
            "(max_iterations_reached is not accepted)"
        )
    config, _effective, _path = resolve_effective_config(
        repo_root=Path(source.repository.root),
        config_path=config_path,
    )
    v2 = _require_v2_config(config)

    staged_rel = None
    for entry in reversed(source.iterations):
        git_section = entry.get("git") if isinstance(entry, dict) else None
        if isinstance(git_section, dict) and git_section.get("staged_diff_path"):
            staged_rel = str(git_section["staged_diff_path"])
            break
    if not staged_rel:
        raise ValidationError("source run is missing an accepted staged patch artifact")
    patch_path = source_dir / staged_rel
    if not patch_path.is_file() or patch_path.is_symlink():
        raise ValidationError("source staged patch artifact is missing")
    patch_bytes = patch_path.read_bytes()
    if not patch_bytes:
        raise ValidationError("source staged patch artifact is empty")
    accepted_patch_sha256 = sha256_file(patch_path)

    plan_path = source.plan.repository_path
    prompt_path = source.prompt.snapshot_path
    # Prefer immutable run-owned plan snapshot when present; never fall back to
    # mutable repository content as the authoritative frozen source.
    plan_snapshot = source_dir / source.plan.snapshot_path
    prompt_file = source_dir / prompt_path
    if not plan_snapshot.is_file() or plan_snapshot.is_symlink():
        raise ValidationError("source plan snapshot is missing")
    plan_file = plan_snapshot
    plan_sha256 = sha256_file(plan_file)
    if source.plan.sha256 and plan_sha256 != source.plan.sha256:
        raise ValidationError("source plan snapshot hash drift")
    if not prompt_file.is_file() or prompt_file.is_symlink():
        raise ValidationError("source prompt snapshot is missing")
    prompt_sha256 = sha256_file(prompt_file)
    if source.prompt.sha256 and prompt_sha256 != source.prompt.sha256:
        raise ValidationError("source prompt snapshot hash drift")
    plan_bytes = plan_file.read_bytes()
    prompt_bytes = prompt_file.read_bytes()
    if not plan_bytes or not prompt_bytes.strip():
        raise ValidationError("source plan or prompt snapshot is empty")

    repo_root = Path(source.repository.root)
    repo_info = discover_repository(repo_root)
    if str(repo_info.root.resolve()) != str(Path(source.repository.root).resolve()):
        raise ValidationError("source repository root drift")
    if repo_info.branch != source.repository.branch:
        raise ValidationError("source repository branch drift")
    if repo_info.head.lower() != source.repository.initial_head.lower():
        raise ValidationError("source repository HEAD drift from prepared initial_head")
    expected_head = repo_info.head.lower()

    remote = run_process(
        [v2.git_command, "remote", "get-url", v2.remote_name],
        cwd=str(repo_info.root),
        timeout=30,
    )
    if remote.returncode != 0:
        raise ValidationError("failed to resolve repository remote identity")
    repo_name = _owner_repo_from_remote(remote.stdout.strip())

    # Worktree must have the exact accepted staged patch and no unstaged/untracked noise
    # outside ignored files for a safe publication baseline.
    live_staged = run_process(
        [v2.git_command, "diff", "--cached", "--binary"],
        cwd=str(repo_info.root),
        timeout=60,
    )
    if live_staged.returncode != 0:
        raise ValidationError("failed to read live staged patch")
    live_staged_bytes = (live_staged.stdout or "").encode("utf-8", errors="surrogateescape")
    if live_staged_bytes.rstrip(b"\n") != patch_bytes.rstrip(b"\n"):
        raise ValidationError("live staged patch does not match source accepted patch")
    unstaged = run_process(
        [v2.git_command, "diff", "--name-only"],
        cwd=str(repo_info.root),
        timeout=30,
    )
    if unstaged.returncode != 0:
        raise ValidationError("failed to inspect unstaged changes")
    if unstaged.stdout.strip():
        raise ValidationError("repository has unstaged tracked changes")
    untracked = run_process(
        [v2.git_command, "ls-files", "--others", "--exclude-standard"],
        cwd=str(repo_info.root),
        timeout=30,
    )
    if untracked.returncode != 0:
        raise ValidationError("failed to inspect untracked files")
    if untracked.stdout.strip():
        raise ValidationError("repository has untracked non-ignored files")

    if plan_file.is_relative_to(repo_info.root):
        resolve_repo_relative_path(repo_info.root, Path(plan_path))

    ctx = _execution_context_from_source(
        source,
        v2=v2,
        repository=repo_name,
        head_branch=source.repository.branch,
        base_branch=v2.base_branch,
        expected_head_sha=expected_head,
        plan_path=plan_path,
        prompt_path=prompt_path,
        plan_sha256=plan_sha256,
        prompt_sha256=prompt_sha256,
        accepted_patch_sha256=accepted_patch_sha256,
        repository_root=str(repo_info.root),
    )
    engine = _open_engine()
    store = ProtectedResultStore(_artifact_root())
    prep = PreparationService(engine, store)
    result = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id=source.run_id,
            repository=repo_name,
            head_branch=source.repository.branch,
            base_branch=v2.base_branch,
            expected_head_sha=expected_head,
            accepted_patch_bytes=patch_bytes,
            plan_bytes=plan_bytes,
            prompt_bytes=prompt_bytes,
            execution_context=ctx,
        )
    )
    reused = "reused" if result.reused else "created"
    return (
        f"pr-review-v2 {reused} prepared run {result.run_id}\n"
        f"origin: source_run\n"
        f"next: ai_dev_loop pr-review-v2 start {result.run_id}\n"
    )


def prepare_existing_pr(
    *,
    repo: str,
    pr_number: int,
    codex_session_id: str,
    plan_path: Path,
    prompt_path: Path,
    cursor_chat_id: str | None = None,
    review_model: str | None = None,
    config_path: Path | None = None,
    repo_path: Path | None = None,
    discoverer: ExistingPrDiscoverer | None = None,
) -> str:
    """Prepare a PreparedState from an existing open PR (read-only discovery)."""

    import hashlib

    from ai_dev_loop.pr_review_v2.application.preparation import ExistingPrSnapshot
    from ai_dev_loop.process import run_process
    from ai_dev_loop.runners.git import discover_repository, resolve_repo_relative_path

    root = (repo_path or Path.cwd()).resolve()
    config, _effective, _path = resolve_effective_config(repo_root=root, config_path=config_path)
    v2 = _require_v2_config(config)
    session_id = require_codex_session_id(codex_session_id)

    repo_info = discover_repository(root)
    if repo_info.root.resolve() != root.resolve():
        raise ValidationError("repository path does not match git toplevel")

    # Constrain plan/prompt to the repository root (no escape).
    plan_file = resolve_repo_relative_path(repo_info.root, plan_path)
    prompt_file = resolve_repo_relative_path(repo_info.root, prompt_path)
    if not plan_file.is_file() or not prompt_file.is_file():
        raise ValidationError("plan or prompt path is missing")

    remote = run_process(
        [v2.git_command, "remote", "get-url", v2.remote_name],
        cwd=str(repo_info.root),
        timeout=30,
    )
    if remote.returncode != 0:
        raise ValidationError("failed to resolve repository remote identity")
    local_owner_repo = _owner_repo_from_remote(remote.stdout.strip())
    if local_owner_repo.lower() != repo.strip().lower():
        raise ValidationError("local checkout remote does not match --repo")

    session_runtime = read_codex_session_runtime(session_id)
    review_runtime = resolve_effective_review_runtime(
        session=session_runtime,
        configured_review_model=(
            review_model if review_model is not None else config.codex.review_model
        ),
        configured_review_reasoning_effort=config.codex.review_reasoning_effort,
    )

    disc: ExistingPrDiscoverer = discoverer or ExistingPrDiscoverer(
        gh_command=v2.gh_command,
        timeout_seconds=float(v2.per_call_timeout_seconds),
        repository_cwd=str(repo_info.root),
    )
    discovered = disc.discover(owner_repo=repo, pr_number=pr_number)
    binding = discovered.binding

    if repo_info.branch != binding.head_branch:
        raise ValidationError("local checkout branch does not match pull request head branch")
    if repo_info.head.lower() != binding.head_sha.lower():
        raise ValidationError("local checkout HEAD does not match pull request head SHA")

    plan_rel = str(plan_file.relative_to(repo_info.root))
    prompt_rel = str(prompt_file.relative_to(repo_info.root))

    ctx = ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from=OriginKind.EXISTING_PR,
            source_run_id=None,
            repository=binding.repository.name_with_owner,
            head_branch=binding.head_branch,
            base_branch=binding.base_branch,
            expected_head_sha=binding.head_sha,
        ),
        cursor=ExecutionContextCursor(
            chat_id=cursor_chat_id.strip() if cursor_chat_id and cursor_chat_id.strip() else None,
            model=config.cursor.model,
            command=config.cursor.command,
            output_format=config.cursor.output_format,
            force=config.cursor.force,
            trust_workspace=config.cursor.trust_workspace,
            sandbox=config.cursor.sandbox,
        ),
        codex=ExecutionContextCodex(
            session_id=session_id,
            review_model=review_runtime.review_model,
            review_reasoning_effort=review_runtime.review_reasoning_effort,
            command=config.codex.command,
            sandbox=config.codex.sandbox,
            review_skill=config.codex.review_skill,
            external_review_skill=v2.external_review_skill,
        ),
        workflow=ExecutionContextWorkflow(
            max_local_iterations=v2.max_local_iterations,
            cursor_timeout_minutes=config.workflow.cursor_timeout_minutes,
            codex_timeout_minutes=config.workflow.codex_timeout_minutes,
        ),
        pr_review_v2=ExecutionContextPrReviewV2(
            gh_command=v2.gh_command,
            git_command=v2.git_command,
            ssh_command=v2.ssh_command,
            remote_name=v2.remote_name,
            reviewer_logins=tuple(v2.reviewer_logins),
            review_trigger_body=v2.review_trigger_body,
            user_mention=v2.user_mention,
            poll_interval_seconds=v2.poll_interval_seconds,
            max_external_cycles=v2.max_external_cycles,
            per_call_timeout_seconds=v2.per_call_timeout_seconds,
            overall_timeout_seconds=v2.overall_timeout_seconds,
            max_pages=v2.max_pages,
            max_items=v2.max_items,
            max_server_directed_wait_seconds=v2.max_server_directed_wait_seconds,
            no_findings_enabled=v2.no_findings.enabled,
            no_findings_prefixes=tuple(v2.no_findings.accepted_comment_prefixes),
            accept_bot_thumbs_up=v2.no_findings.accept_bot_thumbs_up,
            no_findings_prefix_length=v2.no_findings.reviewed_commit_prefix_length,
            worker=ExecutionContextWorker(
                lease_ttl_seconds=v2.worker.lease_ttl_seconds,
                heartbeat_interval_seconds=v2.worker.heartbeat_interval_seconds,
                idle_poll_seconds=v2.worker.idle_poll_seconds,
            ),
        ),
        plan_prompt=ExecutionContextPlanPrompt(
            plan_path=plan_rel,
            plan_sha256=sha256_file(plan_file),
            prompt_path=prompt_rel,
            prompt_sha256=sha256_file(prompt_file),
            accepted_patch_sha256=hashlib.sha256(b"").hexdigest(),
        ),
        repository_root=str(repo_info.root),
    )
    plan_bytes = plan_file.read_bytes()
    prompt_bytes = prompt_file.read_bytes()
    if not plan_bytes or not prompt_bytes.strip():
        raise ValidationError("plan or prompt content is empty")
    engine = _open_engine()
    store = ProtectedResultStore(_artifact_root())
    prep = PreparationService(engine, store)
    result = prep.prepare_existing_pr(
        ExistingPrSnapshot(
            binding=binding,
            execution_context=ctx,
            plan_bytes=plan_bytes,
            prompt_bytes=prompt_bytes,
            title=discovered.title,
            body=discovered.body,
            accepted_patch_bytes=None,
        )
    )
    reused = "reused" if result.reused else "created"
    return (
        f"pr-review-v2 {reused} prepared run {result.run_id}\n"
        f"origin: existing_pr\n"
        f"pr: {binding.pr_number}\n"
        f"next: ai_dev_loop pr-review-v2 start {result.run_id}\n"
    )


def start_run(run_id: str) -> str:
    from ai_dev_loop.pr_review_v2.workers.spawn import spawn_detached_supervisor

    engine = _open_engine()
    store = ProtectedResultStore(_artifact_root())
    launchers = SupervisorLauncherStore(_artifact_root())

    def _spawn(rid: str) -> str:
        return spawn_detached_supervisor(
            rid, artifact_root=_artifact_root(), launcher_store=launchers
        )

    control = ControlPlaneService(
        engine, artifact_store=store, launcher_store=launchers, spawner=_spawn
    )
    try:
        result = control.start(run_id)
    except ControlError as exc:
        raise ValidationError(exc.safe_message) from exc
    return (
        f"pr-review-v2 start {result.run_id}\n"
        f"state: {result.state_kind}\n"
        f"transition_applied: {result.transition_applied}\n"
        f"supervisor: {result.supervisor_action}\n"
        f"next_action: {result.next_action.value}\n"
        + (f"detail: {result.safe_detail}\n" if result.safe_detail else "")
    )


def status_run(run_id: str, *, output: str = "text") -> str:
    engine = _open_engine()
    store = ProtectedResultStore(_artifact_root())
    launchers = SupervisorLauncherStore(_artifact_root())
    control = ControlPlaneService(engine, artifact_store=store, launcher_store=launchers)
    try:
        status = control.status(run_id)
    except Exception as exc:  # noqa: BLE001
        raise ValidationError(str(exc)) from exc
    if output == "json":
        return status.model_dump_json(indent=2) + "\n"
    lines = [
        f"run_id: {status.run_id}",
        f"state: {status.state_kind}",
        f"origin: {status.origin_kind.value if status.origin_kind else 'unknown'}",
        f"cycle: {status.cycle_number}",
        f"next_action: {status.next_action.value}",
        f"supervisor_live: {status.supervisor_live}",
    ]
    if status.repository:
        lines.append(f"repository: {status.repository}")
    if status.pr_number is not None:
        lines.append(f"pr: {status.pr_number}")
    if status.active_effect_kind:
        lines.append(f"active_effect: {status.active_effect_kind}")
    if status.last_error_summary:
        lines.append(f"last_error: {status.last_error_summary}")
    return "\n".join(lines) + "\n"


def history_run(
    run_id: str, *, limit: int = 50, order: str = "oldest", output: str = "text"
) -> str:
    engine = _open_engine()
    store = ProtectedResultStore(_artifact_root())
    launchers = SupervisorLauncherStore(_artifact_root())
    control = ControlPlaneService(engine, artifact_store=store, launcher_store=launchers)
    try:
        result = control.history(run_id, limit=limit, order=order)
    except ControlError as exc:
        raise ValidationError(exc.safe_message) from exc
    if output == "json":
        return result.model_dump_json(indent=2) + "\n"
    lines = [f"history for {result.run_id} ({result.order}, limit={result.limit})"]
    if result.truncated:
        lines.append("(truncated)")
    for entry in result.entries:
        lines.append(f"{entry.sequence:04d} {entry.event_kind} {entry.disposition}")
    return "\n".join(lines) + "\n"


def resume_run(run_id: str, *, confirm_user_continuation: bool = False) -> str:
    from ai_dev_loop.pr_review_v2.workers.spawn import spawn_detached_supervisor

    engine = _open_engine()
    store = ProtectedResultStore(_artifact_root())
    launchers = SupervisorLauncherStore(_artifact_root())

    def _spawn(rid: str) -> str:
        return spawn_detached_supervisor(
            rid, artifact_root=_artifact_root(), launcher_store=launchers
        )

    control = ControlPlaneService(
        engine,
        artifact_store=store,
        launcher_store=launchers,
        spawner=_spawn,
    )
    try:
        result = control.resume(run_id, confirm_user_continuation=confirm_user_continuation)
    except ControlError as exc:
        raise ValidationError(exc.safe_message) from exc
    return (
        f"pr-review-v2 resume {result.run_id}\n"
        f"state: {result.state_kind}\n"
        f"transition_applied: {result.transition_applied}\n"
        f"supervisor: {result.supervisor_action}\n"
        f"next_action: {result.next_action.value}\n"
    )


def abort_run(run_id: str) -> str:
    engine = _open_engine()
    store = ProtectedResultStore(_artifact_root())
    launchers = SupervisorLauncherStore(_artifact_root())
    control = ControlPlaneService(engine, artifact_store=store, launcher_store=launchers)
    try:
        result = control.abort(run_id)
    except ControlError as exc:
        raise ValidationError(exc.safe_message) from exc
    return (
        f"pr-review-v2 abort {result.run_id}\n"
        f"state: {result.state_kind}\n"
        f"abort_persisted: {result.abort_persisted}\n"
        f"process_action: {result.process_action.value}\n"
    )
