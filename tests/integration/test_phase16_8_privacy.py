"""Phase 16.8 privacy, corruption, and redaction sweep across v2 durable surfaces."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.integration.phase16_4_matrix_helpers import (
    claim_next,
    complete_ok,
    make_engine,
    make_observe_eligible,
)
from tests.integration.phase16_8_checkpoint_helpers import sqlite_table_dump
from tests.integration.phase16_8_helpers import (
    SESSION,
    SHA_B,
    assemble_read_stack,
    scan_text_surfaces,
)
from tests.integration.phase16_8_local_engine_helpers import (
    make_executor,
    publication_payload,
)
from tests.integration.stateful_fake_gh import StatefulFakeGhController
from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.github_read_helpers import policy
from tests.unit.pr_review_v2.helpers import HASH_2, artifact

from ai_dev_loop.pr_review_v2.application.contracts import EventSubmission
from ai_dev_loop.pr_review_v2.application.control import ControlPlaneService
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
from ai_dev_loop.pr_review_v2.application.preparation import PreparationService, SourceRunSnapshot
from ai_dev_loop.pr_review_v2.domain import (
    AdjudicationDecisionKind,
    AdjudicationEvidence,
    AdjudicationRecordedOutcome,
    CommitRecordedOutcome,
    EffectSucceeded,
    EligibleThreadsObservedOutcome,
    FrozenThreadSet,
    PrBoundOutcome,
    PullRequestBinding,
    PushConfirmedOutcome,
    ReviewTriggerConfirmedOutcome,
    StartRequested,
    ThreadDecisionRecord,
    TriggerEvidence,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    PublicationTextPreparedOutcome,
    ThreadReplyConfirmedOutcome,
)
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
from ai_dev_loop.pr_review_v2.infrastructure.paths import run_artifact_root
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.workers.owned_children import OwnedChildStore, register_owned_child
from ai_dev_loop.pr_review_v2.workers.supervisor import (
    SupervisorLauncherMetadata,
    SupervisorLauncherStore,
)
from ai_dev_loop.state import sha256_bytes

SECRET = "ghp_super_secret_token_do_not_leak"
CHAT_FULL = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SESSION_FULL = SESSION
PROMPT_SENTINEL = "SENTINEL_PROMPT_BODY_DO_NOT_LEAK_987654321"
PATCH_SENTINEL = "SENTINEL_PATCH_BYTES_DO_NOT_LEAK_abcdef"
STDERR_SENTINEL = "SENTINEL_STDERR_STREAM_DO_NOT_LEAK"
ENV_SENTINEL = "SENTINEL_ENV_VALUE_DO_NOT_LEAK"
LAUNCHER_TOKEN = "launcher-token-sentinel-do-not-leak"
STDOUT_SENTINEL = "SENTINEL_STDOUT_STREAM_DO_NOT_LEAK"
REPLY_SENTINEL = "SENTINEL_REPLY_BODY_DO_NOT_LEAK"
COMMIT_MSG_SENTINEL = "SENTINEL_COMMIT_MESSAGE_DO_NOT_LEAK"

ALL_FORBIDDEN = (
    SECRET,
    CHAT_FULL,
    SESSION_FULL,
    PROMPT_SENTINEL,
    PATCH_SENTINEL,
    STDERR_SENTINEL,
    ENV_SENTINEL,
    LAUNCHER_TOKEN,
    STDOUT_SENTINEL,
    REPLY_SENTINEL,
    COMMIT_MSG_SENTINEL,
)


@pytest.fixture(autouse=True)
def _native_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("TMP", "/tmp")
    monkeypatch.setenv("TEMP", "/tmp")


def _execution_context_with_sentinels(*, repo_root: str) -> ExecutionContextArtifact:
    plan_bytes = f"plan {PROMPT_SENTINEL}\n".encode()
    prompt_bytes = f"{PROMPT_SENTINEL}\n".encode()
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from="source_run",
            source_run_id="src-privacy",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha="a" * 40,
        ),
        cursor=ExecutionContextCursor(
            chat_id=CHAT_FULL,
            model="composer-2.5-fast",
            command="agent",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        codex=ExecutionContextCodex(
            session_id=SESSION_FULL,
            review_model="gpt-5",
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
            gh_command="gh",
            git_command="git",
            ssh_command="ssh",
            remote_name="origin",
            reviewer_logins=("chatgpt-codex-connector",),
            review_trigger_body="@codex review",
            user_mention="rojobad",
            poll_interval_seconds=60,
            max_external_cycles=8,
            per_call_timeout_seconds=60,
            overall_timeout_seconds=180,
            max_pages=20,
            max_items=500,
            max_server_directed_wait_seconds=3600,
            no_findings_enabled=False,
            worker=ExecutionContextWorker(
                lease_ttl_seconds=30,
                heartbeat_interval_seconds=10,
                idle_poll_seconds=1,
            ),
        ),
        plan_prompt=ExecutionContextPlanPrompt(
            plan_path="plans/x.md",
            plan_sha256=sha256_bytes(plan_bytes),
            prompt_path="prompts/prompt.txt",
            prompt_sha256=sha256_bytes(prompt_bytes),
        ),
        repository_root=repo_root,
    )


class _SentinelCapturingRunner:
    def __init__(self, *, payload: dict[str, object]) -> None:
        self.payload = payload
        self.last_stdin: str | None = None
        self.last_argv: list[str] | None = None

    def run(
        self,
        argv: list[str],
        *,
        cwd: str,
        stdin_text: str,
        timeout_seconds: float,
    ):
        del cwd, timeout_seconds
        assert os.environ.get("SENSITIVE_ENV_PROBE") == ENV_SENTINEL
        self.last_argv = list(argv)
        self.last_stdin = f"{stdin_text}\n{PROMPT_SENTINEL}\n{SESSION_FULL}\n{STDOUT_SENTINEL}\n"
        from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import CodexProcessResult

        if "--output-last-message" not in argv:
            raise AssertionError("missing --output-last-message")
        index = argv.index("--output-last-message")
        result_path = Path(argv[index + 1])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(self.payload), encoding="utf-8")
        return CodexProcessResult(
            returncode=0,
            stdout_bytes=STDOUT_SENTINEL.encode(),
            stderr_bytes=STDERR_SENTINEL.encode(),
        )


def test_persistent_run_operational_surfaces_redact_all_sentinels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end on one prepared run: LOCAL claim, observe, control output, and metadata scans."""

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SENSITIVE_ENV_PROBE", ENV_SENTINEL)
    clock = FakeClock()
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "engine.sqlite3"
    engine = make_engine(db_path, clock, prefix="p168-privacy-e2e")
    arts = ProtectedResultStore(tmp_path / "artifacts")
    ctx = _execution_context_with_sentinels(repo_root=str(repo.resolve()))
    plan_bytes = f"plan {PROMPT_SENTINEL}\n".encode()
    prompt_bytes = f"{PROMPT_SENTINEL}\n".encode()
    patch_bytes = PATCH_SENTINEL.encode()

    prep = PreparationService(engine, arts, clock=clock)
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-privacy",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha="a" * 40,
            accepted_patch_bytes=patch_bytes,
            plan_bytes=plan_bytes,
            prompt_bytes=prompt_bytes,
            execution_context=ctx,
        )
    )
    run_id = created.run_id
    engine.apply_event(
        EventSubmission(
            submission_id="start",
            run_id=run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    with engine.store.begin_read() as conn:
        prepared_state, _version, _updated = engine.store.load_validated_snapshot(conn, run_id)
    ctx_ref = prepared_state.origin.execution_context_ref

    pub_payload = publication_payload()
    pub_payload["commit_subject"] = COMMIT_MSG_SENTINEL
    pub_payload["commit_body"] = f"body includes {COMMIT_MSG_SENTINEL}"
    runner = _SentinelCapturingRunner(payload=pub_payload)
    executor = make_executor(arts, ctx_ref=ctx_ref, process_runner=runner, run_id=run_id)
    pub_lease, pub_claim = claim_next(engine, run_id)
    assert pub_claim.effect.kind == "generate_publication_text"
    pub_event = executor.execute(pub_claim.effect, pub_claim.completion_token, now=clock.now())
    assert isinstance(pub_event, EffectSucceeded)
    assert isinstance(pub_event.outcome, PublicationTextPreparedOutcome)
    assert PROMPT_SENTINEL in (runner.last_stdin or "")
    reader = InputArtifactReader(arts.root)
    commit_msg = reader.read_commit_message(run_id=run_id, ref=pub_event.outcome.commit_message_ref)
    assert COMMIT_MSG_SENTINEL in commit_msg.subject
    assert COMMIT_MSG_SENTINEL in commit_msg.body
    complete_ok(engine, pub_lease, pub_claim, "pub-complete", pub_event)

    def _drive_after_publication() -> PullRequestBinding:
        def step(sub_id: str, factory):
            lease, claim = claim_next(engine, run_id)
            complete_ok(engine, lease, claim, sub_id, factory(claim))
            return claim

        step(
            "g2",
            lambda c: EffectSucceeded(
                occurred_at=clock.now(),
                token=c.completion_token,
                outcome=CommitRecordedOutcome(
                    commit_sha=SHA_B,
                    new_head_sha=SHA_B,
                    expected_remote_sha_before_push=None,
                ),
            ),
        )
        step(
            "g3",
            lambda c: EffectSucceeded(
                occurred_at=clock.now(),
                token=c.completion_token,
                outcome=PushConfirmedOutcome(commit_sha=SHA_B, remote_ref="feature"),
            ),
        )
        binding = PullRequestBinding(
            repository=prepared_state.origin.repository,
            pr_number=7,
            head_branch="feature",
            base_branch="main",
            head_sha=SHA_B,
        )
        step(
            "g4",
            lambda c: EffectSucceeded(
                occurred_at=clock.now(),
                token=c.completion_token,
                outcome=PrBoundOutcome(binding=binding),
            ),
        )
        step(
            "g5",
            lambda c: EffectSucceeded(
                occurred_at=clock.now(),
                token=c.completion_token,
                outcome=ReviewTriggerConfirmedOutcome(
                    evidence=TriggerEvidence(
                        marker=c.effect.marker,
                        comment_ref=artifact("artifacts/trigger.json", HASH_2),
                        head_sha=binding.head_sha,
                    )
                ),
            ),
        )
        return binding

    _drive_after_publication()
    make_observe_eligible(engine, run_id, clock)
    lease, claim = claim_next(engine, run_id)
    assert claim.effect.kind == "observe_bot_review"
    assert claim.effect.trigger_marker is not None

    controller = StatefulFakeGhController(tmp_path / "gh-state.json")
    controller.seed_waiting_observation(
        marker=claim.effect.trigger_marker,
        head_sha=SHA_B,
    )
    controller.state.fixture["threads"] = [
        {
            "id": "PRRT_1",
            "isResolved": False,
            "comments": {
                "nodes": [
                    {
                        "id": "RC_1",
                        "body": f"fix auth token={SECRET}",
                        "createdAt": "2026-07-21T12:02:00Z",
                        "author": {"login": "chatgpt-codex-connector"},
                        "commit": {"oid": SHA_B},
                    }
                ]
            },
        }
    ]
    controller.state.save(controller.state_path)

    launchers = SupervisorLauncherStore(arts.root)
    launchers.write(
        SupervisorLauncherMetadata(
            schema_version=1,
            run_id=run_id,
            token=LAUNCHER_TOKEN,
            pid=os.getpid(),
            pgid=os.getpid(),
            process_start_time=str(os.getpid()),
            executable="/bin/false",
            created_at=clock.now().isoformat(),
        )
    )
    children = OwnedChildStore(arts.root)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        time.sleep(0.05)
        register_owned_child(
            children,
            run_id=run_id,
            component="codex",
            pid=proc.pid,
            pgid=os.getpgid(proc.pid),
            executable=str(Path(sys.executable).resolve()),
        )
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), 9)
            proc.wait(timeout=2)

    _, read_executor = assemble_read_stack(tmp_path, controller, policy=policy(gh_command="gh"))
    event = read_executor.execute(claim.effect, claim.completion_token, now=clock.now())
    assert event.kind == "effect_succeeded", getattr(event, "safe_summary", event)
    assert event.outcome.kind == "eligible_threads_observed"
    complete_ok(
        engine,
        lease,
        claim,
        "obs-complete",
        EffectSucceeded(
            occurred_at=clock.now(),
            token=claim.completion_token,
            outcome=EligibleThreadsObservedOutcome(
                frozen=FrozenThreadSet(
                    thread_ids=("PRRT_1",),
                    snapshot_ref=event.outcome.frozen.snapshot_ref,
                    head_sha=SHA_B,
                    cycle_number=1,
                    trigger_marker=claim.effect.trigger_marker,
                )
            ),
        ),
    )

    reply_ref = arts.persist_reply_text(
        run_id=run_id,
        relative_hint="reply-sentinel.txt",
        text=f"{REPLY_SENTINEL}\n",
    )
    lease, adj_claim = claim_next(engine, run_id)
    assert adj_claim.effect.kind == "adjudicate_threads"
    complete_ok(
        engine,
        lease,
        adj_claim,
        "adj-complete",
        EffectSucceeded(
            occurred_at=clock.now(),
            token=adj_claim.completion_token,
            outcome=AdjudicationRecordedOutcome(
                evidence=AdjudicationEvidence(
                    frozen=FrozenThreadSet(
                        thread_ids=("PRRT_1",),
                        snapshot_ref=event.outcome.frozen.snapshot_ref,
                        head_sha=SHA_B,
                        cycle_number=1,
                        trigger_marker=claim.effect.trigger_marker,
                    ),
                    decisions=(
                        ThreadDecisionRecord(
                            thread_id="PRRT_1",
                            decision=AdjudicationDecisionKind.NOT_APPLICABLE,
                            safe_summary="reply PRRT_1",
                            reply_ref=reply_ref,
                        ),
                    ),
                    result_ref=artifact("artifacts/adjudication.json"),
                    fix_prompt_ref=None,
                )
            ),
        ),
    )
    lease, reply_claim = claim_next(engine, run_id)
    assert reply_claim.effect.kind == "post_thread_reply"
    assert reply_claim.effect.reply_ref == reply_ref
    reply_text = reader.read_reply_text(run_id=run_id, ref=reply_claim.effect.reply_ref)
    assert REPLY_SENTINEL in reply_text
    complete_ok(
        engine,
        lease,
        reply_claim,
        "reply-complete",
        EffectSucceeded(
            occurred_at=clock.now(),
            token=reply_claim.completion_token,
            outcome=ThreadReplyConfirmedOutcome(
                thread_id=reply_claim.effect.thread_id,
                reply_ref=reply_claim.effect.reply_ref,
            ),
        ),
    )

    control = ControlPlaneService(
        engine,
        artifact_store=arts,
        launcher_store=launchers,
        spawner=lambda _rid: "spawned",
    )
    status_text = json.dumps(control.status(run_id).model_dump(mode="json"), default=str)
    history_text = json.dumps(
        control.history(run_id, limit=50).model_dump(mode="json"), default=str
    )
    sqlite_text = sqlite_table_dump(db_path)
    abort_text = json.dumps(control.abort(run_id).model_dump(mode="json"), default=str)

    operational_surfaces = [
        status_text,
        history_text,
        sqlite_text,
        abort_text,
    ]
    for path in (tmp_path / "xdg").rglob("*"):
        if path.is_file():
            try:
                operational_surfaces.append(path.read_text(encoding="utf-8"))
            except UnicodeDecodeError:
                continue
    for path in arts.root.rglob("observations/**/*.json"):
        operational_surfaces.append(path.read_text(encoding="utf-8"))
    for path in arts.root.rglob("local/owned-children/**/*.json"):
        operational_surfaces.append(path.read_text(encoding="utf-8"))
    for path in (tmp_path / "xdg").rglob("logs/**/*.log"):
        if path.is_file():
            operational_surfaces.append(path.read_text(encoding="utf-8"))

    combined = "\n".join(operational_surfaces)
    for needle in ALL_FORBIDDEN:
        assert needle not in combined, f"{needle!r} leaked into operational surfaces"

    launcher_path = launchers.path_for(run_id)
    assert launcher_path.is_file(), "expected owner-only launcher metadata artifact"
    launcher_raw = launcher_path.read_text(encoding="utf-8")
    assert LAUNCHER_TOKEN in launcher_raw
    if hasattr(os, "stat"):
        launcher_mode = stat.S_IMODE(launcher_path.stat().st_mode)
        assert launcher_mode == stat.S_IRUSR | stat.S_IWUSR

    ctx_path = run_artifact_root(arts.root, run_id) / ctx_ref.relative_path
    assert ctx_path.is_file()
    ctx_raw = ctx_path.read_text(encoding="utf-8")
    assert CHAT_FULL in ctx_raw
    assert SESSION_FULL in ctx_raw
    patch_ref = prepared_state.origin.accepted_patch
    patch_path = run_artifact_root(arts.root, run_id) / patch_ref.relative_path
    if not patch_path.is_file():
        patch_path = next(run_artifact_root(arts.root, run_id).rglob("*.patch"))
    patch_raw = patch_path.read_bytes()
    assert PATCH_SENTINEL in patch_raw.decode("utf-8", errors="replace")
    assert hashlib.sha256(patch_raw).hexdigest() == patch_ref.sha256

    reply_path = run_artifact_root(arts.root, run_id) / reply_ref.relative_path
    assert reply_path.is_file()
    assert REPLY_SENTINEL in reply_path.read_text(encoding="utf-8")
    if hasattr(os, "stat"):
        mode = stat.S_IMODE(reply_path.stat().st_mode)
        assert mode == stat.S_IRUSR | stat.S_IWUSR
    commit_path = (
        run_artifact_root(arts.root, run_id) / pub_event.outcome.commit_message_ref.relative_path
    )
    assert commit_path.is_file()
    assert COMMIT_MSG_SENTINEL in commit_path.read_text(encoding="utf-8")
    observation_files = list(run_artifact_root(arts.root, run_id).rglob("observations/**/*.json"))
    assert observation_files, "expected bounded observation artifact"
    if hasattr(os, "stat"):
        for path in observation_files:
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_permitted_sensitive_artifacts_are_owner_only_and_hash_verified(
    tmp_path: Path,
) -> None:
    store = ProtectedResultStore(tmp_path / "artifacts")
    data = PATCH_SENTINEL.encode()
    ref = store.persist_patch_bytes(run_id="run-perm", data=data)
    path = run_artifact_root(store.root, "run-perm") / ref.relative_path
    assert path.is_file()
    raw = path.read_bytes()
    assert raw == data
    assert hashlib.sha256(raw).hexdigest() == ref.sha256
    if hasattr(os, "stat"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == stat.S_IRUSR | stat.S_IWUSR

    scan_text_surfaces(tmp_path / "artifacts", forbidden=(SECRET, LAUNCHER_TOKEN))
