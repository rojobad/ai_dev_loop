"""Phase 16.8 Gate A: production-assembled existing-PR happy path to completed.

Does not cover source-run E2E or exhaustive resilience matrices.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from tests.integration.phase16_8_existing_pr_happy_path_helpers import (
    PLAN_BYTES,
    PROMPT_BYTES,
    SESSION,
    THREAD_ID,
    OwnedChildStore,
    assemble_existing_pr_happy_path_stack,
    build_execution_context,
    count_commits_since,
    head_sha,
    reload_ledger,
    remote_head_sha,
    wait_for_supervisor_completed,
)
from tests.unit.pr_review_v2.durable_helpers import FakeClock

from ai_dev_loop.pr_review_v2.application.control_contracts import (
    OriginKind,
    SafeNextAction,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.preparation import ExistingPrSnapshot
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import carrier_run_id
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.workers.supervisor import validate_launcher_ownership_against_os
from ai_dev_loop.pr_review_v2_carrier import FilesystemLocalCarrierRuntime
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import RunStatus


@pytest.fixture(autouse=True)
def _native_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("TMP", "/tmp")
    monkeypatch.setenv("TEMP", "/tmp")


def _git_log_message(repo: Path) -> str:
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_existing_pr_production_assembled_happy_path_reaches_completed(
    tmp_path: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = assemble_existing_pr_happy_path_stack(
        tmp_path, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    ctx = build_execution_context(
        repo_root=str(stack.work.resolve()),
        head_sha=stack.head_sha,
        gh_command=str(tmp_path / "gh-bin" / "gh"),
    )

    head_before = head_sha(stack.work)
    remote_before = remote_head_sha(stack.bare)
    mutations_before = dict(stack.controller.mutation_counts)
    comments_before = len(stack.controller.reload().issue_comments)

    created = stack.prep.prepare_existing_pr(
        ExistingPrSnapshot(
            binding=stack.binding,
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=ctx,
            accepted_patch_bytes=None,
        )
    )
    assert created.reused is False
    assert created.origin_kind is OriginKind.EXISTING_PR
    assert created.next_action is SafeNextAction.START
    assert stack.engine.get_status(created.run_id).state_kind == "prepared"

    # Bind Codex owned-child identity before start (worker also binds on rebuild).
    stack.process_runner.bind(created.run_id)

    # 1. prepare has no Git/GitHub side effects
    assert head_sha(stack.work) == head_before
    assert remote_head_sha(stack.bare) == remote_before
    assert stack.controller.mutation_counts == mutations_before
    assert len(stack.controller.reload().issue_comments) == comments_before
    assert stack.fake_codex.all_argv == []
    assert reload_ledger(stack).kinds == []

    # 2. explicit start through production launcher/ownership boundary
    start = stack.control.start(created.run_id)
    assert start.transition_applied is True
    assert start.supervisor_action == "spawned"
    after_start = stack.engine.get_status(created.run_id)
    assert after_start.state_kind == "waiting_for_bot"
    assert after_start.active_effect_kind == "request_bot_review"

    meta = stack.launchers.read(created.run_id)
    assert meta is not None
    assert validate_launcher_ownership_against_os(meta, run_id=created.run_id)

    # 3–10. launched supervisor performs the workflow; test does not drive the loop
    final = wait_for_supervisor_completed(
        engine=stack.engine,
        run_id=created.run_id,
        registry=stack.supervisor_registry,
        launchers=stack.launchers,
        timeout_seconds=180.0,
    )
    assert final == "completed"
    assert stack.launchers.read(created.run_id) is None

    status = stack.control.status(created.run_id)
    assert status.state_kind == "completed"
    assert status.cycle_number >= 2

    history = stack.control.history(created.run_id, limit=500)
    event_kinds = [entry.event_kind for entry in history.entries]
    assert "start_requested" in event_kinds
    assert "effect_succeeded" in event_kinds

    ledger = reload_ledger(stack)
    succeeded = ledger.succeeded_kinds
    assert succeeded.count("request_bot_review") == 2
    assert succeeded.count("observe_bot_review") == 2
    assert succeeded.count("adjudicate_threads") == 1
    assert succeeded.count("run_local_fix") == 1
    assert succeeded.count("generate_publication_text") == 1
    assert succeeded.count("commit_patch") == 1
    assert succeeded.count("push_commit") == 1
    assert succeeded.count("update_pr_text") == 1
    assert succeeded.count("resolve_thread") == 1
    # All-actionable adjudication resolves after publication; it does not post replies.
    assert succeeded.count("post_thread_reply") == 0
    assert "create_or_update_pr" not in succeeded

    trigger_keys = [
        key
        for key, kind in zip(ledger.idempotency_keys, ledger.kinds, strict=True)
        if kind == "request_bot_review"
    ]
    assert len(trigger_keys) == len(set(trigger_keys)) == 2
    commit_keys = [
        key
        for key, kind in zip(ledger.idempotency_keys, ledger.kinds, strict=True)
        if kind == "commit_patch"
    ]
    assert len(commit_keys) == len(set(commit_keys)) == 1
    assert ledger.push_force_flags == [False]

    assert count_commits_since(stack.work, head_before) == 1
    final_head = head_sha(stack.work)
    assert final_head != head_before
    assert remote_head_sha(stack.bare) == final_head
    assert "ADL-Idempotency:" in _git_log_message(stack.work)

    counts = stack.controller.mutation_counts
    assert counts.get("create_issue_comment", 0) == 2
    assert counts.get("update_pull_request", 0) == 1
    assert counts.get("resolve_review_thread", 0) == 1
    assert counts.get("add_review_thread_reply", 0) == 0
    assert counts.get("create_pull_request", 0) == 0

    assert len(stack.fake_codex.all_argv) == 2
    for argv in stack.fake_codex.all_argv:
        assert "resume" in argv
        assert SESSION in argv
        assert "--last" not in argv
    assert OwnedChildStore(stack.artifacts.root).list_for_run(created.run_id) == []

    fix_effect_id = next(
        eid
        for eid, kind in zip(ledger.effect_ids, ledger.kinds, strict=True)
        if kind == "run_local_fix"
    )
    cid = carrier_run_id(created.run_id, 1, fix_effect_id)
    assert FilesystemLocalCarrierRuntime().carrier_exists(cid)
    _path, carrier_state = load_run(cid)
    assert carrier_state.cursor.chat_id
    assert carrier_state.codex.session_id == SESSION
    assert carrier_state.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_RESIDUAL_RISK}

    gh_state = stack.controller.reload()
    assert any(gh_state.fixture.get("reactions", {}).values())
    open_threads = [
        node
        for node in (gh_state.fixture.get("threads") or [])
        if isinstance(node, dict) and not node.get("isResolved")
    ]
    assert open_threads == []
    assert THREAD_ID in gh_state.resolved_threads or all(
        isinstance(n, dict) and n.get("isResolved") for n in (gh_state.fixture.get("threads") or [])
    )

    reopened = PrReviewEngine(SqlitePrReviewStore(stack.db_path), clock=FakeClock())
    assert reopened.get_status(created.run_id).state_kind == "completed"
