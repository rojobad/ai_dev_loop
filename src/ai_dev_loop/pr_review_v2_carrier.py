"""Filesystem subordinate-carrier runtime for PR review v2 local-fix effects.

Lives outside ``pr_review_v2`` so the package stays free of ``ai_dev_loop.state``
imports while production assembly still seeds real XDG RunState carriers.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.paths import DIR_MODE, ensure_dir, run_dir, set_sensitive_file_mode
from ai_dev_loop.pr_review_v2.application.write_contracts import DEFAULT_MAX_PATCH_BYTES
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import (
    CarrierSeed,
    TerminalCarrierAcceptance,
)
from ai_dev_loop.pr_review_v2.infrastructure.paths import resolve_run_relative_path
from ai_dev_loop.process import run_process
from ai_dev_loop.review_result import CodexReviewResult, completion_status_for_review
from ai_dev_loop.run_discovery import find_run_directory, list_run_directories, load_run
from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.state import (
    CodexState,
    CursorState,
    PlanState,
    ProjectRef,
    PromptState,
    RepositoryState,
    RunState,
    RunStatus,
    WorkflowState,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    save_run_state,
    sha256_bytes,
    utc_now,
)

_TERMINAL_ACCEPTED = frozenset({RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_RESIDUAL_RISK})
MAX_TERMINAL_REVIEW_RESULT_BYTES = 1_048_576
# Same bound as Phase 16.6 write-gateway patch consumption / protected persistence.
MAX_STAGED_PATCH_BYTES = DEFAULT_MAX_PATCH_BYTES
CARRIER_PROJECT = "prv2-carrier"
_CARRIER_FIX_PROMPT = Path("prompts") / "fixes" / "01.txt"
_CARRIER_PLAN = Path("plan") / "plan.md"
_CARRIER_PROMPT = Path("prompts") / "cursor-initial.txt"
_CARRIER_PLAN_REL = _CARRIER_PLAN.as_posix()
_CARRIER_PROMPT_REL = _CARRIER_PROMPT.as_posix()


class FilesystemLocalCarrierRuntime:
    """Create/reopen deterministic Phase 16.2 carriers under XDG runs/."""

    def carrier_exists(self, carrier_run_id: str) -> bool:
        try:
            path = find_run_directory(carrier_run_id)
        except ValidationError:
            return False
        return (path / "state.json").is_file()

    def carrier_has_progress(self, carrier_run_id: str) -> bool:
        if not self.carrier_exists(carrier_run_id):
            return False
        _path, state = load_run(carrier_run_id)
        if state.cursor.chat_id:
            return True
        if state.iterations:
            return True
        return state.status not in {RunStatus.PREPARED}

    def recovery_successor_id(self, carrier_run_id: str) -> str | None:
        """Return the unique verified generic-recovery successor, if one exists.

        A recovery successor is allowed to continue the local review checkpoint
        without replaying Cursor. Its lineage must bind to the failed carrier and
        to the exact staged patch artifact retained by that carrier.
        """

        if not self.carrier_exists(carrier_run_id):
            return None
        source_path, source = load_run(carrier_run_id)
        source_patch_sha256 = self._recovery_source_staged_patch_sha256(source_path, source)
        if source_patch_sha256 is None:
            return None
        matches: list[RunState] = []
        for _path, candidate in list_run_directories(project=CARRIER_PROJECT):
            recovery = candidate.recovery
            if recovery is None or recovery.source_run_id != carrier_run_id:
                continue
            if recovery.source_staged_patch_sha256 != source_patch_sha256:
                raise ValidationError("carrier recovery successor staged patch lineage mismatch")
            if (
                candidate.repository.root != source.repository.root
                or candidate.repository.branch != source.repository.branch
                or candidate.repository.initial_head != source.repository.initial_head
            ):
                raise ValidationError("carrier recovery successor repository binding mismatch")
            if candidate.status in {RunStatus.FAILED, RunStatus.ABORTED}:
                raise ValidationError("carrier recovery successor is terminal without acceptance")
            matches.append(candidate)

        if not matches:
            return None
        if len(matches) != 1:
            raise ValidationError("multiple carrier recovery successors found")
        return matches[0].run_id

    def ensure_seeded_carrier(self, *, carrier_run_id: str, seed: CarrierSeed) -> None:
        if self.carrier_exists(carrier_run_id):
            path, state = load_run(carrier_run_id)
            self.verify_carrier_bindings(carrier_run_id=carrier_run_id, seed=seed)
            if self.carrier_has_progress(carrier_run_id):
                return
            # Fresh prepared carrier without progress: refresh fix-prompt bytes only.
            self._write_fix_prompt(path, seed.fix_prompt_bytes)
            if seed.cursor_chat_id and state.cursor.chat_id is None:
                state.cursor.chat_id = seed.cursor_chat_id
                state.updated_at = utc_now()
                save_run_state(path, state)
            if state.cursor.chat_id:
                self._write_chat_artifact(path, state.cursor.chat_id, state.cursor.command)
            return

        repo_info = discover_repository(Path(seed.repository_root))
        if repo_info.head.lower() != seed.bound_head_sha.lower():
            raise ValidationError("carrier repository HEAD does not match effect binding")
        if repo_info.branch != seed.expected_branch:
            raise ValidationError("carrier repository branch does not match effect binding")
        destination = run_dir(CARRIER_PROJECT, carrier_run_id)
        ensure_dir(destination, mode=DIR_MODE)
        ensure_dir(destination / "plan", mode=DIR_MODE)
        ensure_dir(destination / "prompts", mode=DIR_MODE)
        ensure_dir(destination / "git", mode=DIR_MODE)
        ensure_dir(destination / "codex", mode=DIR_MODE)
        ensure_dir(destination / "cursor", mode=DIR_MODE)
        ensure_dir(destination / "logs", mode=DIR_MODE)

        # Seed only from hash-verified protected copies — never mutable repo content.
        plan_bytes = seed.plan_bytes
        if not plan_bytes or sha256_bytes(plan_bytes) != seed.plan_sha256:
            raise ValidationError("carrier plan hash mismatch")
        atomic_write_bytes(destination / _CARRIER_PLAN, plan_bytes, sensitive=True)

        prompt_bytes = seed.prompt_bytes
        if not prompt_bytes.strip() or sha256_bytes(prompt_bytes) != seed.prompt_sha256:
            raise ValidationError("carrier prompt hash mismatch")
        atomic_write_bytes(destination / _CARRIER_PROMPT, prompt_bytes, sensitive=True)
        self._write_fix_prompt(destination, seed.fix_prompt_bytes)
        atomic_write_text(
            destination / "git" / "baseline-status.txt",
            repo_info.status_porcelain + "\n",
        )

        now = utc_now()
        state = RunState(
            run_id=carrier_run_id,
            project=ProjectRef(name=CARRIER_PROJECT),
            status=RunStatus.PREPARED,
            created_at=now,
            updated_at=now,
            repository=RepositoryState(
                root=str(repo_info.root),
                git_common_dir=str(repo_info.git_common_dir),
                git_dir=str(repo_info.git_dir),
                branch=repo_info.branch,
                initial_head=repo_info.head,
                baseline_status_path="git/baseline-status.txt",
            ),
            plan=PlanState(
                repository_path=seed.plan_path,
                snapshot_path=_CARRIER_PLAN_REL,
                sha256=seed.plan_sha256,
            ),
            prompt=PromptState(
                source_repository_path=seed.prompt_path,
                snapshot_path=_CARRIER_PROMPT_REL,
                sha256=sha256_bytes(prompt_bytes),
            ),
            codex=CodexState(
                command=seed.codex_command,
                session_id=seed.codex_session_id,
                session_model=seed.review_model,
                session_reasoning_effort=seed.review_reasoning_effort,
                review_model=seed.review_model,
                review_reasoning_effort=seed.review_reasoning_effort,
                review_model_source="explicit",
                review_reasoning_source="explicit",
                review_skill=seed.review_skill,
                sandbox=seed.codex_sandbox,
            ),
            cursor=CursorState(
                command=seed.cursor_command,
                model=seed.cursor_model,
                output_format=seed.cursor_output_format,
                force=seed.cursor_force,
                trust_workspace=seed.cursor_trust_workspace,
                sandbox=seed.cursor_sandbox,
                chat_id=seed.cursor_chat_id,
            ),
            workflow=WorkflowState(
                max_review_iterations=max(1, seed.max_local_iterations),
                current_review_iteration=0,
                stage_mode="all",
                cursor_timeout_minutes=max(1, seed.cursor_timeout_minutes),
                codex_timeout_minutes=max(1, seed.codex_timeout_minutes),
            ),
        )
        save_run_state(destination, state)
        if state.cursor.chat_id:
            self._write_chat_artifact(destination, state.cursor.chat_id, state.cursor.command)

    @staticmethod
    def _write_chat_artifact(destination: Path, chat_id: str, command: str) -> None:
        atomic_write_json(
            destination / "cursor" / "chat.json",
            {
                "chat_id": chat_id,
                "created_at": utc_now().isoformat(),
                "command": command,
                "provenance": "inherited_carrier_seed",
            },
            sensitive=True,
        )

    def verify_carrier_bindings(self, *, carrier_run_id: str, seed: CarrierSeed) -> None:
        """Fail closed when a reopened carrier drifts from the frozen CarrierSeed."""

        if not self.carrier_exists(carrier_run_id):
            raise ValidationError("carrier missing for binding verification")
        path, state = load_run(carrier_run_id)

        if Path(state.repository.root).resolve() != Path(seed.repository_root).resolve():
            raise ValidationError("carrier repository root mismatch")
        if state.repository.initial_head.lower() != seed.bound_head_sha.lower():
            raise ValidationError("carrier initial HEAD mismatch")
        if state.repository.branch != seed.expected_branch:
            raise ValidationError("carrier stored branch mismatch")
        live_branch = self._current_branch(seed.repository_root)
        live_head = self.current_head_sha(seed.repository_root)
        if live_branch != seed.expected_branch:
            raise ValidationError("carrier live repository branch mismatch")
        if live_head.lower() != seed.bound_head_sha.lower():
            raise ValidationError("carrier live repository HEAD mismatch")

        if state.plan.repository_path != seed.plan_path or state.plan.sha256 != seed.plan_sha256:
            raise ValidationError("carrier plan binding mismatch")
        if (
            state.prompt.source_repository_path != seed.prompt_path
            or state.prompt.sha256 != seed.prompt_sha256
        ):
            raise ValidationError("carrier prompt binding mismatch")
        if state.plan.snapshot_path != _CARRIER_PLAN_REL:
            raise ValidationError("carrier plan snapshot_path mismatch")
        if state.prompt.snapshot_path != _CARRIER_PROMPT_REL:
            raise ValidationError("carrier prompt snapshot_path mismatch")
        self._resolve_carrier_relative(path, state.plan.snapshot_path, label="plan snapshot")
        self._resolve_carrier_relative(path, state.prompt.snapshot_path, label="prompt snapshot")
        self._verify_bytes_file(path / _CARRIER_PLAN, seed.plan_bytes, seed.plan_sha256, "plan")
        self._verify_bytes_file(
            path / _CARRIER_PROMPT, seed.prompt_bytes, seed.prompt_sha256, "prompt"
        )
        fix_digest = sha256_bytes(seed.fix_prompt_bytes)
        self._verify_bytes_file(
            path / _CARRIER_FIX_PROMPT, seed.fix_prompt_bytes, fix_digest, "fix prompt"
        )

        # Expected chat when seed carries one; allow null→created on ExistingPr first cycle.
        if seed.cursor_chat_id is not None and state.cursor.chat_id != seed.cursor_chat_id:
            raise ValidationError("carrier Cursor chat identity mismatch")

        if state.cursor.command != seed.cursor_command:
            raise ValidationError("carrier Cursor command mismatch")
        if state.cursor.model != seed.cursor_model:
            raise ValidationError("carrier Cursor model mismatch")
        if state.cursor.output_format != seed.cursor_output_format:
            raise ValidationError("carrier Cursor output_format mismatch")
        if state.cursor.force != seed.cursor_force:
            raise ValidationError("carrier Cursor force mismatch")
        if state.cursor.trust_workspace != seed.cursor_trust_workspace:
            raise ValidationError("carrier Cursor trust_workspace mismatch")
        if state.cursor.sandbox != seed.cursor_sandbox:
            raise ValidationError("carrier Cursor sandbox mismatch")

        if state.codex.session_id != seed.codex_session_id:
            raise ValidationError("carrier Codex session identity mismatch")
        if state.codex.command != seed.codex_command:
            raise ValidationError("carrier Codex command mismatch")
        if state.codex.review_model != seed.review_model:
            raise ValidationError("carrier Codex review_model mismatch")
        if state.codex.review_reasoning_effort != seed.review_reasoning_effort:
            raise ValidationError("carrier Codex review_reasoning_effort mismatch")
        if state.codex.review_skill != seed.review_skill:
            raise ValidationError("carrier Codex review_skill mismatch")
        if state.codex.sandbox != seed.codex_sandbox:
            raise ValidationError("carrier Codex sandbox mismatch")

        if state.workflow.max_review_iterations != max(1, seed.max_local_iterations):
            raise ValidationError("carrier workflow max_local_iterations mismatch")
        if state.workflow.cursor_timeout_minutes != max(1, seed.cursor_timeout_minutes):
            raise ValidationError("carrier workflow cursor_timeout_minutes mismatch")
        if state.workflow.codex_timeout_minutes != max(1, seed.codex_timeout_minutes):
            raise ValidationError("carrier workflow codex_timeout_minutes mismatch")

    def current_head_sha(self, repo_root: str) -> str:
        result = run_process(["git", "rev-parse", "HEAD"], cwd=repo_root, timeout=30)
        if result.returncode != 0 or not result.stdout.strip():
            raise ValidationError("failed to resolve carrier repository HEAD")
        return result.stdout.strip().lower()

    def read_verified_staged_patch_bytes(self, carrier_run_id: str, relative_path: str) -> bytes:
        """Read accepted staged patch with the same checks as terminal replay."""

        if not self.carrier_exists(carrier_run_id):
            raise ValidationError("carrier missing for staged patch verification")
        path, state = load_run(carrier_run_id)
        recorded = self._recorded_staged_patch_sha256(state, relative_path)
        if recorded is None:
            raise ValidationError("carrier missing staged patch hash")
        return self._read_verified_staged_patch(
            carrier_root=path,
            relative_path=relative_path,
            expected_sha256=recorded,
        )

    def read_terminal_acceptance(
        self, carrier_run_id: str, *, expected_session_id: str
    ) -> TerminalCarrierAcceptance | None:
        """Return hash-verified terminal evidence, or None when not terminal."""

        if not self.carrier_exists(carrier_run_id):
            return None
        path, state = load_run(carrier_run_id)
        if state.status not in _TERMINAL_ACCEPTED:
            return None
        if state.codex.session_id != expected_session_id:
            raise ValidationError("terminal carrier Codex session identity mismatch")
        if not state.cursor.chat_id:
            raise ValidationError("terminal carrier missing Cursor chat id")
        if not state.iterations:
            raise ValidationError("terminal carrier missing iteration evidence")
        latest = max(state.iterations, key=lambda entry: int(entry.get("number", 0)))
        git_section = latest.get("git")
        if not isinstance(git_section, dict):
            raise ValidationError("terminal carrier missing git iteration evidence")
        staged_rel = git_section.get("staged_diff_path")
        recorded_patch_sha = git_section.get("staged_diff_sha256")
        if not isinstance(staged_rel, str) or not staged_rel:
            raise ValidationError("terminal carrier missing staged patch path")
        if not isinstance(recorded_patch_sha, str) or not recorded_patch_sha:
            raise ValidationError("terminal carrier missing staged patch hash")
        patch_bytes = self._read_verified_staged_patch(
            carrier_root=path,
            relative_path=staged_rel,
            expected_sha256=recorded_patch_sha,
        )

        codex_section = latest.get("codex")
        if not isinstance(codex_section, dict):
            raise ValidationError("terminal carrier missing codex iteration evidence")
        result_rel = codex_section.get("result_path")
        recorded_sha = codex_section.get("result_sha256")
        if not isinstance(result_rel, str) or not result_rel:
            raise ValidationError("terminal carrier missing review result path")
        if not isinstance(recorded_sha, str) or not recorded_sha:
            raise ValidationError("terminal carrier missing review result hash")
        result_path = self._resolve_carrier_relative(path, result_rel, label="review result")
        raw = self._read_bounded_owner_protected_file(
            result_path,
            max_bytes=MAX_TERMINAL_REVIEW_RESULT_BYTES,
            label="review result",
            require_owner_protected=False,
        )
        digest = sha256_bytes(raw)
        if digest != recorded_sha:
            raise ValidationError("terminal carrier review result hash mismatch")
        try:
            payload = json.loads(raw.decode("utf-8"))
            review = CodexReviewResult.model_validate(payload)
        except (UnicodeError, json.JSONDecodeError, PydanticValidationError, ValueError) as exc:
            raise ValidationError("terminal carrier review result failed canonical parse") from exc
        if review.has_actionable_findings:
            raise ValidationError("terminal carrier review still has actionable findings")
        expected_status = completion_status_for_review(review)
        if expected_status == "completed_with_residual_risk":
            if state.status is not RunStatus.COMPLETED_WITH_RESIDUAL_RISK:
                raise ValidationError("terminal carrier residual-risk status mismatch")
            residual_risk = True
        elif expected_status == "completed":
            if state.status is not RunStatus.COMPLETED:
                raise ValidationError("terminal carrier accepted status mismatch")
            residual_risk = False
        else:
            raise ValidationError("terminal carrier review outcome is not an accepted terminal")

        iteration_count = int(latest.get("number", 1) or 1)
        return TerminalCarrierAcceptance(
            status=state.status.value,
            chat_id=state.cursor.chat_id,
            codex_session_id=state.codex.session_id,
            iteration_count=max(1, iteration_count),
            staged_diff_path=staged_rel,
            patch_bytes=patch_bytes,
            residual_risk=residual_risk,
        )

    def _staged_patch_sha256(self, carrier_root: Path, state: RunState) -> str:
        patch_sha256 = self._recovery_source_staged_patch_sha256(carrier_root, state)
        if patch_sha256 is None:
            if not state.iterations:
                raise ValidationError("carrier recovery source missing iteration evidence")
            raise ValidationError("carrier recovery source missing git iteration evidence")
        return patch_sha256

    def _recovery_source_staged_patch_sha256(
        self, carrier_root: Path, state: RunState
    ) -> str | None:
        """Return staged-patch hash for recovery lineage lookup, or None when not yet durable."""

        if not state.iterations:
            return None
        latest = max(state.iterations, key=lambda entry: int(entry.get("number", 0)))
        git_section = latest.get("git")
        if not isinstance(git_section, dict):
            return None
        staged_rel = git_section.get("staged_diff_path")
        if not isinstance(staged_rel, str) or not staged_rel:
            return None
        patch_path = self._resolve_carrier_relative(
            carrier_root, staged_rel, label="recovery source staged patch"
        )
        patch_bytes = self._read_bounded_owner_protected_file(
            patch_path,
            max_bytes=MAX_STAGED_PATCH_BYTES,
            label="recovery source staged patch",
            require_owner_protected=True,
        )
        if not patch_bytes:
            raise ValidationError("carrier recovery source staged patch empty")
        actual = sha256_bytes(patch_bytes)
        recorded = git_section.get("staged_diff_sha256")
        if isinstance(recorded, str) and recorded and actual != recorded:
            raise ValidationError("carrier recovery source staged patch hash mismatch")
        return actual

    def _recorded_staged_patch_sha256(self, state: RunState, relative_path: str) -> str | None:
        if not state.iterations:
            return None
        latest = max(state.iterations, key=lambda entry: int(entry.get("number", 0)))
        git_section = latest.get("git")
        if not isinstance(git_section, dict):
            return None
        staged_rel = git_section.get("staged_diff_path")
        recorded = git_section.get("staged_diff_sha256")
        if staged_rel != relative_path:
            return None
        if not isinstance(recorded, str) or not recorded:
            return None
        return recorded

    def _read_verified_staged_patch(
        self,
        *,
        carrier_root: Path,
        relative_path: str,
        expected_sha256: str,
    ) -> bytes:
        patch_path = self._resolve_carrier_relative(
            carrier_root, relative_path, label="staged patch"
        )
        patch_bytes = self._read_bounded_owner_protected_file(
            patch_path,
            max_bytes=MAX_STAGED_PATCH_BYTES,
            label="staged patch",
            require_owner_protected=True,
        )
        if not patch_bytes:
            raise ValidationError("carrier staged patch empty")
        if sha256_bytes(patch_bytes) != expected_sha256:
            raise ValidationError("carrier staged patch hash mismatch")
        return patch_bytes

    def _current_branch(self, repo_root: str) -> str:
        result = run_process(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, timeout=30
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise ValidationError("failed to resolve carrier repository branch")
        branch = result.stdout.strip()
        if branch in {"HEAD", ""}:
            raise ValidationError("carrier repository is in detached HEAD")
        return branch

    def _resolve_carrier_relative(
        self, carrier_root: Path, relative_path: str, *, label: str
    ) -> Path:
        try:
            return resolve_run_relative_path(carrier_root, relative_path)
        except ValueError as exc:
            raise ValidationError(f"terminal carrier {label} path is unsafe") from exc

    def _read_bounded_owner_protected_file(
        self,
        path: Path,
        *,
        max_bytes: int,
        label: str,
        require_owner_protected: bool,
    ) -> bytes:
        if path.is_symlink():
            raise ValidationError(f"terminal carrier {label} must not be a symlink")
        if not path.is_file():
            raise ValidationError(f"terminal carrier {label} missing")
        try:
            st = path.stat()
        except OSError as exc:
            raise ValidationError(f"terminal carrier {label} unreadable") from exc
        if st.st_size > max_bytes:
            raise ValidationError(f"terminal carrier {label} exceeds size bound")
        if require_owner_protected:
            mode = stat.S_IMODE(st.st_mode)
            if mode & 0o077:
                raise ValidationError(f"terminal carrier {label} is not owner-protected")
            if (mode & 0o700) != 0o600:
                raise ValidationError(f"terminal carrier {label} is not owner-protected")
        raw = path.read_bytes()
        if len(raw) > max_bytes:
            raise ValidationError(f"terminal carrier {label} exceeds size bound")
        return raw

    def _verify_bytes_file(
        self, path: Path, expected: bytes, expected_sha256: str, label: str
    ) -> None:
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"carrier {label} artifact missing")
        raw = path.read_bytes()
        if raw != expected or sha256_bytes(raw) != expected_sha256:
            raise ValidationError(f"carrier {label} bytes/hash drift")

    def _write_fix_prompt(self, destination: Path, fix_prompt_bytes: bytes) -> None:
        target = destination / _CARRIER_FIX_PROMPT
        ensure_dir(target.parent, mode=DIR_MODE)
        atomic_write_bytes(target, fix_prompt_bytes, sensitive=True)
        set_sensitive_file_mode(target)


__all__ = [
    "CARRIER_PROJECT",
    "FilesystemLocalCarrierRuntime",
    "MAX_STAGED_PATCH_BYTES",
    "MAX_TERMINAL_REVIEW_RESULT_BYTES",
]
