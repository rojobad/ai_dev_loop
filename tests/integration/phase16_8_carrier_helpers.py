"""Carrier checkpoint crash/reopen helpers for Phase 16.8 local restart tests."""

from __future__ import annotations

import contextlib
import os
import pickle
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

import pytest
from tests.integration.phase16_8_checkpoint_helpers import (
    install_blocking_codex,
    install_checkpoint_blocking_agent,
    read_ready_pgid,
    read_ready_pid,
    wait_for_path,
)

from ai_dev_loop.commands.start_preflight import RESUMABLE_CHECKPOINT_STATUSES
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import LocalFixAdapter
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.workers.supervisor import process_group_alive
from ai_dev_loop.pr_review_v2_carrier import FilesystemLocalCarrierRuntime
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import RunStatus

CarrierCheckpoint = Literal[
    "before_seed",
    "after_seed",
    "before_cursor",
    "mid_cursor",
    "after_cursor_before_staging",
    "after_staging_before_codex",
    "mid_codex",
    "after_finalization_before_v2_result",
    "terminal_reopen",
]

RUN_ID = "run-p168-local"
PLAN_BYTES = b"frozen-plan\n"
PROMPT_BYTES = b"frozen-prompt\n"
SESSION = "11111111-1111-1111-1111-111111111111"


def _terminate_process_group(pid: int) -> None:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline and process_group_alive(pgid):
        time.sleep(0.02)
    if process_group_alive(pgid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)


def _crash_after_ready(*, ready_path: Path, label: str) -> None:
    ready_path.write_text(f"{label}\n", encoding="utf-8")
    while True:
        time.sleep(3600)


def _apply_carrier_checkpoint_hooks(
    checkpoint: CarrierCheckpoint,
    *,
    ready_path: Path,
) -> None:
    """Install crash hooks in-process (used by worker subprocess and tests)."""

    crash_once = {"fired": False}
    runtime = FilesystemLocalCarrierRuntime
    original_seed = runtime.ensure_seeded_carrier

    def _maybe_crash(label: str) -> None:
        if crash_once["fired"]:
            return
        crash_once["fired"] = True
        _crash_after_ready(ready_path=ready_path, label=label)

    if checkpoint == "before_seed":

        def _seed_block(self, *, carrier_run_id: str, seed) -> None:  # noqa: ANN001
            _maybe_crash("before_seed")
            return original_seed(self, carrier_run_id=carrier_run_id, seed=seed)

        runtime.ensure_seeded_carrier = _seed_block  # type: ignore[method-assign]
        return

    if checkpoint == "after_seed":

        def _seed_after(self, *, carrier_run_id: str, seed) -> None:  # noqa: ANN001
            original_seed(self, carrier_run_id=carrier_run_id, seed=seed)
            _maybe_crash("after_seed")

        runtime.ensure_seeded_carrier = _seed_after  # type: ignore[method-assign]
        return

    if checkpoint == "before_cursor":
        import ai_dev_loop.workflow_engine as workflow_engine

        original_execute = workflow_engine.execute_prompt

        def _before_cursor_block(*args, **kwargs):  # noqa: ANN001
            ready_path.write_text("before_cursor\n", encoding="utf-8")
            _maybe_crash("before_cursor")
            return original_execute(*args, **kwargs)

        workflow_engine.execute_prompt = _before_cursor_block
        return

    if checkpoint == "after_cursor_before_staging":
        import ai_dev_loop.workflow_engine as workflow_engine

        original_staging = workflow_engine._run_staging_pass

        def _staging_block(  # noqa: ANN001
            run_directory,
            state,
            *,
            iteration_number,
            loop_ctx=None,
        ):
            _maybe_crash("after_cursor_before_staging")
            return original_staging(
                run_directory,
                state,
                iteration_number=iteration_number,
                loop_ctx=loop_ctx,
            )

        workflow_engine._run_staging_pass = _staging_block
        return

    if checkpoint == "after_staging_before_codex":
        import ai_dev_loop.workflow_engine as workflow_engine

        original_review = workflow_engine._run_review_pass

        def _review_block(  # noqa: ANN001
            run_directory,
            state,
            *,
            iteration_number,
            loop_ctx=None,
        ):
            _maybe_crash("after_staging_before_codex")
            return original_review(
                run_directory,
                state,
                iteration_number=iteration_number,
                loop_ctx=loop_ctx,
            )

        workflow_engine._run_review_pass = _review_block
        return

    if checkpoint == "after_finalization_before_v2_result":
        original_persist = ProtectedResultStore.persist_local_fix_result

        def _persist_block(self, *, run_id: str, artifact):  # noqa: ANN001
            _maybe_crash("after_finalization_before_v2_result")
            return original_persist(self, run_id=run_id, artifact=artifact)

        ProtectedResultStore.persist_local_fix_result = _persist_block  # type: ignore[method-assign]


def install_carrier_checkpoint_hooks(
    checkpoint: CarrierCheckpoint,
    tmp_path: Path,
    *,
    ready_path: Path,
    proceed_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_path, proceed_path
    _apply_carrier_checkpoint_hooks(checkpoint, ready_path=ready_path)
    monkeypatch.setattr(
        FilesystemLocalCarrierRuntime,
        "ensure_seeded_carrier",
        FilesystemLocalCarrierRuntime.ensure_seeded_carrier,
    )


def blocking_cli_path(
    tmp_path: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    checkpoint: CarrierCheckpoint,
    ready_path: Path,
    proceed_path: Path,
    ledger_path: Path | None = None,
) -> str:
    bin_dir = tmp_path / "blocking-bin"
    ledger = ledger_path or (tmp_path / "cli-invocation.ledger")
    counted_bin = _install_counting_cli_wrappers(
        tmp_path=tmp_path,
        fake_clis=fake_clis,
        ledger_path=ledger,
    )
    if checkpoint == "mid_codex":
        install_blocking_codex(
            bin_dir,
            ready_path=ready_path,
            proceed_path=proceed_path,
            delegate_executable=counted_bin / "codex",
            ledger_path=ledger,
        )
    if checkpoint == "mid_cursor":
        install_checkpoint_blocking_agent(
            bin_dir,
            ready_path=ready_path,
            proceed_path=proceed_path,
            ledger_path=ledger,
        )
    path = f"{bin_dir}:{counted_bin}:{fake_clis['bin_dir']}:{os.environ.get('PATH', '')}"
    monkeypatch.setenv("PATH", path)
    return path


def _fresh_adapter(store: ProtectedResultStore) -> LocalFixAdapter:
    return LocalFixAdapter(runtime=FilesystemLocalCarrierRuntime(), store=store)


def _ledger_counts(ledger_path: Path) -> dict[str, int]:
    if not ledger_path.is_file():
        return {}
    counts: dict[str, int] = {}
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        key = line.strip()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def _install_counting_cli_wrappers(
    *,
    tmp_path: Path,
    fake_clis: dict[str, Path],
    ledger_path: Path,
) -> Path:
    """Wrap replacement fakes so every model invocation shares one ledger."""

    bin_dir = tmp_path / "counted-bin"
    bin_dir.mkdir(exist_ok=True)
    delegates = {
        "agent": fake_clis["bin_dir"] / "agent",
        "codex": fake_clis["bin_dir"] / "codex",
    }
    for command, delegate in delegates.items():
        script = bin_dir / command
        script.write_text(
            (
                f"#!{sys.executable}\n"
                "import os\n"
                "import sys\n"
                f"command = {command!r}\n"
                f"delegate = {str(delegate)!r}\n"
                f"ledger = {str(ledger_path)!r}\n"
                "args = sys.argv[1:]\n"
                "event = None\n"
                "if command == 'agent' and args and args[0] == 'create-chat':\n"
                "    event = 'agent:create-chat'\n"
                "elif command == 'agent' and '-p' in args:\n"
                "    resume_id = args[args.index('--resume') + 1] if '--resume' in args else '<missing>'\n"
                "    event = 'agent:prompt:' + resume_id\n"
                "elif command == 'codex' and args and args[0] == 'exec' and 'resume' in args:\n"
                "    session_id = args[args.index('-') - 1] if '-' in args else '<missing>'\n"
                "    event = 'codex:review:' + session_id\n"
                "if event is not None:\n"
                "    with open(ledger, 'a', encoding='utf-8') as handle:\n"
                "        handle.write(event + '\\n')\n"
                "os.execv(delegate, [delegate, *args])\n"
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)
    return bin_dir


def _count_agent_invocations(agent_log: Path, *, needle: str) -> int:
    if not agent_log.is_file():
        return 0
    return agent_log.read_text(encoding="utf-8").count(needle)


def _count_codex_reviews(codex_log: Path) -> int:
    if not codex_log.is_file():
        return 0
    return codex_log.read_text(encoding="utf-8").count("ARGS:['exec'")


def _assert_carrier_recovery(
    *,
    carrier_id: str,
    outcome,
    first_chat: str | None = None,
    agent_log: Path | None = None,
    codex_log: Path | None = None,
    create_chat_calls_before: int | None = None,
    codex_reviews_before: int | None = None,
    ledger_path: Path | None = None,
    expected_ledger: dict[str, int] | None = None,
) -> None:
    _path, terminal = load_run(carrier_id)
    assert terminal.codex.session_id == SESSION
    assert terminal.cursor.chat_id
    if first_chat is not None:
        assert terminal.cursor.chat_id == first_chat
    assert outcome.outcome.value == "accepted"  # type: ignore[union-attr]
    if terminal.iterations:
        numbers = [int(entry.get("number", 0)) for entry in terminal.iterations]
        assert numbers == sorted(numbers)
        assert len(numbers) == len(set(numbers))
    if agent_log is not None and create_chat_calls_before is not None:
        assert _count_agent_invocations(agent_log, needle="create-chat") == create_chat_calls_before
    if codex_log is not None and codex_reviews_before is not None:
        assert _count_codex_reviews(codex_log) >= codex_reviews_before
    if ledger_path is not None and expected_ledger is not None:
        counts = _ledger_counts(ledger_path)
        assert counts == expected_ledger


def _assert_resumable_carrier_state(carrier_id: str) -> None:
    _path, state = load_run(carrier_id)
    assert state.status in RESUMABLE_CHECKPOINT_STATUSES, (
        f"carrier must remain on a production resume checkpoint, got {state.status.value}"
    )
    assert state.status is not RunStatus.FAILED


def _carrier_worker_main(spec_path: str) -> None:
    spec = pickle.loads(Path(spec_path).read_bytes())
    for key, value in spec["env"].items():
        os.environ[key] = value
    checkpoint = spec.get("checkpoint")
    if checkpoint:
        _apply_carrier_checkpoint_hooks(checkpoint, ready_path=Path(spec["ready_path"]))
    from ai_dev_loop.pr_review_v2.application.execution_context import ExecutionContextArtifact
    from ai_dev_loop.pr_review_v2.domain.effects import RunLocalFixEffect
    from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import LocalFixAdapter
    from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
        ProtectedResultStore,
    )
    from ai_dev_loop.pr_review_v2_carrier import FilesystemLocalCarrierRuntime

    store = ProtectedResultStore(Path(spec["artifact_root"]))
    ctx = ExecutionContextArtifact.model_validate_json(spec["ctx_json"])
    effect = RunLocalFixEffect.model_validate_json(spec["effect_json"])
    adapter = LocalFixAdapter(runtime=FilesystemLocalCarrierRuntime(), store=store)
    adapter.execute(
        run_id=spec["run_id"],
        effect=effect,
        execution_context=ctx,
        fix_prompt_bytes=spec["fix_prompt_bytes"],
        plan_bytes=spec["plan_bytes"],
        prompt_bytes=spec["prompt_bytes"],
    )


def _spawn_carrier_worker(
    *,
    tmp_path: Path,
    env: dict[str, str],
    run_id: str,
    ctx,
    effect,
    fix_prompt: bytes,
    checkpoint: CarrierCheckpoint | None,
    ready_path: Path,
) -> subprocess.Popen[bytes]:
    spec_path = tmp_path / "carrier-worker-spec.pkl"
    spec_path.write_bytes(
        pickle.dumps(
            {
                "env": env,
                "artifact_root": str(tmp_path / "artifacts"),
                "run_id": run_id,
                "ctx_json": ctx.model_dump_json(),
                "effect_json": effect.model_dump_json(),
                "fix_prompt_bytes": fix_prompt,
                "plan_bytes": PLAN_BYTES,
                "prompt_bytes": PROMPT_BYTES,
                "checkpoint": checkpoint,
                "ready_path": str(ready_path),
            }
        )
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from tests.integration.phase16_8_carrier_helpers import _carrier_worker_main; "
            f"_carrier_worker_main({str(spec_path)!r})",
        ],
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
        start_new_session=True,
    )


def run_carrier_until_checkpoint_then_resume(
    *,
    checkpoint: CarrierCheckpoint,
    adapter: LocalFixAdapter,
    run_id: str,
    effect,
    execution_context,
    fix_prompt: bytes,
    carrier_id: str,
    ready_path: Path,
    proceed_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_clis: dict[str, Path],
) -> object:
    del proceed_path, adapter
    store = ProtectedResultStore(tmp_path / "artifacts")
    agent_log = Path(fake_clis["agent_log"])
    codex_log = Path(fake_clis["codex_log"])
    ledger_path = tmp_path / "cli-invocation.ledger"
    counted_bin = _install_counting_cli_wrappers(
        tmp_path=tmp_path,
        fake_clis=fake_clis,
        ledger_path=ledger_path,
    )
    monkeypatch.setenv(
        "PATH",
        f"{counted_bin}:{fake_clis['bin_dir']}:{os.environ.get('PATH', '')}",
    )
    create_chat_before = _count_agent_invocations(agent_log, needle="create-chat")
    codex_reviews_before = _count_codex_reviews(codex_log)

    if checkpoint == "terminal_reopen":
        first = _fresh_adapter(store).execute(
            run_id=run_id,
            effect=effect,
            execution_context=execution_context,
            fix_prompt_bytes=fix_prompt,
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
        )
        second = _fresh_adapter(store).execute(
            run_id=run_id,
            effect=effect,
            execution_context=execution_context,
            fix_prompt_bytes=fix_prompt,
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
        )
        _assert_carrier_recovery(
            carrier_id=carrier_id,
            outcome=second,
            agent_log=agent_log,
            codex_log=codex_log,
            create_chat_calls_before=create_chat_before,
            codex_reviews_before=codex_reviews_before,
            ledger_path=ledger_path,
            expected_ledger={
                f"agent:prompt:{execution_context.cursor.chat_id}": 1,
                f"codex:review:{SESSION}": 1,
            },
        )
        assert second.outcome.value == first.outcome.value  # type: ignore[union-attr]
        return second

    blocking_cli_path(
        tmp_path,
        fake_clis,
        monkeypatch,
        checkpoint=checkpoint,
        ready_path=ready_path,
        proceed_path=tmp_path / "unused-proceed",
        ledger_path=ledger_path,
    )

    worker_env = os.environ.copy()
    proc = _spawn_carrier_worker(
        tmp_path=tmp_path,
        env=worker_env,
        run_id=run_id,
        ctx=execution_context,
        effect=effect,
        fix_prompt=fix_prompt,
        checkpoint=checkpoint,
        ready_path=ready_path,
    )

    worker_checkpoints = {
        "before_seed",
        "after_seed",
        "before_cursor",
        "mid_cursor",
        "after_cursor_before_staging",
        "after_staging_before_codex",
        "mid_codex",
        "after_finalization_before_v2_result",
    }

    seeded_chat: str | None = None
    orphan_pgid: int | None = None
    if checkpoint in worker_checkpoints:
        wait_for_path(ready_path, timeout_seconds=90.0)

    if checkpoint == "before_cursor":
        assert ready_path.read_text(encoding="utf-8").strip() == "before_cursor"
        agent_tail = agent_log.read_text(encoding="utf-8") if agent_log.is_file() else ""
        assert "ARGS:" not in agent_tail or "-p" not in agent_tail

    if checkpoint in {
        "before_cursor",
        "mid_cursor",
    } and FilesystemLocalCarrierRuntime().carrier_exists(carrier_id):
        _path, seeded = load_run(carrier_id)
        assert seeded.codex.session_id == SESSION
        seeded_chat = seeded.cursor.chat_id

    if checkpoint == "before_seed":
        assert not FilesystemLocalCarrierRuntime().carrier_exists(carrier_id)
    elif checkpoint == "after_seed":
        assert FilesystemLocalCarrierRuntime().carrier_exists(carrier_id)
        _path, seeded = load_run(carrier_id)
        assert seeded.status == RunStatus.PREPARED
        assert seeded.codex.session_id == SESSION
        seeded_chat = seeded.cursor.chat_id

    if checkpoint in {"mid_cursor", "mid_codex"}:
        ready_text = ready_path.read_text(encoding="utf-8")
        assert "ready" in ready_text.splitlines()[0]
        assert read_ready_pid(ready_path) > 0
        orphan_pgid = read_ready_pgid(ready_path)
        assert orphan_pgid > 0
        assert process_group_alive(orphan_pgid)

    # Kill only the carrier worker process group. Cursor/Codex children use a
    # distinct PGID and must remain live until production ownership recovery.
    worker_pgid = os.getpgid(proc.pid)
    _terminate_process_group(proc.pid)
    proc.wait(timeout=30.0)
    assert proc.returncode is not None
    assert not process_group_alive(worker_pgid)

    if orphan_pgid is not None:
        assert process_group_alive(orphan_pgid), (
            "orphaned Cursor/Codex child must still be live after worker-only kill"
        )

    expects_resumable_carrier = checkpoint in {
        "after_seed",
        "before_cursor",
        "mid_cursor",
        "after_cursor_before_staging",
        "after_staging_before_codex",
        "mid_codex",
    }
    if expects_resumable_carrier and FilesystemLocalCarrierRuntime().carrier_exists(carrier_id):
        _assert_resumable_carrier_state(carrier_id)

    # Resume uses non-blocking fakes; production LocalFixAdapter fences orphans first.
    resume_path = f"{counted_bin}:{fake_clis['bin_dir']}:{os.environ.get('PATH', '')}"
    monkeypatch.setenv("PATH", resume_path)

    resumed = _fresh_adapter(store).execute(
        run_id=run_id,
        effect=effect,
        execution_context=execution_context,
        fix_prompt_bytes=fix_prompt,
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
    )

    if orphan_pgid is not None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and process_group_alive(orphan_pgid):
            time.sleep(0.05)
        assert not process_group_alive(orphan_pgid), (
            "production ownership recovery must terminate the original child PGID "
            "before a replacement child can run"
        )

    prompt_count = 2 if checkpoint == "mid_cursor" else 1
    review_count = 2 if checkpoint == "mid_codex" else 1
    expected_ledger = {
        f"agent:prompt:{execution_context.cursor.chat_id}": prompt_count,
        f"codex:review:{SESSION}": review_count,
    }

    create_chat_after = _count_agent_invocations(agent_log, needle="create-chat")
    if seeded_chat is not None:
        # Crash/reopen must reuse the exact chat; never create a second one.
        assert create_chat_after == create_chat_before or (
            create_chat_before == 0 and create_chat_after == 1
        )
    elif checkpoint == "before_seed":
        # If execution context already binds a chat id, prepare reuses it and must
        # not invent a second create-chat call. Otherwise create exactly one.
        if execution_context.cursor.chat_id:
            assert create_chat_after == 0
        else:
            assert create_chat_after == 1

    _assert_carrier_recovery(
        carrier_id=carrier_id,
        outcome=resumed,
        first_chat=seeded_chat
        or (execution_context.cursor.chat_id if checkpoint == "before_seed" else None),
        agent_log=agent_log,
        codex_log=codex_log,
        create_chat_calls_before=create_chat_after,
        codex_reviews_before=codex_reviews_before,
        ledger_path=ledger_path,
        expected_ledger=expected_ledger,
    )
    return resumed


__all__ = ["CarrierCheckpoint", "run_carrier_until_checkpoint_then_resume"]
