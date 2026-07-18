"""Independent existing-PR adoption for Phase 15.5 PR-review cycles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_dev_loop.abort_control import is_abort_requested
from ai_dev_loop.commands.pr_review import (
    _ensure_review_trigger_comment,
    _mark_status,
    _require_github_config,
    _run_locks,
    _spawn_pr_review_worker,
    _worker_is_live,
    load_run_state_fresh,
)
from ai_dev_loop.commands.prepare import (
    PrepareOptions,
    _build_overrides,
    _read_stdin_prompt,
    _resolve_inputs,
)
from ai_dev_loop.config import ProjectConfig, resolve_effective_config
from ai_dev_loop.errors import UsageError, ValidationError
from ai_dev_loop.event_log import append_orchestrator_event
from ai_dev_loop.integrations.codex.session_runtime import (
    read_codex_session_runtime,
    require_codex_session_id,
)
from ai_dev_loop.paths import (
    ensure_app_dirs,
    ensure_dir,
    run_dir,
    runs_dir,
    set_sensitive_file_mode,
)
from ai_dev_loop.resume_planner import TERMINAL_STATUSES
from ai_dev_loop.review_runtime import resolve_effective_review_runtime
from ai_dev_loop.run_discovery import list_run_directories, load_run
from ai_dev_loop.runners.git import (
    GitRepositoryInfo,
    copy_file_atomic,
    discover_repository,
    relative_repo_path,
    validate_clean_worktree,
)
from ai_dev_loop.runners.github import (
    GithubPullRequest,
    check_gh_auth,
    get_pull_request,
    resolve_repository_nwo,
)
from ai_dev_loop.runners.probes import (
    CompatibilityClassification,
    probe_cursor_model_compatibility,
    probe_version,
    run_tool_compatibility_probes,
)
from ai_dev_loop.runners.publish import resolve_upstream, verify_ssh_push_ready
from ai_dev_loop.state import (
    CodexState,
    ControllerState,
    CursorState,
    GithubPrReviewState,
    ManifestArtifact,
    PlanState,
    ProjectRef,
    PromptState,
    RepositoryState,
    RunManifest,
    RunState,
    RunStatus,
    WorkflowState,
    atomic_write_json,
    atomic_write_text,
    atomic_write_yaml,
    generate_run_id,
    save_run_state,
    serialize_run_state,
    sha256_bytes,
    sha256_file,
    sha256_text,
    utc_now,
)

_INDEPENDENT_TERMINAL_LIFECYCLES = frozenset(
    {"completed", "failed", "aborted", "max_external_cycles_reached"}
)
_MODEL_SELECTION_ARTIFACT = "cursor/model-selection.json"
_SET_CURSOR_MODEL_ELIGIBLE_LIFECYCLES = frozenset({"prepared_independent", "awaiting_bot_review"})


@dataclass(frozen=True)
class IndependentPrReviewPrepareOptions:
    config_path: Path | None = None
    project_name: str | None = None
    repo_path: Path | None = None
    pr_number: int | None = None
    branch: str | None = None
    plan_path: Path | None = None
    prompt_source_path: Path | None = None
    codex_session_id: str | None = None
    controller_session_id: str | None = None
    cursor_command: str | None = None
    cursor_model: str | None = None
    cursor_output_format: str | None = None
    codex_command: str | None = None
    codex_review_model: str | None = None
    codex_review_reasoning_effort: str | None = None
    review_skill: str | None = None
    max_review_iterations: int | None = None
    cursor_timeout_minutes: int | None = None
    codex_timeout_minutes: int | None = None
    output: str = "text"


@dataclass(frozen=True)
class IndependentPrReviewPrepareResult:
    run_id: str
    project: str
    pr_number: int
    status: str
    lifecycle: str
    start_command: str
    reused_existing: bool
    controller_session_id: str | None
    requires_codex_exit: bool
    reviewer_must_remain_inactive: bool
    bound_head_sha_prefix: str
    cursor_model: str
    cursor_model_mutable: bool
    message: str
    model_family_warning: str | None = None
    model_mismatch_warning: str | None = None


@dataclass(frozen=True)
class IndependentPrReviewStartResult:
    run_id: str
    status: str
    lifecycle: str
    pr_number: int
    already_running: bool
    message: str


@dataclass(frozen=True)
class SetCursorModelResult:
    run_id: str
    previous_model: str
    new_model: str
    mutable: bool
    message: str


def _to_prepare_options(options: IndependentPrReviewPrepareOptions) -> PrepareOptions:
    return PrepareOptions(
        config_path=options.config_path,
        project_name=options.project_name,
        repo_path=options.repo_path,
        plan_path=options.plan_path,
        prompt_source_path=options.prompt_source_path,
        codex_session_id=options.codex_session_id,
        controller_session_id=options.controller_session_id,
        cursor_command=options.cursor_command,
        cursor_model=options.cursor_model,
        cursor_output_format=options.cursor_output_format,
        codex_command=options.codex_command,
        codex_review_model=options.codex_review_model,
        codex_review_reasoning_effort=options.codex_review_reasoning_effort,
        review_skill=options.review_skill,
        max_review_iterations=options.max_review_iterations,
        cursor_timeout_minutes=options.cursor_timeout_minutes,
        codex_timeout_minutes=options.codex_timeout_minutes,
        output=options.output,
    )


def _create_independent_layout(base: Path) -> None:
    for relative in (
        "plan",
        "prompts/fixes",
        "cursor/iterations",
        "codex/reviews",
        "codex/events",
        "preflight",
        "logs",
        "locks",
        "git/status",
        "git/diffs",
        "git/cursor-output",
        "github/cycles",
    ):
        ensure_dir(base / relative)


def _is_active_pr_cycle(state: RunState) -> bool:
    gpr = state.github_pr_review
    if gpr is None:
        return False
    if state.status in TERMINAL_STATUSES:
        return False
    return gpr.lifecycle not in _INDEPENDENT_TERMINAL_LIFECYCLES


def _find_matching_independent_cycle(
    *,
    repository_nwo: str,
    pr_number: int,
    bound_head_sha: str,
) -> RunState | None:
    for _, state in list_run_directories():
        gpr = state.github_pr_review
        if gpr is None or gpr.origin != "independent_pr":
            continue
        if not _is_active_pr_cycle(state):
            continue
        if (
            gpr.repository_name_with_owner == repository_nwo
            and gpr.pr_number == pr_number
            and gpr.bound_head_sha == bound_head_sha
        ):
            return state
    return None


def _find_conflicting_independent_cycle(
    *,
    repository_root: str,
    repository_nwo: str,
    pr_number: int,
    bound_head_sha: str,
) -> RunState | None:
    for _, state in list_run_directories():
        gpr = state.github_pr_review
        if gpr is None or gpr.origin != "independent_pr":
            continue
        if not _is_active_pr_cycle(state):
            continue
        same_repo = Path(state.repository.root).resolve() == Path(repository_root).resolve()
        same_pr = gpr.repository_name_with_owner == repository_nwo and gpr.pr_number == pr_number
        if not (same_repo or same_pr):
            continue
        if (
            gpr.repository_name_with_owner == repository_nwo
            and gpr.pr_number == pr_number
            and gpr.bound_head_sha == bound_head_sha
        ):
            continue
        return state
    return None


def _validate_open_pr_binding(
    *,
    command: str,
    repo_info: GitRepositoryInfo,
    pr_number: int,
    branch: str,
    pr_base: str,
) -> tuple[GithubPullRequest, str]:
    if repo_info.branch in {"HEAD", ""} or repo_info.branch.startswith("("):
        raise ValidationError("detached HEAD is not allowed for independent PR-review prepare")
    if repo_info.branch != branch:
        raise ValidationError(
            f"checked-out branch {repo_info.branch!r} does not match requested branch {branch!r}"
        )
    nwo = resolve_repository_nwo(command, cwd=str(repo_info.root))
    pr = get_pull_request(command, cwd=str(repo_info.root), pr_number=pr_number)
    if pr.state != "OPEN":
        raise ValidationError(f"pull request #{pr_number} is not open")
    if pr.is_cross_repository:
        raise ValidationError("cross-repository PRs are not supported")
    if pr.base_ref != pr_base:
        raise ValidationError(f"PR base must be {pr_base}; found {pr.base_ref!r}")
    if pr.head_ref != branch:
        raise ValidationError(
            f"PR head branch {pr.head_ref!r} does not match requested branch {branch!r}"
        )
    if pr.head_sha != repo_info.head:
        raise ValidationError(
            "local HEAD does not match the requested PR head SHA; "
            "check out the exact PR head before prepare"
        )
    if pr.repository_name_with_owner and pr.repository_name_with_owner != nwo:
        raise ValidationError("PR repository identity does not match the local repository")
    return pr, nwo


def _revalidate_independent_binding(state: RunState, config: ProjectConfig) -> GithubPullRequest:
    assert config.github is not None
    assert state.github_pr_review is not None
    gpr = state.github_pr_review
    if gpr.pr_number is None:
        raise ValidationError("independent cycle is missing bound PR number")
    repo_info = discover_repository(Path(state.repository.root))
    if (
        repo_info.root.resolve() != Path(state.repository.root).resolve()
        or str(repo_info.git_common_dir) != state.repository.git_common_dir
        or str(repo_info.git_dir) != state.repository.git_dir
    ):
        raise ValidationError("repository identity drifted from the prepared independent cycle")
    pr, nwo = _validate_open_pr_binding(
        command=config.github.command,
        repo_info=repo_info,
        pr_number=gpr.pr_number,
        branch=gpr.head_branch,
        pr_base=gpr.base_branch,
    )
    if gpr.bound_head_sha != pr.head_sha:
        raise ValidationError("PR head SHA drifted from the prepared bound head")
    if gpr.repository_name_with_owner != nwo:
        raise ValidationError("repository NWO drifted from the prepared independent cycle")
    if repo_info.head != state.repository.initial_head:
        raise ValidationError("local HEAD drifted from the prepared independent baseline")
    if repo_info.branch != gpr.head_branch:
        raise ValidationError("local branch drifted from the prepared independent binding")
    return pr


def validate_independent_pre_cursor_baseline(
    state: RunState, config: ProjectConfig
) -> GithubPullRequest:
    """Re-verify PR/local binding and a clean correction baseline before Cursor.

    Must run under run/repo locks immediately before creating a Cursor chat or
    beginning the first independent Cursor turn.
    """

    pr = _revalidate_independent_binding(state, config)
    repo_root = Path(state.repository.root)
    validate_clean_worktree(
        discover_repository(repo_root),
        plan_path=repo_root / state.plan.repository_path,
        prompt_source_path=repo_root / state.prompt.source_repository_path,
        repo_root=repo_root,
        require_clean=True,
    )
    return pr


def _require_start_tool_compatibility(run_directory: Path, state: RunState) -> None:
    """Fail closed before any GitHub write when Cursor or Codex models are incompatible.

    Independent start does not accept update flags; updaters run only through the
    existing explicit start/resume/launch policy elsewhere.
    """

    results, _versions = run_tool_compatibility_probes(
        cursor_command=state.cursor.command,
        cursor_model=state.cursor.model,
        codex_command=state.codex.command,
        codex_model=state.codex.review_model,
        run_directory=run_directory,
    )
    failures = [
        item for item in results if item.classification != CompatibilityClassification.COMPATIBLE
    ]
    if not failures:
        return
    details = "; ".join(
        f"{item.tool}/{item.classification.value}: {item.detail}" for item in failures
    )
    raise ValidationError(f"tool compatibility preflight failed before GitHub write ({details})")


def _cursor_model_mutable(state: RunState) -> bool:
    gpr = state.github_pr_review
    if gpr is None or gpr.origin != "independent_pr":
        return False
    if gpr.lifecycle not in _SET_CURSOR_MODEL_ELIGIBLE_LIFECYCLES:
        return False
    if state.cursor.chat_id:
        return False
    return not state.iterations


def prepare_independent_pr_review(
    options: IndependentPrReviewPrepareOptions,
) -> IndependentPrReviewPrepareResult:
    ensure_app_dirs()
    if options.pr_number is None or options.pr_number < 1:
        raise UsageError("independent pr-review prepare requires --pr <number>")
    if not options.branch or not options.branch.strip():
        raise UsageError("independent pr-review prepare requires --branch <name>")
    branch = options.branch.strip()
    prompt_text = _read_stdin_prompt()
    prepare_opts = _to_prepare_options(options)
    repo_candidate = options.repo_path or Path.cwd()
    repo_info = discover_repository(repo_candidate)
    effective, source_repo_config, _ = resolve_effective_config(
        repo_root=repo_info.root,
        config_path=options.config_path,
        overrides=_build_overrides(prepare_opts),
    )
    if not effective.github_enabled() or effective.github is None:
        raise ValidationError(
            "GitHub PR review is disabled; set github.enabled: true in ai_dev_loop.yaml"
        )
    github = effective.github
    plan_path, prompt_source_path = _resolve_inputs(prepare_opts, repo_info)
    validate_clean_worktree(
        repo_info,
        plan_path=plan_path,
        prompt_source_path=prompt_source_path,
        repo_root=repo_info.root,
        require_clean=True,
    )
    auth = check_gh_auth(github.command)
    if not auth.authenticated:
        raise ValidationError(f"gh authentication failed: {auth.detail}")

    pr, nwo = _validate_open_pr_binding(
        command=github.command,
        repo_info=repo_info,
        pr_number=options.pr_number,
        branch=branch,
        pr_base=github.pr_base,
    )

    matching = _find_matching_independent_cycle(
        repository_nwo=nwo,
        pr_number=pr.number,
        bound_head_sha=pr.head_sha,
    )
    if matching is not None:
        is_ab = matching.controller is not None
        controller_id = matching.controller.controller_session_id if matching.controller else None
        start_command = (
            f"ai_dev_loop pr-review start {matching.run_id} --controller-session-id {controller_id}"
            if is_ab and controller_id
            else f"ai_dev_loop pr-review start {matching.run_id}"
        )
        return IndependentPrReviewPrepareResult(
            run_id=matching.run_id,
            project=matching.project.name,
            pr_number=pr.number,
            status=matching.status.value,
            lifecycle=matching.github_pr_review.lifecycle
            if matching.github_pr_review
            else "prepared_independent",
            start_command=start_command,
            reused_existing=True,
            controller_session_id=controller_id,
            requires_codex_exit=not is_ab,
            reviewer_must_remain_inactive=is_ab,
            bound_head_sha_prefix=pr.head_sha[:12],
            cursor_model=matching.cursor.model,
            cursor_model_mutable=_cursor_model_mutable(matching),
            message=(
                f"Reusing existing independent PR-review cycle {matching.run_id} "
                f"for PR #{pr.number} at {pr.head_sha[:12]}"
            ),
            model_family_warning=matching.codex.model_family_warning,
        )

    conflict = _find_conflicting_independent_cycle(
        repository_root=str(repo_info.root),
        repository_nwo=nwo,
        pr_number=pr.number,
        bound_head_sha=pr.head_sha,
    )
    if conflict is not None:
        raise ValidationError(
            "an active independent PR-review cycle already exists for this "
            "repository worktree or PR with a different binding; abort or finish it first"
        )

    session_id = require_codex_session_id(options.codex_session_id)
    controller_session_id: str | None = None
    if options.controller_session_id is not None:
        controller_session_id = require_codex_session_id(options.controller_session_id)
        if controller_session_id == session_id:
            raise ValidationError(
                "controller session id must differ from the reviewer Codex session id; "
                "refusing equal A/B identities"
            )
    session_runtime = read_codex_session_runtime(session_id)
    review_runtime = resolve_effective_review_runtime(
        session=session_runtime,
        configured_review_model=effective.codex.review_model,
        configured_review_reasoning_effort=effective.codex.review_reasoning_effort,
    )

    now = utc_now()
    run_id = generate_run_id(effective.project.name, now=now)
    destination = run_dir(effective.project.name, run_id)
    if destination.exists():
        raise ValidationError(f"run directory already exists: {destination}")
    project_root = runs_dir() / effective.project.name
    ensure_dir(project_root)
    _create_independent_layout(destination)

    plan_snapshot = destination / "plan" / "plan.md"
    prompt_snapshot = destination / "prompts" / "cursor-initial.txt"
    copy_file_atomic(plan_path, plan_snapshot)
    atomic_write_text(prompt_snapshot, prompt_text, sensitive=True)

    effective_yaml_path = destination / "effective-config.yaml"
    source_yaml_path = destination / "source-config.yaml"
    atomic_write_yaml(effective_yaml_path, effective.model_dump(by_alias=True), sensitive=True)
    atomic_write_yaml(
        source_yaml_path, source_repo_config.model_dump(by_alias=True), sensitive=True
    )
    baseline_status_path = destination / "git" / "baseline-status.txt"
    atomic_write_text(baseline_status_path, "", sensitive=True)

    session_runtime_artifact = {
        "session_id_prefix": session_id[:8],
        "model": review_runtime.session_model,
        "reasoning_effort": review_runtime.session_reasoning_effort,
        "origin": review_runtime.session_origin,
        "source_event_type": review_runtime.source_event_type,
        "source_timestamp": review_runtime.source_timestamp,
        "review_model": review_runtime.review_model,
        "review_reasoning_effort": review_runtime.review_reasoning_effort,
        "review_model_source": review_runtime.review_model_source,
        "review_reasoning_source": review_runtime.review_reasoning_source,
        "model_mismatch_warning": review_runtime.model_mismatch_warning,
        "model_family_warning": review_runtime.model_family_warning,
    }
    session_runtime_path = destination / "codex" / "session-runtime.json"
    atomic_write_json(session_runtime_path, session_runtime_artifact, sensitive=True)

    pr_binding_path = destination / "github" / "pr-binding.json"
    atomic_write_json(
        pr_binding_path,
        {
            "schema_version": 1,
            "origin": "independent_pr",
            "pr_number": pr.number,
            "pr_url": pr.url or None,
            "repository_name_with_owner": nwo,
            "head_branch": pr.head_ref,
            "base_branch": pr.base_ref,
            "bound_head_sha": pr.head_sha,
            "prepared_at": now.isoformat(),
        },
        sensitive=True,
    )

    plan_hash = sha256_file(plan_snapshot)
    prompt_hash = sha256_text(prompt_text)
    source_config_hash = sha256_file(source_yaml_path)
    effective_config_hash = sha256_bytes(effective_yaml_path.read_bytes())
    session_runtime_hash = sha256_file(session_runtime_path)
    pr_binding_hash = sha256_file(pr_binding_path)
    plan_repo_path = relative_repo_path(repo_info.root, plan_path)
    prompt_repo_path = relative_repo_path(repo_info.root, prompt_source_path)

    max_local = (
        options.max_review_iterations
        if options.max_review_iterations is not None
        else github.max_local_review_iterations
    )
    state = RunState(
        run_id=run_id,
        project=ProjectRef(name=effective.project.name),
        status=RunStatus.PREPARED,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root=str(repo_info.root),
            git_common_dir=str(repo_info.git_common_dir),
            git_dir=str(repo_info.git_dir),
            branch=branch,
            initial_head=pr.head_sha,
            baseline_status_path="git/baseline-status.txt",
        ),
        plan=PlanState(
            repository_path=plan_repo_path,
            snapshot_path="plan/plan.md",
            sha256=plan_hash,
        ),
        prompt=PromptState(
            source_repository_path=prompt_repo_path,
            snapshot_path="prompts/cursor-initial.txt",
            sha256=prompt_hash,
        ),
        codex=CodexState(
            command=effective.codex.command,
            session_id=session_id,
            session_model=review_runtime.session_model,
            session_reasoning_effort=review_runtime.session_reasoning_effort,
            review_model=review_runtime.review_model,
            review_reasoning_effort=review_runtime.review_reasoning_effort,
            review_model_source=review_runtime.review_model_source,
            review_reasoning_source=review_runtime.review_reasoning_source,
            model_family_warning=review_runtime.model_family_warning,
            review_skill=effective.codex.review_skill,
            sandbox=effective.codex.sandbox,
        ),
        cursor=CursorState(
            command=effective.cursor.command,
            model=effective.cursor.model,
            output_format=effective.cursor.output_format,
            force=effective.cursor.force,
            trust_workspace=effective.cursor.trust_workspace,
            sandbox=effective.cursor.sandbox,
            chat_id=None,
        ),
        workflow=WorkflowState(
            max_review_iterations=max_local,
            current_review_iteration=0,
            stage_mode=effective.workflow.stage_mode,
            cursor_timeout_minutes=(
                options.cursor_timeout_minutes
                if options.cursor_timeout_minutes is not None
                else effective.workflow.cursor_timeout_minutes
            ),
            codex_timeout_minutes=(
                options.codex_timeout_minutes
                if options.codex_timeout_minutes is not None
                else effective.workflow.codex_timeout_minutes
            ),
        ),
        controller=None
        if controller_session_id is None
        else ControllerState(controller_session_id=controller_session_id),
        result=(
            f"Independent PR-review cycle prepared for PR #{pr.number}; "
            "no GitHub write until pr-review start"
        ),
        github_pr_review=GithubPrReviewState(
            origin="independent_pr",
            source_run_id=None,
            lifecycle="prepared_independent",
            cycle_number=1,
            max_external_cycles=github.max_external_cycles,
            pr_number=pr.number,
            pr_url=pr.url or None,
            repository_name_with_owner=nwo,
            head_branch=branch,
            base_branch=github.pr_base,
            bound_head_sha=pr.head_sha,
        ),
    )

    manifest = RunManifest(
        run_id=run_id,
        project=effective.project.name,
        created_at=now,
        artifacts=[
            ManifestArtifact(path="plan/plan.md", sha256=plan_hash),
            ManifestArtifact(path="prompts/cursor-initial.txt", sha256=prompt_hash),
            ManifestArtifact(path="source-config.yaml", sha256=source_config_hash),
            ManifestArtifact(path="effective-config.yaml", sha256=effective_config_hash),
            ManifestArtifact(
                path="git/baseline-status.txt", sha256=sha256_file(baseline_status_path)
            ),
            ManifestArtifact(path="codex/session-runtime.json", sha256=session_runtime_hash),
            ManifestArtifact(path="github/pr-binding.json", sha256=pr_binding_hash),
        ],
    )
    atomic_write_json(
        destination / "plan" / "metadata.json",
        {
            "repository_path": plan_repo_path,
            "snapshot_path": "plan/plan.md",
            "original_filename": plan_path.name,
            "sha256": plan_hash,
        },
    )
    atomic_write_json(destination / "state.json", serialize_run_state(state), sensitive=True)
    atomic_write_json(
        destination / "manifest.json",
        json.loads(manifest.model_dump_json(by_alias=True)),
    )
    set_sensitive_file_mode(destination / "state.json")
    append_orchestrator_event(
        destination,
        run_id=run_id,
        component="orchestrator",
        event="independent_pr_review_prepared",
        status=RunStatus.PREPARED.value,
        detail={
            "origin": "independent_pr",
            "pr_number": pr.number,
            "bound_head_sha_prefix": pr.head_sha[:12],
            "has_controller": controller_session_id is not None,
            "cursor_model": effective.cursor.model,
        },
    )

    is_ab = controller_session_id is not None
    start_command = (
        f"ai_dev_loop pr-review start {run_id} --controller-session-id {controller_session_id}"
        if is_ab
        else f"ai_dev_loop pr-review start {run_id}"
    )
    return IndependentPrReviewPrepareResult(
        run_id=run_id,
        project=effective.project.name,
        pr_number=pr.number,
        status=state.status.value,
        lifecycle="prepared_independent",
        start_command=start_command,
        reused_existing=False,
        controller_session_id=controller_session_id,
        requires_codex_exit=not is_ab,
        reviewer_must_remain_inactive=is_ab,
        bound_head_sha_prefix=pr.head_sha[:12],
        cursor_model=state.cursor.model,
        cursor_model_mutable=True,
        message=(
            f"Prepared independent PR-review cycle {run_id} for PR #{pr.number}. "
            "No GitHub write, Cursor chat, or Codex turn occurred. "
            + (
                "Leave reviewer B inactive; start from controller A only."
                if is_ab
                else "Exit/inactivate the reviewer Codex session before start."
            )
        ),
        model_family_warning=review_runtime.model_family_warning,
        model_mismatch_warning=review_runtime.model_mismatch_warning,
    )


def start_independent_pr_review(
    run_id: str,
    *,
    controller_session_id: str | None = None,
) -> IndependentPrReviewStartResult:
    run_directory, state = load_run(run_id)
    gpr = state.github_pr_review
    if gpr is None or gpr.origin != "independent_pr":
        raise ValidationError(
            "pr-review start requires an independent_pr cycle; "
            "use pr-review create for source-run cycles"
        )
    if gpr.lifecycle == "awaiting_bot_review" and _worker_is_live(run_directory, run_id):
        return IndependentPrReviewStartResult(
            run_id=run_id,
            status=state.status.value,
            lifecycle=gpr.lifecycle,
            pr_number=gpr.pr_number or 0,
            already_running=True,
            message=f"PR-review worker already running for {run_id}",
        )

    # Fresh start or idempotent respawn after a failed launcher with durable awaiting state.
    respawn_only = (
        gpr.lifecycle == "awaiting_bot_review"
        and state.status in {RunStatus.AWAITING_BOT_REVIEW, RunStatus.INTERRUPTED}
        and not _worker_is_live(run_directory, run_id)
    )
    if not respawn_only:
        if gpr.lifecycle != "prepared_independent":
            raise ValidationError(
                f"pr-review start requires lifecycle prepared_independent "
                f"or awaiting_bot_review without a live worker (current: {gpr.lifecycle})"
            )
        if state.status != RunStatus.PREPARED:
            raise ValidationError(
                f"pr-review start requires status prepared (current: {state.status.value})"
            )

    if state.controller is not None:
        if controller_session_id is None:
            raise ValidationError(
                "independent A/B cycle requires --controller-session-id matching state.controller"
            )
        controller_id = require_codex_session_id(controller_session_id)
        if state.controller.controller_session_id != controller_id:
            raise ValidationError(
                "controller session id does not match the prepared independent cycle"
            )
        if state.codex.session_id == controller_id:
            raise ValidationError("controller session id must differ from the reviewer session id")
    elif controller_session_id is not None:
        raise ValidationError(
            "run was prepared without --controller-session-id; omit the controller flag "
            "and ensure the reviewer session is inactive before start"
        )

    config = _require_github_config(Path(state.repository.root))
    assert config.github is not None
    github = config.github

    locks = _run_locks(run_directory, run_id=run_id, repository_path=state.repository.root)
    locks.acquire()
    try:
        state = load_run_state_fresh(run_directory)
        assert state.github_pr_review is not None
        if is_abort_requested(run_directory):
            raise ValidationError("abort requested; refusing pr-review start")
        if _worker_is_live(run_directory, run_id):
            return IndependentPrReviewStartResult(
                run_id=run_id,
                status=state.status.value,
                lifecycle=state.github_pr_review.lifecycle,
                pr_number=state.github_pr_review.pr_number or 0,
                already_running=True,
                message=f"PR-review worker already running for {run_id}",
            )

        auth = check_gh_auth(github.command)
        if not auth.authenticated:
            raise ValidationError(f"gh authentication failed: {auth.detail}")
        if not github.reviewer_logins:
            raise ValidationError("github.reviewer_logins must be configured")

        validate_clean_worktree(
            discover_repository(Path(state.repository.root)),
            plan_path=Path(state.repository.root) / state.plan.repository_path,
            prompt_source_path=Path(state.repository.root) / state.prompt.source_repository_path,
            repo_root=Path(state.repository.root),
            require_clean=True,
        )
        _revalidate_independent_binding(state, config)

        remote, _ = resolve_upstream(
            Path(state.repository.root), state.github_pr_review.head_branch
        )
        verify_ssh_push_ready(Path(state.repository.root), remote)

        # Both Cursor and frozen Codex review models must be compatible before any write.
        _require_start_tool_compatibility(run_directory, state)

        assert state.github_pr_review.pr_number is not None
        commit_sha = state.github_pr_review.bound_head_sha
        request_comment_id, request_created_at = _ensure_review_trigger_comment(
            run_directory,
            command=github.command,
            cwd=state.repository.root,
            pr_number=state.github_pr_review.pr_number,
            review_trigger_body=github.review_trigger_body,
            run_id=state.run_id,
            cycle_number=state.github_pr_review.cycle_number,
            commit_sha=commit_sha,
        )
        state = load_run_state_fresh(run_directory)
        assert state.github_pr_review is not None
        if state.status != RunStatus.AWAITING_BOT_REVIEW:
            _mark_status(state, RunStatus.AWAITING_BOT_REVIEW)
        state.github_pr_review = state.github_pr_review.model_copy(
            update={
                "lifecycle": "awaiting_bot_review",
                "request_comment_id": request_comment_id,
                "request_created_at": request_created_at,
                "worker_outcome": None,
            }
        )
        state.result = (
            f"PR #{state.github_pr_review.pr_number} trigger posted at "
            f"{commit_sha[:12]}; awaiting Codex-bot review"
        )
        state.last_error = None
        save_run_state(run_directory, state)
        append_orchestrator_event(
            run_directory,
            run_id=state.run_id,
            component="orchestrator",
            event="independent_pr_review_started"
            if not respawn_only
            else "independent_pr_review_worker_respawned",
            status=state.status.value,
            detail={
                "origin": "independent_pr",
                "pr_number": state.github_pr_review.pr_number,
                "bound_head_sha_prefix": commit_sha[:12],
                "respawn_only": respawn_only,
            },
        )
    finally:
        locks.release()

    try:
        _spawn_pr_review_worker(run_directory, run_id)
    except Exception as exc:
        # Durable awaiting checkpoint already exists; make resume restartable and
        # keep the trigger comment bound (no duplicate write on the next start/resume).
        locks.acquire()
        try:
            state = load_run_state_fresh(run_directory)
            from ai_dev_loop.commands.start_preflight import mark_interrupted

            mark_interrupted(
                state,
                "PR-review worker launch failed after trigger checkpoint; "
                "use pr-review resume or pr-review start to respawn without duplicating the trigger",
            )
            if state.github_pr_review is not None:
                state.github_pr_review = state.github_pr_review.model_copy(
                    update={
                        "lifecycle": "awaiting_bot_review",
                        "worker_outcome": "worker_launch_failed",
                    }
                )
            save_run_state(run_directory, state)
            append_orchestrator_event(
                run_directory,
                run_id=run_id,
                component="orchestrator",
                event="independent_pr_review_worker_launch_failed",
                status=state.status.value,
                detail={"error_class": type(exc).__name__},
            )
        finally:
            locks.release()
        raise ValidationError(
            "detached PR-review worker failed to start after the review trigger checkpoint; "
            "state is interrupted and awaiting_bot_review — run pr-review resume "
            "(or pr-review start) to respawn without posting a duplicate trigger"
        ) from exc

    state = load_run_state_fresh(run_directory)
    assert state.github_pr_review is not None
    return IndependentPrReviewStartResult(
        run_id=run_id,
        status=state.status.value,
        lifecycle=state.github_pr_review.lifecycle,
        pr_number=state.github_pr_review.pr_number or 0,
        already_running=False,
        message=(
            f"{'Respawned' if respawn_only else 'Started'} independent PR-review cycle "
            f"{run_id}; detached worker is polling for the Codex-bot review."
        ),
    )


def set_independent_cursor_model(run_id: str, *, cursor_model: str) -> SetCursorModelResult:
    if not cursor_model or not cursor_model.strip():
        raise UsageError("pr-review set-cursor-model requires --cursor-model <model>")
    new_model = cursor_model.strip()
    run_directory, state = load_run(run_id)
    gpr = state.github_pr_review
    if gpr is None or gpr.origin != "independent_pr":
        raise ValidationError(
            "set-cursor-model is only valid for independent_pr cycles before a Cursor chat exists"
        )

    locks = _run_locks(run_directory, run_id=run_id, repository_path=state.repository.root)
    locks.acquire()
    try:
        state = load_run_state_fresh(run_directory)
        gpr = state.github_pr_review
        assert gpr is not None
        if is_abort_requested(run_directory):
            raise ValidationError("abort requested; refusing cursor model change")
        if _worker_is_live(run_directory, run_id):
            raise ValidationError("refusing cursor model change while a PR-review worker is live")
        if not _cursor_model_mutable(state):
            raise ValidationError(
                "cursor model is immutable: require independent cycle in "
                "prepared_independent or awaiting_bot_review with no chat, "
                "no cursor iterations, and no active publication work"
            )
        if gpr.publication_phase is not None:
            raise ValidationError("refusing cursor model change during publication")

        previous = state.cursor.model
        if previous == new_model:
            return SetCursorModelResult(
                run_id=run_id,
                previous_model=previous,
                new_model=new_model,
                mutable=True,
                message=f"Cursor model already set to {new_model}",
            )

        cursor_version = probe_version(state.cursor.command, tool="cursor")
        compat = probe_cursor_model_compatibility(
            state.cursor.command,
            required_model=new_model,
            installed_version=cursor_version.version,
        )
        if compat.classification != CompatibilityClassification.COMPATIBLE:
            raise ValidationError(
                f"Cursor model probe failed ({compat.classification.value}); prior model retained"
            )

        artifact_path = run_directory / _MODEL_SELECTION_ARTIFACT
        history: list[dict[str, Any]] = []
        if artifact_path.is_file():
            try:
                existing = json.loads(artifact_path.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and isinstance(existing.get("selections"), list):
                    history = list(existing["selections"])
            except (OSError, json.JSONDecodeError, UnicodeError):
                history = []
        entry = {
            "previous_model": previous,
            "new_model": new_model,
            "source": "cli_set_cursor_model",
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "probe_classification": compat.classification.value,
            "probe_catalog_source": compat.catalog_source,
        }
        history.append(entry)
        atomic_write_json(
            artifact_path,
            {
                "schema_version": 1,
                "current_model": new_model,
                "effective_config_snapshot": "effective-config.yaml",
                "selections": history,
            },
            sensitive=True,
        )
        state.cursor = state.cursor.model_copy(update={"model": new_model})
        save_run_state(run_directory, state)
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="orchestrator",
            event="independent_cursor_model_selected",
            status=state.status.value,
            detail={
                "previous_model": previous,
                "new_model": new_model,
                "probe_classification": compat.classification.value,
                "artifact": _MODEL_SELECTION_ARTIFACT,
            },
        )
    finally:
        locks.release()

    return SetCursorModelResult(
        run_id=run_id,
        previous_model=previous,
        new_model=new_model,
        mutable=True,
        message=(
            f"Cursor model updated from {previous} to {new_model} "
            f"(runtime override; effective-config.yaml unchanged)"
        ),
    )


def render_independent_prepare_result(
    result: IndependentPrReviewPrepareResult, *, output: str = "text"
) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "origin": "independent_pr",
            "run_id": result.run_id,
            "project": result.project,
            "pr_number": result.pr_number,
            "status": result.status,
            "lifecycle": result.lifecycle,
            "start_command": result.start_command,
            "reused_existing": result.reused_existing,
            "controller_session_id_prefix": (
                result.controller_session_id[:8] if result.controller_session_id else None
            ),
            "requires_codex_exit": result.requires_codex_exit,
            "reviewer_must_remain_inactive": result.reviewer_must_remain_inactive,
            "bound_head_sha_prefix": result.bound_head_sha_prefix,
            "cursor_model": result.cursor_model,
            "cursor_model_mutable": result.cursor_model_mutable,
            "message": result.message,
            "has_model_family_warning": result.model_family_warning is not None,
            "has_model_mismatch_warning": result.model_mismatch_warning is not None,
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        result.message,
        f"Run: {result.run_id}",
        f"PR: #{result.pr_number}",
        f"Status: {result.status}",
        f"Lifecycle: {result.lifecycle}",
        f"Bound head: {result.bound_head_sha_prefix}…",
        f"Cursor model: {result.cursor_model} (mutable={result.cursor_model_mutable})",
        f"Next: {result.start_command}",
    ]
    if result.reviewer_must_remain_inactive:
        lines.append("Leave reviewer session B inactive; start from controller A only.")
    elif result.requires_codex_exit:
        lines.append("Exit/inactivate the reviewer Codex session before start.")
    return "\n".join(lines) + "\n"


def render_independent_start_result(
    result: IndependentPrReviewStartResult, *, output: str = "text"
) -> str:
    if output == "json":
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "origin": "independent_pr",
                    "run_id": result.run_id,
                    "status": result.status,
                    "lifecycle": result.lifecycle,
                    "pr_number": result.pr_number,
                    "already_running": result.already_running,
                    "message": result.message,
                },
                indent=2,
            )
            + "\n"
        )
    return (
        f"{result.message}\n"
        f"Run: {result.run_id}\n"
        f"PR: #{result.pr_number}\n"
        f"Status: {result.status}\n"
        f"Lifecycle: {result.lifecycle}\n"
    )


def render_set_cursor_model_result(result: SetCursorModelResult, *, output: str = "text") -> str:
    if output == "json":
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": result.run_id,
                    "previous_model": result.previous_model,
                    "new_model": result.new_model,
                    "mutable": result.mutable,
                    "message": result.message,
                },
                indent=2,
            )
            + "\n"
        )
    return (
        f"{result.message}\n"
        f"Run: {result.run_id}\n"
        f"Previous: {result.previous_model}\n"
        f"Current: {result.new_model}\n"
    )
