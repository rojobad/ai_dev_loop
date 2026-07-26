"""Assemble the production PR review v2 supervisor runtime for one run."""

from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.pr_review_v2.application.control import EngineOriginContextResolver
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextArtifact,
    ExecutionContextPrReviewV2,
)
from ai_dev_loop.pr_review_v2.application.github_read import GitHubReadPolicy
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    GitHubWritePolicy,
    GitRemoteScheme,
    GitWritePolicy,
)
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    CodexProcessResult,
    PublicationTextRunner,
    ThreadAdjudicationRunner,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhApiTransport
from ai_dev_loop.pr_review_v2.infrastructure.gh_write_transport import GhWriteTransport
from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import GitPublicationGateway
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitWriteTransport
from ai_dev_loop.pr_review_v2.infrastructure.github_read_gateway import GitHubReadGateway
from ai_dev_loop.pr_review_v2.infrastructure.github_write_gateway import GitHubWriteGateway
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import LocalFixAdapter
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    default_engine_db_path,
    pr_review_v2_state_dir,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import ReviewArtifactStore
from ai_dev_loop.pr_review_v2.infrastructure.write_evidence_artifacts import WriteEvidenceStore
from ai_dev_loop.pr_review_v2.workers.effect_executor_router import EffectExecutorRouter
from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker
from ai_dev_loop.pr_review_v2.workers.github_read_executor import GitHubReadExecutor
from ai_dev_loop.pr_review_v2.workers.local_executor import LocalEffectExecutor
from ai_dev_loop.pr_review_v2.workers.owned_children import OwnedChildStore, register_owned_child
from ai_dev_loop.pr_review_v2.workers.reconcile_write_executor import ReconcileWriteExecutor
from ai_dev_loop.pr_review_v2.workers.write_executor import WriteExecutor
from ai_dev_loop.pr_review_v2_carrier import FilesystemLocalCarrierRuntime

MAX_CODEX_CAPTURE_BYTES = 1_048_576
_CODEX_MINIMAL_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "CODEX_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
)


@dataclass(frozen=True)
class AssembledSupervisorRuntime:
    engine: PrReviewEngine
    worker: EffectWorker
    execution_context: ExecutionContextArtifact
    idle_poll_seconds: float
    artifact_root: Path


def build_minimal_codex_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Allowlisted environment for Codex children (no inherited secrets)."""

    source = dict(base) if base is not None else dict(os.environ)
    env: dict[str, str] = {}
    for key in _CODEX_MINIMAL_ENV_KEYS:
        if key in source and source[key]:
            env[key] = source[key]
    return env


class ProcessCodexRunner:
    """Production Codex process runner using argv arrays and stdin delivery."""

    def __init__(
        self,
        *,
        artifact_root: Path,
        run_id: str,
        env: dict[str, str] | None = None,
        max_capture_bytes: int = MAX_CODEX_CAPTURE_BYTES,
        term_grace_seconds: float = 0.2,
    ) -> None:
        self._children = OwnedChildStore(artifact_root)
        self._run_id = run_id
        self._env = build_minimal_codex_env(env)
        self._max_capture_bytes = max_capture_bytes
        self._term_grace_seconds = term_grace_seconds

    def run(
        self,
        argv: list[str],
        *,
        cwd: str,
        stdin_text: str,
        timeout_seconds: float,
    ) -> CodexProcessResult:
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                shell=False,
                start_new_session=True,
                env=dict(self._env),
            )
        except FileNotFoundError as exc:
            raise AiDevLoopError(f"executable not found: {argv[0]}") from exc
        try:
            pgid = os.getpgid(proc.pid)
            try:
                exe = str(Path(f"/proc/{proc.pid}/exe").resolve())
            except OSError:
                exe = str(Path(sys.executable).resolve())
            register_owned_child(
                self._children,
                run_id=self._run_id,
                component="codex",
                pid=proc.pid,
                pgid=pgid,
                executable=exe,
            )
        except Exception as exc:
            self._terminate_and_reap(proc)
            raise AiDevLoopError(
                "failed to register owned Codex child; child process group terminated"
            ) from exc

        timed_out = False
        oversized = False
        stdout = b""
        stderr = b""
        returncode = 1
        try:
            try:
                stdout, stderr, oversized = self._communicate_bounded(
                    proc,
                    stdin_text=stdin_text,
                    timeout_seconds=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_and_reap(proc)
                stdout, stderr = b"", b""
            else:
                if oversized or proc.poll() is None:
                    self._terminate_and_reap(proc)
            returncode = proc.returncode if proc.returncode is not None else 1
            if oversized:
                raise AiDevLoopError("codex process output exceeded capture bound")
        finally:
            # Ownership metadata cleared only after TERM→KILL + mandatory reap.
            if proc.poll() is None:
                self._terminate_and_reap(proc)
            self._children.clear(self._run_id, "codex")
        return CodexProcessResult(
            returncode=returncode,
            stdout_bytes=stdout,
            stderr_bytes=stderr,
            timed_out=timed_out,
        )

    def _communicate_bounded(
        self,
        proc: subprocess.Popen[bytes],
        *,
        stdin_text: str,
        timeout_seconds: float,
    ) -> tuple[bytes, bytes, bool]:
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        deadline = time.monotonic() + max(0.01, float(timeout_seconds))
        stdin_bytes = stdin_text.encode("utf-8")
        stdin_offset = 0
        stdin_closed = False
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        oversized = False

        for stream in (proc.stdin, proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)

        readers = {proc.stdout, proc.stderr}
        while readers or not stdin_closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout_seconds)
            writable: list[Any] = []
            if not stdin_closed and proc.stdin is not None:
                writable = [proc.stdin]
            ready_r, ready_w, _ = select.select(list(readers), writable, [], min(remaining, 0.25))
            for stream in ready_w:
                try:
                    written = os.write(
                        stream.fileno(), stdin_bytes[stdin_offset : stdin_offset + 65_536]
                    )
                except BrokenPipeError:
                    stdin_closed = True
                    with contextlib.suppress(OSError):
                        stream.close()
                    continue
                except BlockingIOError:
                    continue
                if written:
                    stdin_offset += written
                if stdin_offset >= len(stdin_bytes):
                    with contextlib.suppress(OSError):
                        stream.close()
                    stdin_closed = True
            if not ready_r:
                if proc.poll() is not None and not ready_w:
                    for stream in list(readers):
                        chunk = stream.read() or b""
                        if stream is proc.stdout:
                            stdout_buf.extend(chunk)
                        else:
                            stderr_buf.extend(chunk)
                        readers.discard(stream)
                    if not stdin_closed:
                        with contextlib.suppress(OSError):
                            if proc.stdin is not None:
                                proc.stdin.close()
                        stdin_closed = True
                    break
                continue
            for stream in ready_r:
                try:
                    chunk = stream.read(65_536) or b""
                except BlockingIOError:
                    continue
                if not chunk:
                    readers.discard(stream)
                    continue
                target = stdout_buf if stream is proc.stdout else stderr_buf
                target.extend(chunk)
                if len(stdout_buf) + len(stderr_buf) > self._max_capture_bytes:
                    oversized = True
                    readers.clear()
                    break
        if not stdin_closed and proc.stdin is not None:
            with contextlib.suppress(OSError):
                proc.stdin.close()
        # Wait briefly for exit if still running and not oversized (caller terminates).
        if proc.poll() is None and not oversized:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout_seconds)
            try:
                proc.wait(timeout=max(0.01, remaining))
            except subprocess.TimeoutExpired as exc:
                raise subprocess.TimeoutExpired(proc.args, timeout_seconds) from exc
        return bytes(stdout_buf), bytes(stderr_buf), oversized

    def _terminate_and_reap(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is not None:
            with contextlib.suppress(Exception):
                proc.wait(timeout=0.1)
            return
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            with contextlib.suppress(Exception):
                proc.wait(timeout=0.1)
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + self._term_grace_seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)


def build_github_read_policy(
    v2: ExecutionContextPrReviewV2,
    *,
    repository_cwd: str,
) -> GitHubReadPolicy:
    """Build the read policy from frozen execution context with opt-in gating."""

    timeout = float(v2.per_call_timeout_seconds)
    overall = float(v2.overall_timeout_seconds)
    if v2.no_findings_enabled:
        accepted_prefixes = tuple(v2.no_findings_prefixes)
        accept_thumbs = v2.accept_bot_thumbs_up
    else:
        accepted_prefixes = ()
        accept_thumbs = False
    return GitHubReadPolicy(
        gh_command=v2.gh_command,
        repository_cwd=repository_cwd,
        per_call_timeout_seconds=timeout,
        overall_timeout_seconds=overall,
        max_pages=v2.max_pages,
        max_items=v2.max_items,
        reviewer_logins=v2.reviewer_logins,
        poll_interval_seconds=v2.poll_interval_seconds,
        accepted_no_findings_prefixes=accepted_prefixes,
        accept_bot_thumbs_up=accept_thumbs,
        reviewed_commit_prefix_length=v2.no_findings_prefix_length,
        max_server_directed_wait_seconds=v2.max_server_directed_wait_seconds,
    )


def assemble_supervisor_runtime(run_id: str) -> AssembledSupervisorRuntime:
    """Load hash-verified execution context and assemble all effect executors."""

    artifact_root = pr_review_v2_state_dir() / "artifacts"
    db_path = default_engine_db_path()
    # Bootstrap engine is read-only for protected context resolution. Operational
    # claims/heartbeats must use the frozen per-run lease TTL, not the engine default.
    bootstrap = PrReviewEngine.open(db_path)
    store = ProtectedResultStore(artifact_root)
    context, _ref = EngineOriginContextResolver(bootstrap, store).resolve(run_id)
    v2 = context.pr_review_v2
    lease_ttl = timedelta(seconds=v2.worker.lease_ttl_seconds)
    heartbeat_interval = timedelta(seconds=v2.worker.heartbeat_interval_seconds)
    engine = PrReviewEngine.open(db_path, lease_ttl=lease_ttl)
    timeout = float(v2.per_call_timeout_seconds)
    overall = float(v2.overall_timeout_seconds)
    input_reader = InputArtifactReader(artifact_root)

    read_policy = build_github_read_policy(v2, repository_cwd=context.repository_root)
    read_transport = GhApiTransport(
        command=v2.gh_command,
        cwd=context.repository_root,
        per_call_timeout_seconds=timeout,
    )
    read_gateway = GitHubReadGateway(
        policy=read_policy,
        transport=read_transport,
        artifacts=ReviewArtifactStore(artifact_root),
    )
    read_executor = GitHubReadExecutor(
        gateway=read_gateway,
        policy=read_policy,
        clock=engine.clock,
    )

    git_policy = GitWritePolicy(
        git_command=v2.git_command,
        ssh_command=v2.ssh_command,
        repository_cwd=context.repository_root,
        remote_name=v2.remote_name,
        remote_scheme=GitRemoteScheme.SSH,
        per_call_timeout_seconds=timeout,
        overall_timeout_seconds=overall,
        require_ssh_agent_identity=True,
    )
    github_write_policy = GitHubWritePolicy(
        gh_command=v2.gh_command,
        repository_cwd=context.repository_root,
        per_call_timeout_seconds=timeout,
        overall_timeout_seconds=overall,
        review_command_body=v2.review_trigger_body,
        max_server_directed_wait_seconds=v2.max_server_directed_wait_seconds,
    )
    git_transport = GitWriteTransport(
        repository_cwd=context.repository_root,
        git_command=v2.git_command,
        ssh_command=v2.ssh_command,
        per_call_timeout_seconds=timeout,
    )
    git_gateway = GitPublicationGateway(
        policy=git_policy,
        transport=git_transport,
        input_reader=input_reader,
    )
    github_write_transport = GhWriteTransport(
        command=v2.gh_command,
        cwd=context.repository_root,
        per_call_timeout_seconds=timeout,
    )
    github_write_gateway = GitHubWriteGateway(
        policy=github_write_policy,
        write_transport=github_write_transport,
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

    codex_runner = ProcessCodexRunner(artifact_root=artifact_root, run_id=run_id)
    publication_runner = PublicationTextRunner(
        artifact_root=artifact_root,
        process_runner=codex_runner,
        timeout_seconds=timeout,
    )
    adjudication_runner = ThreadAdjudicationRunner(
        artifact_root=artifact_root,
        process_runner=codex_runner,
        timeout_seconds=timeout,
    )
    local_adapter = LocalFixAdapter(
        runtime=FilesystemLocalCarrierRuntime(),
        store=store,
    )
    local_executor = LocalEffectExecutor(
        store=store,
        publication_runner=publication_runner,
        adjudication_runner=adjudication_runner,
        local_fix_adapter=local_adapter,
        context_resolver=EngineOriginContextResolver(engine, store),
    )
    router = EffectExecutorRouter(
        read_executor=read_executor,
        write_executor=write_executor,
        reconcile_executor=reconcile_executor,
        local_executor=local_executor,
    )
    owner_id = f"supervisor:{run_id}"
    worker = EffectWorker(
        engine,
        router,
        owner_id=owner_id,
        heartbeat_interval=heartbeat_interval,
    )
    return AssembledSupervisorRuntime(
        engine=engine,
        worker=worker,
        execution_context=context,
        idle_poll_seconds=float(v2.worker.idle_poll_seconds),
        artifact_root=artifact_root,
    )


__all__ = [
    "AssembledSupervisorRuntime",
    "MAX_CODEX_CAPTURE_BYTES",
    "ProcessCodexRunner",
    "assemble_supervisor_runtime",
    "build_minimal_codex_env",
]
