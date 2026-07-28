"""Phase 16.8 LOCAL publication/adjudication through production LocalEffectExecutor paths."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.integration.phase16_4_matrix_helpers import claim_next
from tests.integration.phase16_8_checkpoint_helpers import (
    install_blocking_codex,
    install_term_refusing_codex,
    reopen_engine,
)
from tests.integration.phase16_8_local_engine_helpers import (
    LOCAL_CODEX_CRASH_CASES,
    RUN_ID,
    SESSION,
    _BlockingCodexProcessRunner,
    _CapturingRunner,
    _PreSpawnFailureRunner,
    adjudication_payload,
    assert_exact_session_argv,
    assert_safe_fields_exclude_sentinels,
    assert_stdin_contains_sentinels,
    build_local_worker,
    build_prepared_local_engine,
    complete_publication_worker_step,
    local_fixture,
    make_executor,
    publication_payload,
)
from tests.unit.pr_review_v2.durable_helpers import FakeClock

from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EventDisposition,
    NextActionCategory,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    AdjudicationRecordedOutcome,
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    PublicationTextPreparedOutcome,
)
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import FakeCodexProcessRunner

T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _native_tmpdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("TMP", "/tmp")
    monkeypatch.setenv("TEMP", "/tmp")


@pytest.mark.parametrize("effect_kind", ["publication", "adjudication"])
@pytest.mark.parametrize("crash_case", LOCAL_CODEX_CRASH_CASES)
def test_local_executor_codex_crash_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effect_kind: str,
    crash_case: str,
) -> None:
    store, _ctx, effect, token, holder = local_fixture(tmp_path, effect_kind=effect_kind)  # type: ignore[arg-type]
    payload = publication_payload() if effect_kind == "publication" else adjudication_payload()
    forbidden = ("SENTINEL_PROMPT_BODY_DO_NOT_LEAK_987654321", SESSION)

    if crash_case == "cache_replay":
        runner1 = _CapturingRunner(result_payload=payload)
        executor1 = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner1)
        first = executor1.execute(effect, token, now=T0)
        assert isinstance(first, EffectSucceeded)
        assert_stdin_contains_sentinels(runner1)
        assert_exact_session_argv(runner1)
        assert_safe_fields_exclude_sentinels(first, forbidden)

        runner2 = _CapturingRunner(result_payload={"title": "should-not-run"})
        executor2 = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner2)
        again = executor2.execute(effect, token, now=T0)
        assert isinstance(again, EffectSucceeded)
        assert runner2.invocations == 0
        if effect_kind == "publication":
            assert isinstance(again.outcome, PublicationTextPreparedOutcome)
        else:
            assert isinstance(again.outcome, AdjudicationRecordedOutcome)
        return

    if crash_case == "persist_before_complete_reopen":
        runner1 = _CapturingRunner(result_payload=payload)
        executor1 = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner1)
        first = executor1.execute(effect, token, now=T0)
        assert isinstance(first, EffectSucceeded)
        assert runner1.invocations == 1
        runner2 = _CapturingRunner(result_payload={"title": "should-not-run"})
        executor2 = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner2)
        replay = executor2.execute(effect, token, now=T0)
        assert isinstance(replay, EffectSucceeded)
        assert runner2.invocations == 0
        return

    if crash_case == "pre_spawn_failure":
        runner = _PreSpawnFailureRunner()
        executor = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner)
        result = executor.execute(effect, token, now=T0)
        assert isinstance(result, (EffectRetryableFailure, EffectBlocked))
        assert_safe_fields_exclude_sentinels(result, forbidden)
        return

    if crash_case == "nonzero_exit":
        runner = _CapturingRunner(result_payload=payload, returncode=1)
        executor = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner)
        result = executor.execute(effect, token, now=T0)
        assert isinstance(result, (EffectRetryableFailure, EffectBlocked))
        assert_stdin_contains_sentinels(runner)
        assert_safe_fields_exclude_sentinels(result, forbidden)
        return

    if crash_case == "schema_mismatch":
        runner = _CapturingRunner(result_payload={"title": "only-title"})
        executor = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner)
        result = executor.execute(effect, token, now=T0)
        assert isinstance(result, (EffectRetryableFailure, EffectBlocked))
        assert_stdin_contains_sentinels(runner)
        assert_safe_fields_exclude_sentinels(result, forbidden)
        return

    if crash_case == "oversized_output":
        runner = _CapturingRunner(result_payload=payload, oversized=True)
        executor = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner)
        result = executor.execute(effect, token, now=T0)
        assert isinstance(result, (EffectRetryableFailure, EffectBlocked))
        assert_stdin_contains_sentinels(runner)
        assert_safe_fields_exclude_sentinels(result, forbidden)
        return

    if crash_case == "malformed_json":
        runner = _CapturingRunner(result_payload=payload, malformed=True)
        executor = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner)
        result = executor.execute(effect, token, now=T0)
        assert isinstance(result, (EffectRetryableFailure, EffectBlocked))
        assert_stdin_contains_sentinels(runner)
        assert_safe_fields_exclude_sentinels(result, forbidden)
        return

    if crash_case == "head_sha_drift" and effect_kind == "adjudication":
        drifted = effect.model_copy(update={"bound_head_sha": "c" * 40})
        runner = _CapturingRunner(result_payload=payload)
        executor = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner)
        result = executor.execute(drifted, token, now=T0)
        assert isinstance(result, (EffectRetryableFailure, EffectBlocked))
        assert runner.invocations == 0
        return

    if crash_case == "cycle_drift" and effect_kind == "adjudication":
        drifted = effect.model_copy(update={"cycle_number": 99})
        runner = _CapturingRunner(result_payload=payload)
        executor = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner)
        result = executor.execute(drifted, token, now=T0)
        assert isinstance(result, (EffectRetryableFailure, EffectBlocked))
        assert runner.invocations == 0
        return

    if crash_case == "thread_set_drift" and effect_kind == "adjudication":
        drifted = effect.model_copy(update={"frozen_thread_ids": ("PRRT_other",)})
        runner = _CapturingRunner(result_payload=payload)
        executor = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner)
        result = executor.execute(drifted, token, now=T0)
        assert isinstance(result, (EffectRetryableFailure, EffectBlocked))
        assert runner.invocations == 0
        return

    if crash_case == "execution_context_drift" and effect_kind == "adjudication":
        bad_ref = effect.execution_context_ref.model_copy(update={"sha256": "0" * 64})
        drifted = effect.model_copy(update={"execution_context_ref": bad_ref})
        runner = _CapturingRunner(result_payload=payload)
        executor = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner)
        result = executor.execute(drifted, token, now=T0)
        assert isinstance(result, (EffectRetryableFailure, EffectBlocked))
        assert runner.invocations == 0
        return

    if crash_case in {
        "head_sha_drift",
        "cycle_drift",
        "thread_set_drift",
        "execution_context_drift",
    }:
        if effect_kind == "publication":
            drifted = effect.model_copy(update={"bound_head_sha": "bad"})
            runner = _CapturingRunner(result_payload=payload)
            executor = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=runner)
            result = executor.execute(drifted, token, now=T0)
            assert isinstance(result, (EffectRetryableFailure, EffectBlocked))
            assert runner.invocations == 0
            return
        # adjudication-only cases handled above
        pytest.fail("unreachable adjudication drift branch")

    if crash_case == "active_stream_timeout":
        ready = tmp_path / "codex.ready"
        proceed = tmp_path / "proceed"
        bin_dir = tmp_path / "bin"
        codex_path = install_blocking_codex(bin_dir, ready_path=ready, proceed_path=proceed)
        runner = _BlockingCodexProcessRunner(
            codex_path=codex_path,
            ready_path=ready,
            proceed_path=proceed,
            result_payload=payload,
            timeout_seconds=0.05,
        )
        executor = make_executor(
            store, ctx_ref=holder.ctx_ref, process_runner=runner, timeout_seconds=0.05
        )
        result = executor.execute(effect, token, now=T0)
        assert isinstance(result, (EffectRetryableFailure, EffectBlocked))
        assert runner.invocations == 1
        assert_stdin_contains_sentinels(runner)
        assert_safe_fields_exclude_sentinels(result, forbidden)
        return

    if crash_case == "term_refusal_kill":
        import os

        from tests.integration.phase16_8_checkpoint_helpers import (
            assert_production_term_refusal_kill,
        )

        from ai_dev_loop.pr_review_v2.runtime_factory import ProcessCodexRunner

        ready = tmp_path / "term.ready"
        proceed = tmp_path / "never-proceed"
        bin_dir = tmp_path / "bin"
        codex_path = install_term_refusing_codex(bin_dir, ready_path=ready, proceed_path=proceed)
        assert_production_term_refusal_kill(
            codex_path=codex_path,
            ready_path=ready,
            artifact_root=store.root,
            run_id=RUN_ID,
            monkeypatch=monkeypatch,
        )
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        runner = ProcessCodexRunner(
            artifact_root=store.root,
            run_id=RUN_ID,
            term_grace_seconds=0.15,
        )
        executor = make_executor(
            store,
            ctx_ref=holder.ctx_ref,
            process_runner=runner,
            timeout_seconds=0.05,
        )
        result = executor.execute(effect, token, now=T0)
        assert isinstance(result, (EffectRetryableFailure, EffectBlocked))
        return

    if crash_case == "lease_loss":
        clock = FakeClock()
        engine_root = tmp_path / "engine-lease"
        engine_root.mkdir()
        repo = engine_root / "repo"
        repo.mkdir()
        engine, run_id, _store2, _ctx_ref = build_prepared_local_engine(
            engine_root, clock, repo_root=str(repo.resolve())
        )
        lease, claim = claim_next(engine, run_id)
        assert claim.classification == "local"
        clock.advance(timedelta(seconds=60))
        lease2 = engine.acquire_lease(run_id, "owner-b")
        assert engine.recover_expired_claims(run_id, "owner-b", lease2.generation) == 1
        status = engine.get_status(run_id)
        assert status.state_kind == "paused"
        assert status.next_action is NextActionCategory.INSPECT
        return

    if crash_case == "expired_claim_resume":
        clock = FakeClock()
        engine_root = tmp_path / "engine-expired"
        engine_root.mkdir()
        repo = engine_root / "repo"
        repo.mkdir()
        engine, run_id, store2, ctx_ref = build_prepared_local_engine(
            engine_root, clock, repo_root=str(repo.resolve())
        )
        runner = FakeCodexProcessRunner(result_payload=publication_payload())
        worker = build_local_worker(engine, store2, ctx_ref, runner, run_id=run_id)
        complete_publication_worker_step(engine, worker, run_id)
        assert runner.last_argv is not None
        assert SESSION in runner.last_argv
        replay = FakeCodexProcessRunner(result_payload={"title": "must-not-run"})
        worker2 = build_local_worker(engine, store2, ctx_ref, replay, run_id=run_id)
        step2 = worker2.run_once(run_id)
        assert step2.completed is True
        assert replay.last_argv is None
        return


def test_local_engine_claim_persist_before_complete_reopen(tmp_path: Path) -> None:
    clock = FakeClock()
    engine_root = tmp_path / "engine-reopen"
    engine_root.mkdir()
    repo = engine_root / "repo"
    repo.mkdir()
    db_path = engine_root / "engine.sqlite3"
    engine, run_id, store, ctx_ref = build_prepared_local_engine(
        engine_root, clock, repo_root=str(repo.resolve())
    )
    lease, claim = claim_next(engine, run_id)
    assert claim.effect.kind == "generate_publication_text"
    runner = FakeCodexProcessRunner(result_payload=publication_payload())
    executor = make_executor(store, ctx_ref=ctx_ref, process_runner=runner, run_id=run_id)
    event = executor.execute(claim.effect, claim.completion_token, now=clock.now())
    assert isinstance(event, EffectSucceeded)
    assert runner.last_argv is not None

    engine2 = reopen_engine(db_path, clock, prefix="p168-local-reopen")
    replay_runner = FakeCodexProcessRunner(result_payload={"title": "must-not-run"})
    executor2 = make_executor(store, ctx_ref=ctx_ref, process_runner=replay_runner, run_id=run_id)
    replay = executor2.execute(claim.effect, claim.completion_token, now=clock.now())
    assert isinstance(replay, EffectSucceeded)
    assert replay_runner.last_argv is None

    receipt = engine2.complete_claim(
        EffectCompletionRequest(
            submission_id="pub-complete-after-reopen",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id=lease.owner_id,
            lease_generation=lease.generation,
            event=event,
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED
    assert engine2.get_status(run_id).active_effect_kind == "commit_patch"


def test_local_executor_exact_session_and_protected_cache_replay(tmp_path: Path) -> None:
    store, _ctx, bundle_effect, token, holder = local_fixture(tmp_path, effect_kind="publication")
    fake = FakeCodexProcessRunner(result_payload=publication_payload())
    executor = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=fake)
    result = executor.execute(bundle_effect, token, now=T0)
    assert isinstance(result, EffectSucceeded)
    assert isinstance(result.outcome, PublicationTextPreparedOutcome)
    assert fake.last_argv is not None
    assert SESSION in fake.last_argv
    assert "--last" not in fake.last_argv

    fake2 = FakeCodexProcessRunner(result_payload={"title": "Other"})
    executor2 = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=fake2)
    again = executor2.execute(bundle_effect, token, now=T0)
    assert isinstance(again, EffectSucceeded)
    assert fake2.last_argv is None


def test_domain_invalid_adjudication_retries_then_blocks_at_limit(tmp_path: Path) -> None:
    """Gate B contradiction is retryable before exhaustion, blocked only at max_attempts."""
    store, _ctx, effect, token, holder = local_fixture(tmp_path, effect_kind="adjudication")
    reply = "SENSITIVE_REPLY_BODY_DO_NOT_LEAK"
    summary = "SENSITIVE_ADJUDICATION_SUMMARY_DO_NOT_LEAK"
    fix_prompt = "SENSITIVE_FIX_PROMPT_DO_NOT_LEAK"
    contradictory = {
        "decisions": [
            {
                "thread_id": effect.frozen_thread_ids[0],
                "decision": "actionable",
                "safe_summary": summary,
                "reply_body": reply,
            }
        ],
        "fix_prompt_text": fix_prompt,
    }
    forbidden = (reply, summary, fix_prompt, SESSION, "forbids reply_body", "input_value")
    domain_message = "codex adjudication result failed domain validation"

    early = effect.model_copy(update={"attempt": 1})
    assert early.attempt < early.max_attempts
    early_fake = FakeCodexProcessRunner(result_payload=contradictory)
    early_result = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=early_fake).execute(
        early, token, now=T0
    )
    assert isinstance(early_result, EffectRetryableFailure)
    assert early_result.error.safe_summary == domain_message
    assert early_result.failed_attempt == 1
    assert store.read_cached_external_adjudication(early) is None
    assert_safe_fields_exclude_sentinels(early_result, forbidden)
    assert early_fake.last_argv is not None
    assert SESSION in early_fake.last_argv
    assert "--last" not in early_fake.last_argv
    assert early_fake.last_stdin is not None
    assert 'decision == "actionable", reply_body must be null' in early_fake.last_stdin

    exhausted = effect.model_copy(update={"attempt": effect.max_attempts})
    assert exhausted.attempt >= exhausted.max_attempts
    late_fake = FakeCodexProcessRunner(result_payload=contradictory)
    blocked = make_executor(store, ctx_ref=holder.ctx_ref, process_runner=late_fake).execute(
        exhausted, token, now=T0
    )
    assert isinstance(blocked, EffectBlocked)
    assert blocked.safe_summary == domain_message
    assert blocked.safe_summary != "local effect failed"
    assert store.read_cached_external_adjudication(exhausted) is None
    assert_safe_fields_exclude_sentinels(blocked, forbidden)


def test_publication_runner_blocking_codex_subprocess_ipc(tmp_path: Path) -> None:
    from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import PublicationTextRunner

    store, ctx, effect, _token, _holder = local_fixture(tmp_path, effect_kind="publication")
    ready = tmp_path / "codex.ready"
    proceed = tmp_path / "proceed"
    bin_dir = tmp_path / "bin"
    codex_path = install_blocking_codex(bin_dir, ready_path=ready, proceed_path=proceed)
    runner = _BlockingCodexProcessRunner(
        codex_path=codex_path,
        ready_path=ready,
        proceed_path=proceed,
        result_payload=publication_payload(),
        timeout_seconds=30,
        release_after_ready=True,
    )
    pub = PublicationTextRunner(
        artifact_root=tmp_path / "artifacts",
        process_runner=runner,
        timeout_seconds=30,
    )
    artifact = store.persist_patch_bytes(run_id=RUN_ID, data=b"diff --git a/x b/x\n")
    result = pub.generate(
        run_id=RUN_ID,
        session_id=SESSION,
        repo_root=ctx.repository_root,
        execution_context=ctx,
        evidence_ref=effect.evidence_ref,
        patch_ref=artifact,
        effect=effect,
    )
    assert result.title == "Publication title"
    assert runner.invocations == 1
    assert runner.last_argv is not None
    assert SESSION in runner.last_argv
    assert ready.is_file() or proceed.is_file()
