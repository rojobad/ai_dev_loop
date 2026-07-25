"""Production-assembled existing-PR happy-path harness for Phase 16.8 Gate A.

Assembles the real v2 store, SQLite engine, EffectWorker/router, LOCAL/MUTATING/
READ/RECONCILE executors, Git/GitHub gateways, and control/supervisor boundary
against a temporary Git worktree + bare remote and a stateful fake ``gh``.
Observation evidence (eligible thread, then exact-trigger +1) is scripted by
updating fake-gh durable state after production mutations — reducer outcomes are
never forced.

Codex adjudication/publication use production ``ProcessCodexRunner`` against a
deterministic local fake ``codex`` executable. ``ControlPlaneService.start``
launches a controlled temporary supervisor subprocess through the same launcher
metadata / ownership boundary as production.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tests.integration.stateful_fake_gh import StatefulFakeGhController
from tests.unit.pr_review_v2.durable_helpers import FakeClock

from ai_dev_loop.launcher import read_process_starttime
from ai_dev_loop.paths import DIR_MODE, ensure_dir, set_sensitive_file_mode
from ai_dev_loop.pr_review_v2.application.control import (
    ControlPlaneService,
    EngineOriginContextResolver,
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
from ai_dev_loop.pr_review_v2.application.github_read import GitHubReadPolicy
from ai_dev_loop.pr_review_v2.application.preparation import PreparationService
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    GitHubWritePolicy,
    GitRemoteScheme,
    GitWritePolicy,
    append_owned_marker,
    canonicalize_publication_text,
    derive_content_bound_marker,
    html_comment_marker,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    EffectCompletionToken,
    PullRequestBinding,
    RepositoryIdentity,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    PrReviewEffect,
    PushCommitEffect,
    RequestBotReviewEffect,
)
from ai_dev_loop.pr_review_v2.domain.events import EffectSucceeded, PushConfirmedOutcome
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    PublicationTextRunner,
    ThreadAdjudicationRunner,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import (
    GhApiTransport,
    build_minimal_gh_env,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_write_transport import GhWriteTransport
from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import GitPublicationGateway
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitWriteTransport
from ai_dev_loop.pr_review_v2.infrastructure.github_read_gateway import GitHubReadGateway
from ai_dev_loop.pr_review_v2.infrastructure.github_write_gateway import GitHubWriteGateway
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import LocalFixAdapter
from ai_dev_loop.pr_review_v2.infrastructure.paths import ensure_run_artifact_root
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import ReviewArtifactStore
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory, SystemClock
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.infrastructure.write_evidence_artifacts import WriteEvidenceStore
from ai_dev_loop.pr_review_v2.runtime_factory import ProcessCodexRunner, build_minimal_codex_env
from ai_dev_loop.pr_review_v2.workers.effect_executor_router import EffectExecutorRouter
from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker
from ai_dev_loop.pr_review_v2.workers.github_read_executor import GitHubReadExecutor
from ai_dev_loop.pr_review_v2.workers.local_executor import LocalEffectExecutor
from ai_dev_loop.pr_review_v2.workers.owned_children import OwnedChildStore
from ai_dev_loop.pr_review_v2.workers.reconcile_write_executor import ReconcileWriteExecutor
from ai_dev_loop.pr_review_v2.workers.supervisor import (
    SupervisorLauncherMetadata,
    SupervisorLauncherStore,
)
from ai_dev_loop.pr_review_v2.workers.write_executor import WriteExecutor
from ai_dev_loop.pr_review_v2_carrier import FilesystemLocalCarrierRuntime
from ai_dev_loop.state import sha256_bytes

PLAN_BYTES = b"frozen-plan-for-existing-pr-happy-path\n"
PROMPT_BYTES = b"frozen-prompt-for-existing-pr-happy-path\n"
PLAN_SHA = hashlib.sha256(PLAN_BYTES).hexdigest()
PROMPT_SHA = hashlib.sha256(PROMPT_BYTES).hexdigest()
SESSION = "11111111-1111-1111-1111-111111111111"
THREAD_ID = "PRRT_happy_1"
T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
OWNER = "acme"
NAME = "demo"
PR_NUMBER = 42
REVIEWER = "chatgpt-codex-connector"

WORKER_SCRIPT = Path(__file__).resolve().parent / "phase16_8_existing_pr_happy_path_worker.py"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "p168-happy",
    "GIT_AUTHOR_EMAIL": "p168-happy@example.com",
    "GIT_COMMITTER_NAME": "p168-happy",
    "GIT_COMMITTER_EMAIL": "p168-happy@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}

ADJUDICATION_PAYLOAD = {
    "decisions": [
        {
            "thread_id": THREAD_ID,
            "decision": "actionable",
            "safe_summary": "needs a null check fix",
            "reply_body": None,
        }
    ],
    "fix_prompt_text": "Please fix the null check on app.py",
}
PUBLICATION_PAYLOAD = {
    "title": "Fix null check",
    "body": "Address bot feedback on app.py",
    "commit_subject": "fix: null check",
    "commit_body": "Address review thread feedback.",
}


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(_GIT_ENV)
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def init_existing_pr_git(tmp_path: Path) -> tuple[Path, Path, str]:
    """Disposable worktree + bare remote on ``feature``, with plan/prompt files."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=feature")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "--initial-branch=feature")
    _git(work, "config", "user.name", "p168-happy")
    _git(work, "config", "user.email", "p168-happy@example.com")
    (work / "app.py").write_text("x = 1\n", encoding="utf-8")
    (work / "ai_dev_loop.yaml").write_text("project: demo\n", encoding="utf-8")
    (work / "plans").mkdir()
    (work / "plans" / "x.md").write_bytes(PLAN_BYTES)
    (work / "prompts").mkdir()
    (work / "prompts" / "prompt.txt").write_bytes(PROMPT_BYTES)
    _git(work, "add", "app.py", "ai_dev_loop.yaml", "plans", "prompts")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", "feature")
    head = _git(work, "rev-parse", "HEAD").lower()
    return work, remote, head


def seed_owned_pr_preimage(
    *, title: str = "Existing feature", body: str = "Adopted PR body"
) -> tuple[str, str]:
    """Return title/body carrying an intact v2 owned preimage (update_pr_text prerequisite)."""

    marker = derive_content_bound_marker(
        operation="create_or_update_pr",
        target_kind="pr_body",
        idempotency_key="seed-existing-pr-preimage",
        canonical_content=canonicalize_publication_text(title=title, body=body),
    )
    return title, append_owned_marker(body, marker.marker_text)


def build_execution_context(
    *,
    repo_root: str,
    head_sha: str,
    gh_command: str,
) -> ExecutionContextArtifact:
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from="existing_pr",
            source_run_id=None,
            repository=f"{OWNER}/{NAME}",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=head_sha,
        ),
        cursor=ExecutionContextCursor(
            chat_id=None,
            model="composer-2.5-fast",
            command="agent",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        codex=ExecutionContextCodex(
            session_id=SESSION,
            review_model="gpt-5.5",
            review_reasoning_effort="medium",
            command="codex",
            sandbox="workspace-write",
            review_skill="review-staged-cursor-execution",
            external_review_skill="review-github-pr-feedback",
        ),
        workflow=ExecutionContextWorkflow(
            max_local_iterations=3,
            cursor_timeout_minutes=30,
            codex_timeout_minutes=30,
        ),
        pr_review_v2=ExecutionContextPrReviewV2(
            gh_command=gh_command,
            git_command="git",
            ssh_command="ssh",
            remote_name="origin",
            reviewer_logins=(REVIEWER,),
            review_trigger_body="@codex review",
            user_mention="rojobad",
            poll_interval_seconds=60,
            max_external_cycles=8,
            per_call_timeout_seconds=60,
            overall_timeout_seconds=180,
            max_pages=20,
            max_items=500,
            max_server_directed_wait_seconds=3600,
            no_findings_enabled=True,
            no_findings_prefixes=(),
            accept_bot_thumbs_up=True,
            no_findings_prefix_length=12,
            worker=ExecutionContextWorker(
                lease_ttl_seconds=60,
                heartbeat_interval_seconds=10,
                idle_poll_seconds=1,
            ),
        ),
        plan_prompt=ExecutionContextPlanPrompt(
            plan_path="plans/x.md",
            plan_sha256=PLAN_SHA,
            prompt_path="prompts/prompt.txt",
            prompt_sha256=PROMPT_SHA,
        ),
        repository_root=repo_root,
    )


@dataclass
class EffectLedger:
    kinds: list[str] = field(default_factory=list)
    succeeded_kinds: list[str] = field(default_factory=list)
    idempotency_keys: list[str] = field(default_factory=list)
    effect_ids: list[str] = field(default_factory=list)
    push_force_flags: list[bool] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "kinds": list(self.kinds),
            "succeeded_kinds": list(self.succeeded_kinds),
            "idempotency_keys": list(self.idempotency_keys),
            "effect_ids": list(self.effect_ids),
            "push_force_flags": list(self.push_force_flags),
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(self.to_payload(), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> EffectLedger:
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            kinds=list(raw.get("kinds") or []),
            succeeded_kinds=list(raw.get("succeeded_kinds") or []),
            idempotency_keys=list(raw.get("idempotency_keys") or []),
            effect_ids=list(raw.get("effect_ids") or []),
            push_force_flags=[bool(v) for v in (raw.get("push_force_flags") or [])],
        )


@dataclass
class CodexInvocationLog:
    """Reads argv/stdin records written by the local fake Codex executable."""

    log_path: Path

    @property
    def all_argv(self) -> list[list[str]]:
        if not self.log_path.is_file():
            return []
        out: list[list[str]] = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            out.append(list(payload["argv"]))
        return out


class BoundProcessCodexRunner:
    """ProcessCodexRunner that binds the durable run_id before the first Codex call."""

    def __init__(self, *, artifact_root: Path, env: dict[str, str] | None = None) -> None:
        self._artifact_root = artifact_root
        self._env = env
        self._run_id = "unbound"

    def bind(self, run_id: str) -> None:
        self._run_id = run_id

    def run(
        self,
        argv: list[str],
        *,
        cwd: str,
        stdin_text: str,
        timeout_seconds: float,
    ):
        return ProcessCodexRunner(
            artifact_root=self._artifact_root,
            run_id=self._run_id,
            env=self._env,
        ).run(
            argv,
            cwd=cwd,
            stdin_text=stdin_text,
            timeout_seconds=timeout_seconds,
        )


def install_happy_path_codex(
    bin_dir: Path,
    *,
    payloads_dir: Path,
    log_path: Path,
    counter_path: Path,
    delegate_codex: Path | None = None,
) -> Path:
    """Install a deterministic local fake ``codex`` that writes ``--output-last-message``.

    Adjudication/publication schemas consume sequential payloads. All other argv
    shapes (carrier local review) optionally delegate to the suite fake ``codex``.
    """

    bin_dir.mkdir(parents=True, exist_ok=True)
    payloads_dir.mkdir(parents=True, exist_ok=True)
    (payloads_dir / "00.json").write_text(
        json.dumps(ADJUDICATION_PAYLOAD, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (payloads_dir / "01.json").write_text(
        json.dumps(PUBLICATION_PAYLOAD, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    counter_path.write_text("0\n", encoding="utf-8")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        log_path.unlink()
    delegate_literal = "None" if delegate_codex is None else repr(str(delegate_codex.resolve()))
    script = bin_dir / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"payloads_dir = {str(payloads_dir.resolve())!r}\n"
        f"log_path = {str(log_path.resolve())!r}\n"
        f"counter_path = {str(counter_path.resolve())!r}\n"
        f"delegate = {delegate_literal}\n"
        "argv = list(sys.argv)\n"
        "joined = ' '.join(argv)\n"
        "is_v2 = (\n"
        "    'pr-review-v2-external-adjudication' in joined\n"
        "    or 'pr-review-v2-publication-generation' in joined\n"
        ")\n"
        "if not is_v2:\n"
        "    if delegate is None:\n"
        "        print('happy-path codex: no delegate for non-v2 argv', file=sys.stderr)\n"
        "        raise SystemExit(2)\n"
        "    os.execv(delegate, [delegate, *argv[1:]])\n"
        "stdin_text = sys.stdin.read()\n"
        "with open(counter_path, 'r+', encoding='utf-8') as handle:\n"
        "    raw = handle.read().strip() or '0'\n"
        "    index = int(raw)\n"
        "    handle.seek(0)\n"
        "    handle.write(str(index + 1) + '\\n')\n"
        "    handle.truncate()\n"
        "payload_path = os.path.join(payloads_dir, f'{index:02d}.json')\n"
        "if not os.path.isfile(payload_path):\n"
        "    print(f'no payload for index {index}', file=sys.stderr)\n"
        "    raise SystemExit(2)\n"
        "with open(payload_path, encoding='utf-8') as handle:\n"
        "    payload = handle.read()\n"
        "if '--output-last-message' not in argv:\n"
        "    print('missing --output-last-message', file=sys.stderr)\n"
        "    raise SystemExit(2)\n"
        "out = argv[argv.index('--output-last-message') + 1]\n"
        "parent = os.path.dirname(out)\n"
        "if parent:\n"
        "    os.makedirs(parent, exist_ok=True)\n"
        "with open(out, 'w', encoding='utf-8') as handle:\n"
        "    handle.write(payload)\n"
        "os.chmod(out, 0o600)\n"
        "record = {'argv': argv, 'stdin_len': len(stdin_text), 'index': index}\n"
        "with open(log_path, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(record, ensure_ascii=False) + '\\n')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


class ObservationScriptingRouter:
    """Delegates to the production router; scripts fake-gh evidence after mutations."""

    requires_authority = True

    def __init__(
        self,
        inner: EffectExecutorRouter,
        *,
        controller: StatefulFakeGhController,
        ledger: EffectLedger,
        ledger_path: Path,
        thread_id: str = THREAD_ID,
    ) -> None:
        self._inner = inner
        self._controller = controller
        self._ledger = ledger
        self._ledger_path = ledger_path
        self._thread_id = thread_id

    def execute(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
        authority: Any = None,
        claim: Any = None,
    ):
        self._ledger.kinds.append(effect.kind)
        self._ledger.idempotency_keys.append(effect.idempotency_key)
        self._ledger.effect_ids.append(effect.effect_id)
        if isinstance(effect, PushCommitEffect):
            self._ledger.push_force_flags.append(bool(effect.force))
        result = self._inner.execute(
            effect, token, now=now, authority=authority, claim=claim
        )
        if isinstance(result, EffectSucceeded):
            self._ledger.succeeded_kinds.append(effect.kind)
            self._after_success(effect, result)
        self._ledger.save(self._ledger_path)
        return result

    def _after_success(self, effect: PrReviewEffect, result: EffectSucceeded) -> None:
        if isinstance(effect, RequestBotReviewEffect):
            if effect.cycle_number == 1:
                self._seed_actionable_thread(effect)
            elif effect.cycle_number == 2:
                self._seed_exact_trigger_thumbs_up(effect)
            return
        if isinstance(effect, PushCommitEffect) and isinstance(
            result.outcome, PushConfirmedOutcome
        ):
            state = self._controller.reload()
            state.fixture["head_sha"] = result.outcome.commit_sha.lower()
            state.save(self._controller.state_path)

    def _seed_actionable_thread(self, effect: RequestBotReviewEffect) -> None:
        state = self._controller.reload()
        head = effect.bound_head_sha.lower()
        state.fixture["head_sha"] = head
        state.fixture["marker"] = effect.marker
        state.fixture["default_thread_id"] = self._thread_id
        state.fixture["threads"] = [
            {
                "id": self._thread_id,
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "id": "PRRC_happy_1",
                            "body": "Please fix the null check on app.py",
                            "createdAt": "2026-07-21T12:06:00Z",
                            "author": {"login": REVIEWER},
                            "commit": {"oid": head},
                            "path": "app.py",
                            "line": 1,
                            "pullRequestReview": {"id": "PRR_happy_1"},
                        }
                    ]
                },
            }
        ]
        state.save(self._controller.state_path)

    def _seed_exact_trigger_thumbs_up(self, effect: RequestBotReviewEffect) -> None:
        state = self._controller.reload()
        state.fixture["head_sha"] = effect.bound_head_sha.lower()
        state.fixture["marker"] = effect.marker
        for node in state.fixture.get("threads") or []:
            if isinstance(node, dict):
                node["isResolved"] = True
        if self._thread_id not in state.resolved_threads:
            state.resolved_threads.append(self._thread_id)
        needle = html_comment_marker(effect.marker)
        comment_id: int | None = None
        for comment in state.issue_comments:
            body = str(comment.get("body") or "")
            if needle in body:
                comment_id = int(comment.get("databaseId") or comment.get("id") or 0)
                break
        if comment_id is None:
            raise AssertionError("cycle-2 trigger comment missing from fake gh state")
        state.fixture.setdefault("reactions", {})[str(comment_id)] = [
            {
                "id": 9001,
                "user": {"login": REVIEWER},
                "content": "+1",
                "created_at": "2026-07-21T12:20:00Z",
            }
        ]
        state.save(self._controller.state_path)


class _ReusableIntervalWait:
    """Stoppable interval wait safe to reuse across sequential claim renewals."""

    def __init__(self) -> None:
        self._ev = threading.Event()

    def wait(self, timeout: float) -> bool:
        triggered = self._ev.wait(timeout=timeout)
        if triggered:
            self._ev.clear()
        return triggered

    def wake_for_stop(self) -> None:
        self._ev.set()


@dataclass
class SupervisorProcessRegistry:
    """Track controlled supervisor children so tests always reap them."""

    procs: dict[str, subprocess.Popen[bytes]] = field(default_factory=dict)

    def register(self, run_id: str, proc: subprocess.Popen[bytes]) -> None:
        self.procs[run_id] = proc

    def reap(self, run_id: str, *, timeout_seconds: float = 5.0) -> int | None:
        proc = self.procs.get(run_id)
        if proc is None:
            return None
        if proc.poll() is None:
            try:
                pgid = os.getpgid(proc.pid)
            except (OSError, ProcessLookupError):
                pgid = None
            if pgid is not None:
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(pgid, signal.SIGTERM)
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline and proc.poll() is None:
                    time.sleep(0.05)
                if proc.poll() is None:
                    with contextlib.suppress(OSError, ProcessLookupError):
                        os.killpg(pgid, signal.SIGKILL)
            else:
                with contextlib.suppress(OSError, ProcessLookupError):
                    proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=2)
        return proc.returncode


def _terminate_and_reap(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        try:
            pgid = os.getpgid(proc.pid)
        except (OSError, ProcessLookupError):
            pgid = None
        if pgid is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pgid, sig)
                except (OSError, ProcessLookupError):
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
        else:
            with contextlib.suppress(OSError, ProcessLookupError):
                proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=1)


def _read_process_executable(pid: int) -> str:
    try:
        return str(Path(f"/proc/{pid}/exe").resolve())
    except OSError:
        return str(Path(sys.executable).resolve())


def spawn_happy_path_supervisor(
    run_id: str,
    *,
    artifact_root: Path,
    launcher_store: SupervisorLauncherStore,
    config_path: Path,
    registry: SupervisorProcessRegistry,
    child_env: dict[str, str],
) -> str:
    """Spawn a controlled supervisor using production launcher metadata ownership."""

    existing = launcher_store.read(run_id)
    if existing is not None:
        launcher_store.clear(run_id)

    token = secrets.token_hex(16)
    run_root = ensure_run_artifact_root(artifact_root, run_id)
    log_dir = ensure_dir(run_root / "logs", mode=DIR_MODE)
    stdout_path = log_dir / "supervisor.stdout.txt"
    stderr_path = log_dir / "supervisor.stderr.txt"
    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")
    set_sensitive_file_mode(stdout_path)
    set_sensitive_file_mode(stderr_path)
    env = dict(child_env)
    env["HAPPY_PATH_STACK_CONFIG"] = str(config_path.resolve())
    try:
        proc = subprocess.Popen(
            [sys.executable, str(WORKER_SCRIPT), run_id, token],
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            cwd=str(run_root),
            env=env,
        )
    except OSError:
        return "spawn_failed"
    finally:
        stdout_handle.close()
        stderr_handle.close()

    registry.register(run_id, proc)
    try:
        if proc.pid <= 0:
            raise RuntimeError("invalid supervisor pid")
        try:
            pgid = os.getpgid(proc.pid)
        except (OSError, ProcessLookupError) as exc:
            raise RuntimeError("failed to resolve supervisor pgid") from exc
        starttime = read_process_starttime(proc.pid)
        if starttime is None:
            raise RuntimeError("failed to capture supervisor process start time")
        executable = _read_process_executable(proc.pid)
        metadata = SupervisorLauncherMetadata(
            schema_version=1,
            run_id=run_id,
            token=token,
            pid=proc.pid,
            pgid=pgid,
            process_start_time=str(starttime),
            executable=executable,
            created_at=SystemClock().now().isoformat(),
        )
        launcher_store.write(metadata)
    except Exception:
        _terminate_and_reap(proc)
        with contextlib.suppress(Exception):
            launcher_store.clear(run_id)
        return "spawn_failed"
    return "spawned"


def wait_for_supervisor_completed(
    *,
    engine: PrReviewEngine,
    run_id: str,
    registry: SupervisorProcessRegistry,
    launchers: SupervisorLauncherStore,
    timeout_seconds: float = 180.0,
) -> str:
    """Poll until durable completed or the controlled supervisor exits; always reap."""

    deadline = time.monotonic() + timeout_seconds
    proc = registry.procs.get(run_id)
    last_kind = "unknown"
    exit_code: int | None = None

    def _status_kind() -> str:
        # Concurrent supervisor WAL/SHM churn can briefly race chmod on sidecars.
        for _ in range(20):
            try:
                return engine.get_status(run_id).state_kind
            except FileNotFoundError:
                time.sleep(0.05)
        return engine.get_status(run_id).state_kind

    try:
        while time.monotonic() < deadline:
            last_kind = _status_kind()
            if last_kind == "completed":
                if proc is not None and proc.poll() is None:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=5)
                break
            if proc is not None and proc.poll() is not None:
                exit_code = proc.returncode
                break
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"timed out waiting for completed; last_state={last_kind!r}"
            )
    finally:
        code = registry.reap(run_id)
        if code is not None:
            exit_code = code
        last_kind = _status_kind()
        if last_kind != "completed":
            raise AssertionError(
                f"supervisor did not reach completed; state={last_kind!r} exit={exit_code!r}"
            )
        if exit_code not in {0, None}:
            raise AssertionError(f"supervisor exited uncleanly with code {exit_code}")
        settle_deadline = time.monotonic() + 2.0
        while time.monotonic() < settle_deadline and launchers.read(run_id) is not None:
            time.sleep(0.05)
    return last_kind


@dataclass
class ExistingPrHappyPathStack:
    clock: FakeClock
    engine: PrReviewEngine
    artifacts: ProtectedResultStore
    prep: PreparationService
    control: ControlPlaneService
    worker: EffectWorker
    launchers: SupervisorLauncherStore
    supervisor_registry: SupervisorProcessRegistry
    controller: StatefulFakeGhController
    fake_codex: CodexInvocationLog
    process_runner: BoundProcessCodexRunner
    ledger: EffectLedger
    ledger_path: Path
    work: Path
    bare: Path
    head_sha: str
    db_path: Path
    binding: PullRequestBinding
    config_path: Path
    stack_config: dict[str, Any]


def _apply_happy_path_env(config: dict[str, Any]) -> None:
    os.environ["XDG_STATE_HOME"] = config["xdg_state"]
    os.environ["XDG_CONFIG_HOME"] = config["xdg_config"]
    os.environ["XDG_CACHE_HOME"] = config["xdg_cache"]
    os.environ["TMPDIR"] = "/tmp"
    os.environ["TMP"] = "/tmp"
    os.environ["TEMP"] = "/tmp"
    os.environ["FAKE_AGENT_MODIFY_MODE"] = "tracked"
    os.environ["FAKE_CODEX_REVIEW_SEQUENCE"] = "no_findings"
    os.environ["PATH"] = config["path"]
    os.environ["STATEFUL_GH_STATE"] = config["gh_state"]


def _assemble_core(
    *,
    config: dict[str, Any],
    run_id: str | None,
    include_control: bool,
) -> ExistingPrHappyPathStack:
    _apply_happy_path_env(config)
    work = Path(config["work"])
    bare = Path(config["bare"])
    head_sha_value = str(config["head_sha"])
    db_path = Path(config["db_path"])
    artifact_root = Path(config["artifact_root"])
    ledger_path = Path(config["ledger_path"])
    gh_path = Path(config["gh_command"])
    path = str(config["path"])

    # Never construct StatefulFakeGhController() here: __init__ wipes durable state.
    controller = StatefulFakeGhController.attach(Path(config["gh_state"]))
    clock = FakeClock(T0)
    engine = PrReviewEngine(
        SqlitePrReviewStore(db_path),
        clock=clock,
        ids=SequenceIdFactory(prefix="happy"),
        lease_ttl=timedelta(seconds=120),
    )
    arts = ProtectedResultStore(artifact_root)
    input_reader = InputArtifactReader(artifact_root)

    gh_env = build_minimal_gh_env()
    gh_env["PATH"] = path
    read_transport = GhApiTransport(
        command=str(gh_path),
        cwd=str(work),
        per_call_timeout_seconds=30.0,
        env=gh_env,
    )
    write_transport = GhWriteTransport(
        command=str(gh_path),
        cwd=str(work),
        per_call_timeout_seconds=30.0,
        env=gh_env,
    )
    read_policy = GitHubReadPolicy(
        gh_command=str(gh_path),
        repository_cwd=str(work),
        reviewer_logins=(REVIEWER,),
        accepted_no_findings_prefixes=(),
        accept_bot_thumbs_up=True,
        poll_interval_seconds=60,
        per_call_timeout_seconds=30.0,
        overall_timeout_seconds=120.0,
    )
    read_gateway = GitHubReadGateway(
        policy=read_policy,
        transport=read_transport,
        artifacts=ReviewArtifactStore(artifact_root),
    )
    read_executor = GitHubReadExecutor(gateway=read_gateway, policy=read_policy, clock=clock)

    git_policy = GitWritePolicy(
        repository_cwd=str(work),
        remote_scheme=GitRemoteScheme.LOCAL,
        require_ssh_agent_identity=False,
        remote_name="origin",
        per_call_timeout_seconds=30.0,
        overall_timeout_seconds=120.0,
    )
    git_transport = GitWriteTransport(
        repository_cwd=str(work),
        git_command="git",
        per_call_timeout_seconds=30.0,
        env={**_GIT_ENV, "PATH": path},
    )
    git_gateway = GitPublicationGateway(
        policy=git_policy,
        transport=git_transport,
        input_reader=input_reader,
    )
    github_write_policy = GitHubWritePolicy(
        gh_command=str(gh_path),
        repository_cwd=str(work),
        review_command_body="@codex review",
        per_call_timeout_seconds=30.0,
        overall_timeout_seconds=120.0,
    )
    github_write_gateway = GitHubWriteGateway(
        policy=github_write_policy,
        write_transport=write_transport,
        read_transport=read_transport,
        input_reader=input_reader,
        write_evidence=WriteEvidenceStore(artifact_root),
    )
    write_executor = WriteExecutor(
        git_gateway=git_gateway,
        github_gateway=github_write_gateway,
        github_policy=github_write_policy,
    )
    reconcile_executor = ReconcileWriteExecutor(
        git_gateway=git_gateway,
        github_gateway=github_write_gateway,
        github_policy=github_write_policy,
    )

    codex_env = build_minimal_codex_env(
        {
            **os.environ,
            "PATH": path,
            "HOME": config.get("home", os.environ.get("HOME", "")),
        }
    )
    process_runner = BoundProcessCodexRunner(artifact_root=artifact_root, env=codex_env)
    if run_id is not None:
        process_runner.bind(run_id)
    publication_runner = PublicationTextRunner(
        artifact_root=artifact_root,
        process_runner=process_runner,
        timeout_seconds=30.0,
    )
    adjudication_runner = ThreadAdjudicationRunner(
        artifact_root=artifact_root,
        process_runner=process_runner,
        timeout_seconds=30.0,
    )
    local_adapter = LocalFixAdapter(
        runtime=FilesystemLocalCarrierRuntime(),
        store=arts,
    )
    local_executor = LocalEffectExecutor(
        store=arts,
        publication_runner=publication_runner,
        adjudication_runner=adjudication_runner,
        local_fix_adapter=local_adapter,
        context_resolver=EngineOriginContextResolver(engine, arts),
    )
    production_router = EffectExecutorRouter(
        read_executor=read_executor,
        write_executor=write_executor,
        reconcile_executor=reconcile_executor,
        local_executor=local_executor,
    )
    ledger = EffectLedger.load(ledger_path)
    router = ObservationScriptingRouter(
        production_router,
        controller=controller,
        ledger=ledger,
        ledger_path=ledger_path,
    )
    worker = EffectWorker(
        engine,
        router,
        owner_id="owner-existing-pr-happy",
        heartbeat_interval=timedelta(seconds=5),
        heartbeat_interval_wait=_ReusableIntervalWait(),
    )
    launchers = SupervisorLauncherStore(arts.root)
    registry = SupervisorProcessRegistry()
    child_env = {
        **os.environ,
        "PATH": path,
        "XDG_STATE_HOME": config["xdg_state"],
        "XDG_CONFIG_HOME": config["xdg_config"],
        "XDG_CACHE_HOME": config["xdg_cache"],
        "TMPDIR": "/tmp",
        "TMP": "/tmp",
        "TEMP": "/tmp",
        "FAKE_AGENT_MODIFY_MODE": "tracked",
        "FAKE_CODEX_REVIEW_SEQUENCE": "no_findings",
        "STATEFUL_GH_STATE": config["gh_state"],
        "HAPPY_PATH_STACK_CONFIG": str(Path(config["config_path"]).resolve()),
    }
    if include_control:
        control = ControlPlaneService(
            engine,
            artifact_store=arts,
            launcher_store=launchers,
            spawner=lambda rid: spawn_happy_path_supervisor(
                rid,
                artifact_root=arts.root,
                launcher_store=launchers,
                config_path=Path(config["config_path"]),
                registry=registry,
                child_env=child_env,
            ),
        )
    else:
        control = ControlPlaneService(
            engine,
            artifact_store=arts,
            launcher_store=launchers,
            spawner=None,
        )
    prep = PreparationService(engine, arts, clock=clock)
    binding = PullRequestBinding(
        repository=RepositoryIdentity(name_with_owner=f"{OWNER}/{NAME}"),
        pr_number=PR_NUMBER,
        head_branch="feature",
        base_branch="main",
        head_sha=head_sha_value,
    )
    return ExistingPrHappyPathStack(
        clock=clock,
        engine=engine,
        artifacts=arts,
        prep=prep,
        control=control,
        worker=worker,
        launchers=launchers,
        supervisor_registry=registry,
        controller=controller,
        fake_codex=CodexInvocationLog(Path(config["codex_log_path"])),
        process_runner=process_runner,
        ledger=ledger,
        ledger_path=ledger_path,
        work=work,
        bare=bare,
        head_sha=head_sha_value,
        db_path=db_path,
        binding=binding,
        config_path=Path(config["config_path"]),
        stack_config=config,
    )


def assemble_existing_pr_happy_path_stack_from_config(
    config: dict[str, Any],
    *,
    run_id: str,
) -> ExistingPrHappyPathStack:
    """Rebuild the happy-path stack inside the controlled supervisor subprocess."""

    return _assemble_core(config=config, run_id=run_id, include_control=False)


def assemble_existing_pr_happy_path_stack(
    tmp_path: Path,
    *,
    fake_clis: dict[str, Path],
    monkeypatch: Any,
) -> ExistingPrHappyPathStack:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg / "cache"))
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("TMP", "/tmp")
    monkeypatch.setenv("TEMP", "/tmp")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")

    work, bare, head_sha_value = init_existing_pr_git(tmp_path / "git")
    controller = StatefulFakeGhController(tmp_path / "gh-state.json")
    title, body = seed_owned_pr_preimage()
    controller.state.fixture = {
        "owner": OWNER,
        "name": NAME,
        "pr_number": PR_NUMBER,
        "head_sha": head_sha_value,
        "head_branch": "feature",
        "base_branch": "main",
        "title": title,
        "body": body,
        "default_thread_id": THREAD_ID,
        "threads": [],
        "reactions": {},
    }
    controller.state.save(controller.state_path)
    gh_path = controller.install(tmp_path / "gh-bin")

    codex_bin = tmp_path / "happy-codex-bin"
    codex_log = tmp_path / "codex" / "invocations.jsonl"
    codex_counter = tmp_path / "codex" / "counter.txt"
    install_happy_path_codex(
        codex_bin,
        payloads_dir=tmp_path / "codex" / "payloads",
        log_path=codex_log,
        counter_path=codex_counter,
        delegate_codex=fake_clis["bin_dir"] / "codex",
    )
    path = (
        f"{codex_bin}:{fake_clis['bin_dir']}:{gh_path.parent}:{os.environ.get('PATH', '')}"
    )
    monkeypatch.setenv("PATH", path)

    db_path = tmp_path / "engine.sqlite3"
    artifact_root = tmp_path / "artifacts"
    ledger_path = tmp_path / "effect-ledger.json"
    config_path = tmp_path / "happy-path-stack.json"
    config = {
        "xdg_state": str(xdg),
        "xdg_config": str(xdg / "config"),
        "xdg_cache": str(xdg / "cache"),
        "home": str(tmp_path / "home"),
        "path": path,
        "work": str(work),
        "bare": str(bare),
        "head_sha": head_sha_value,
        "db_path": str(db_path),
        "artifact_root": str(artifact_root),
        "ledger_path": str(ledger_path),
        "gh_state": str(controller.state_path),
        "gh_command": str(gh_path),
        "codex_log_path": str(codex_log),
        "codex_counter_path": str(codex_counter),
        "codex_payloads_dir": str(tmp_path / "codex" / "payloads"),
        "config_path": str(config_path),
    }
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    EffectLedger().save(ledger_path)
    return _assemble_core(config=config, run_id=None, include_control=True)


def head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").lower()


def remote_head_sha(bare: Path, branch: str = "feature") -> str:
    return _git(bare, "rev-parse", branch).lower()


def count_commits_since(repo: Path, since_sha: str) -> int:
    out = _git(repo, "rev-list", "--count", f"{since_sha}..HEAD")
    return int(out)


def reload_ledger(stack: ExistingPrHappyPathStack) -> EffectLedger:
    stack.ledger = EffectLedger.load(stack.ledger_path)
    return stack.ledger


__all__ = [
    "PLAN_BYTES",
    "PROMPT_BYTES",
    "PLAN_SHA",
    "PROMPT_SHA",
    "SESSION",
    "THREAD_ID",
    "CodexInvocationLog",
    "ExistingPrHappyPathStack",
    "OwnedChildStore",
    "assemble_existing_pr_happy_path_stack",
    "assemble_existing_pr_happy_path_stack_from_config",
    "build_execution_context",
    "count_commits_since",
    "head_sha",
    "reload_ledger",
    "remote_head_sha",
    "seed_owned_pr_preimage",
    "sha256_bytes",
    "spawn_happy_path_supervisor",
    "wait_for_supervisor_completed",
]
