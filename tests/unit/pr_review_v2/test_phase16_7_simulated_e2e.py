"""Phase 16.7 simulated multi-round orchestration (injected fakes only).

Drives create/prepare → start → LOCAL publication/adjudication/fix through the
real EffectWorker/complete_claim fence, with scripted GitHub read/write outcomes.
No real network, GitHub, Cursor, Codex, commit, or push.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.helpers import HASH_1, HASH_2, SHA_A, SHA_B

from ai_dev_loop.local_review_loop import LocalReviewFixResult, LocalReviewOutcome
from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EventDisposition,
    EventSubmission,
)
from ai_dev_loop.pr_review_v2.application.control import ControlPlaneService
from ai_dev_loop.pr_review_v2.application.control_contracts import (
    AbortProcessAction,
    OriginKind,
    SafeNextAction,
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
from ai_dev_loop.pr_review_v2.application.github_read import (
    ObservationEvidenceKind,
    ObservationSnapshot,
    ObservedReviewThread,
    ObservedTriggerComment,
)
from ai_dev_loop.pr_review_v2.application.preparation import (
    ExistingPrSnapshot,
    PreparationService,
    SourceRunSnapshot,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    ArtifactRef,
    EffectClassification,
    EffectCompletionToken,
    FrozenThreadSet,
    PauseReasonKind,
    PullRequestBinding,
    RepositoryIdentity,
    SafeAction,
    SafeActionKind,
    TriggerEvidence,
    VerifiedNoFindingsEvidence,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    CommitPatchEffect,
    CreateOrUpdatePrEffect,
    ObserveBotReviewEffect,
    PrReviewEffect,
    PushCommitEffect,
    RequestBotReviewEffect,
    ResolveThreadEffect,
    UpdatePrTextEffect,
    classify_effect,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    CommitRecordedOutcome,
    EffectBlocked,
    EffectSucceeded,
    EligibleThreadsObservedOutcome,
    PrBoundOutcome,
    PrTextUpdatedOutcome,
    PushConfirmedOutcome,
    ReviewTriggerConfirmedOutcome,
    StartRequested,
    ThreadResolutionConfirmedOutcome,
    VerifiedNoFindingsOutcome,
)
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    FakeCodexProcessRunner,
    PublicationTextRunner,
    ThreadAdjudicationRunner,
)
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import LocalFixAdapter
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import (
    ReviewArtifactStore,
    sha256_text,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker
from ai_dev_loop.pr_review_v2.workers.local_executor import (
    LocalEffectExecutor,
    StoreBackedContextResolver,
)
from ai_dev_loop.pr_review_v2.workers.supervisor import (
    PrReviewV2Supervisor,
    SupervisorLauncherMetadata,
    SupervisorLauncherStore,
)

PLAN_BYTES = b"frozen-plan-for-tests\n"
PROMPT_BYTES = b"frozen-prompt-for-tests\n"
PLAN_SHA = hashlib.sha256(PLAN_BYTES).hexdigest()
PROMPT_SHA = hashlib.sha256(PROMPT_BYTES).hexdigest()
T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
SESSION = "11111111-1111-1111-1111-111111111111"
CHAT = "chat-continuity-1"
SHA_C = "c" * 40
THREAD_ID = "PRRT_thread_1"


@dataclass
class HeadTracker:
    sha: str = SHA_A

    def get(self, _repo_root: str = "") -> str:
        return self.sha

    def set(self, value: str) -> None:
        self.sha = value


class _BlockUntilStopWait:
    def __init__(self) -> None:
        self._ev = threading.Event()

    def wait(self, timeout: float) -> bool:
        return self._ev.wait(timeout=timeout)

    def wake_for_stop(self) -> None:
        self._ev.set()


class _FakeCarrier:
    def __init__(self, heads: HeadTracker) -> None:
        self._heads = heads
        self.seeded: list[str] = []
        self.seeds: list = []

    def ensure_seeded_carrier(self, *, carrier_run_id: str, seed) -> None:
        self.seeded.append(carrier_run_id)
        self.seeds.append(seed)

    def carrier_exists(self, carrier_run_id: str) -> bool:
        return carrier_run_id in self.seeded

    def carrier_has_progress(self, carrier_run_id: str) -> bool:
        # Simulated e2e treats a seeded carrier as brand-new until a later resume.
        return False

    def current_head_sha(self, repo_root: str) -> str:
        return self._heads.get(repo_root)

    def read_verified_staged_patch_bytes(self, carrier_run_id: str, relative_path: str) -> bytes:
        del carrier_run_id, relative_path
        return b"diff --git a/fix.py b/fix.py\n+fixed\n"

    def read_terminal_acceptance(self, carrier_run_id: str, *, expected_session_id: str):
        del carrier_run_id, expected_session_id
        return None

    def verify_carrier_bindings(self, *, carrier_run_id: str, seed) -> None:
        del carrier_run_id, seed


@dataclass
class ScriptedSideExecutor:
    """Scripted READ/MUTATING/RECONCILE outcomes; records write intents."""

    store: ProtectedResultStore
    heads: HeadTracker
    clock: FakeClock
    binding: PullRequestBinding | None = None
    observe_cycle_seen: set[int] = field(default_factory=set)
    write_kinds: list[str] = field(default_factory=list)
    write_idempotency_keys: list[str] = field(default_factory=list)
    write_effect_ids: list[str] = field(default_factory=list)
    requires_authority: bool = True
    actionable_cycles: set[int] = field(default_factory=lambda: {1})

    def execute(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
        authority=None,
        claim=None,
    ) -> EffectSucceeded:
        del authority, claim
        classification = classify_effect(effect)
        if classification is EffectClassification.MUTATING:
            self.write_kinds.append(effect.kind)
            self.write_idempotency_keys.append(effect.idempotency_key)
            self.write_effect_ids.append(effect.effect_id)
        outcome = self._outcome_for(effect)
        return EffectSucceeded(occurred_at=now, token=token, outcome=outcome)

    def _outcome_for(self, effect: PrReviewEffect):
        if isinstance(effect, CommitPatchEffect):
            nxt = SHA_B if effect.expected_head_sha == SHA_A else SHA_C
            if nxt == effect.expected_head_sha:
                nxt = "d" * 40
            self.heads.set(nxt)
            return CommitRecordedOutcome(
                commit_sha=nxt, new_head_sha=nxt, expected_remote_sha_before_push=None
            )
        if isinstance(effect, PushCommitEffect):
            return PushConfirmedOutcome(commit_sha=effect.commit_sha, remote_ref=effect.remote_ref)
        if isinstance(effect, CreateOrUpdatePrEffect):
            self.binding = PullRequestBinding(
                repository=effect.repository,
                pr_number=7,
                head_branch=effect.head_branch,
                base_branch=effect.base_branch,
                head_sha=self.heads.sha,
            )
            return PrBoundOutcome(binding=self.binding)
        if isinstance(effect, RequestBotReviewEffect):
            return ReviewTriggerConfirmedOutcome(
                evidence=TriggerEvidence(
                    marker=effect.marker,
                    comment_ref=ArtifactRef(relative_path="local/trigger.json", sha256=HASH_1),
                    head_sha=effect.bound_head_sha,
                )
            )
        if isinstance(effect, ObserveBotReviewEffect):
            assert effect.trigger_marker is not None
            # Configured cycles emit actionable threads → LOCAL adjudication/fix.
            # Other cycles: verified no-findings completion.
            if (
                effect.cycle_number in self.actionable_cycles
                and effect.cycle_number not in self.observe_cycle_seen
            ):
                self.observe_cycle_seen.add(effect.cycle_number)
                assert self.binding is not None
                snapshot = ObservationSnapshot(
                    binding=self.binding.model_copy(update={"head_sha": effect.bound_head_sha}),
                    cycle_number=effect.cycle_number,
                    poll_sequence=1,
                    trigger_marker=effect.trigger_marker,
                    observed_at=self.clock.now(),
                    evidence_kind=ObservationEvidenceKind.ELIGIBLE_THREADS,
                    trigger=ObservedTriggerComment(
                        comment_id=f"trig-{effect.cycle_number}",
                        author_login="orchestrator",
                        created_at=self.clock.now(),
                        body_sha256=sha256_text(effect.trigger_marker),
                    ),
                    eligible_threads=(
                        ObservedReviewThread(
                            thread_id=THREAD_ID,
                            is_resolved=False,
                            author_login="reviewer",
                            created_at=self.clock.now(),
                            commit_sha=effect.bound_head_sha,
                            root_comment_id=f"rc-{effect.cycle_number}",
                            root_body_sha256=sha256_text(
                                f"fix the auth check cycle {effect.cycle_number}"
                            ),
                            sanitized_root_body=f"fix the auth check cycle {effect.cycle_number}",
                        ),
                    ),
                )
                ref = ReviewArtifactStore(self.store.root).persist_observation_for_run(
                    run_id=effect.run_id, snapshot=snapshot
                )
                return EligibleThreadsObservedOutcome(
                    frozen=FrozenThreadSet(
                        thread_ids=(THREAD_ID,),
                        snapshot_ref=ref,
                        head_sha=effect.bound_head_sha,
                        cycle_number=effect.cycle_number,
                        trigger_marker=effect.trigger_marker,
                    )
                )
            self.observe_cycle_seen.add(effect.cycle_number)
            return VerifiedNoFindingsOutcome(
                evidence=VerifiedNoFindingsEvidence(
                    head_sha=effect.bound_head_sha,
                    observation_ref=ArtifactRef(
                        relative_path="local/no-findings.json", sha256=HASH_2
                    ),
                    verified_at=self.clock.now(),
                )
            )
        if isinstance(effect, UpdatePrTextEffect):
            return PrTextUpdatedOutcome(
                binding=effect.binding,
                publication_text_ref=effect.publication_text_ref,
            )
        if isinstance(effect, ResolveThreadEffect):
            return ThreadResolutionConfirmedOutcome(thread_id=effect.thread_id)
        raise AssertionError(f"unscripted effect kind {effect.kind}")


class HybridExecutor:
    """LOCAL → LocalEffectExecutor; everything else → ScriptedSideExecutor."""

    requires_authority = True

    def __init__(self, local: LocalEffectExecutor, side: ScriptedSideExecutor) -> None:
        self._local = local
        self._side = side

    def execute(self, effect, token, *, now, authority=None, claim=None):
        if classify_effect(effect) is EffectClassification.LOCAL:
            return self._local.execute(effect, token, now=now, authority=authority, claim=claim)
        return self._side.execute(effect, token, now=now, authority=authority, claim=claim)


def _ctx(
    *,
    prepared_from: str,
    source_run_id: str | None,
    head_sha: str,
    repository: str = "acme/demo",
    head_branch: str = "feature",
    repo_root: str = "/tmp/repo",
    chat_id: str | None = CHAT,
) -> ExecutionContextArtifact:
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from=prepared_from,
            source_run_id=source_run_id,
            repository=repository,
            head_branch=head_branch,
            base_branch="main",
            expected_head_sha=head_sha,
        ),
        cursor=ExecutionContextCursor(
            chat_id=chat_id,
            model="composer-2.5-fast",
            command="agent",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        codex=ExecutionContextCodex(
            session_id=SESSION,
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
            no_findings_enabled=True,
            no_findings_prefixes=("No findings",),
            no_findings_prefix_length=12,
            worker=ExecutionContextWorker(
                lease_ttl_seconds=30,
                heartbeat_interval_seconds=10,
                idle_poll_seconds=1,
            ),
        ),
        plan_prompt=ExecutionContextPlanPrompt(
            plan_path="plans/x.md",
            plan_sha256=PLAN_SHA,
            prompt_path="prompts/prompt.txt",
            prompt_sha256=PROMPT_SHA,
            accepted_patch_sha256=HASH_1,
        ),
        repository_root=repo_root,
    )


def _origin_context_ref(engine: PrReviewEngine, run_id: str) -> ArtifactRef:
    with engine.store.begin_read() as conn:
        state, _version, _updated = engine.store.load_validated_snapshot(conn, run_id)
    return state.origin.execution_context_ref


def _build_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, heads: HeadTracker):
    clock = FakeClock(T0)
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "engine.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="e2e"),
        lease_ttl=timedelta(seconds=60),
    )
    arts = ProtectedResultStore(tmp_path / "artifacts")
    carrier = _FakeCarrier(heads)
    monkeypatch.setattr(
        "ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter.run_local_review_fix",
        lambda request: LocalReviewFixResult(
            run_id=request.run_id,
            status="completed",
            chat_id=CHAT,
            iteration_count=1,
            latest_staged_diff_path="git/diffs/01.patch",
            latest_review_path=None,
            result_message="ok",
            outcome=LocalReviewOutcome.ACCEPTED,
            needs_external_continuation=True,
        ),
    )
    fake_codex = FakeCodexProcessRunner(
        result_payloads=[
            {
                "title": "Initial",
                "body": "Body",
                "commit_subject": "Initial subject",
                "commit_body": "Initial body",
            },
            {
                "decisions": [
                    {
                        "thread_id": THREAD_ID,
                        "decision": "actionable",
                        "safe_summary": "needs fix",
                        "reply_body": None,
                    }
                ],
                "fix_prompt_text": "Please fix thread PRRT_thread_1",
            },
            {
                "title": "Fix",
                "body": "Fix body",
                "commit_subject": "Fix subject",
                "commit_body": "Fix body",
            },
        ]
    )
    pub = PublicationTextRunner(
        artifact_root=arts.root, process_runner=fake_codex, timeout_seconds=30
    )
    adj = ThreadAdjudicationRunner(
        artifact_root=arts.root, process_runner=fake_codex, timeout_seconds=30
    )
    adapter = LocalFixAdapter(runtime=carrier, store=arts)
    side = ScriptedSideExecutor(store=arts, heads=heads, clock=clock)
    local = LocalEffectExecutor(
        store=arts,
        publication_runner=pub,
        adjudication_runner=adj,
        local_fix_adapter=adapter,
        context_resolver=StoreBackedContextResolver(arts, ref_for_run={}),
    )
    hybrid = HybridExecutor(local, side)
    worker = EffectWorker(
        engine,
        hybrid,
        owner_id="owner-e2e",
        heartbeat_interval=timedelta(seconds=30),
        heartbeat_interval_wait=_BlockUntilStopWait(),
    )
    launchers = SupervisorLauncherStore(arts.root)
    control = ControlPlaneService(
        engine,
        artifact_store=arts,
        launcher_store=launchers,
        spawner=lambda _rid: "spawned",
    )
    prep = PreparationService(engine, arts, clock=clock)
    return clock, engine, arts, prep, control, worker, hybrid, fake_codex, local, carrier


def _wire_context_resolver(local: LocalEffectExecutor, run_id: str, ref: ArtifactRef) -> None:
    local._context_resolver = StoreBackedContextResolver(  # noqa: SLF001
        local._store,  # noqa: SLF001
        ref_for_run={run_id: ref},
    )


def _drive_to_completed(
    engine: PrReviewEngine, worker: EffectWorker, run_id: str, *, clock: FakeClock
) -> str:
    supervisor = PrReviewV2Supervisor(
        engine,
        worker,
        run_id=run_id,
        idle_poll_seconds=0,
        clock=clock,
        max_steps=120,
    )
    return supervisor.run_until_idle()


def test_source_run_multi_round_to_no_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heads = HeadTracker(SHA_A)
    clock, engine, _arts, prep, control, worker, hybrid, fake_codex, local, carrier = _build_stack(
        tmp_path, monkeypatch, heads=heads
    )
    ctx = _ctx(prepared_from="source_run", source_run_id="src-e2e", head_sha=SHA_A)
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-e2e",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"diff --git a/a b/a\n+x\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=ctx,
        )
    )
    assert created.reused is False
    assert created.next_action is SafeNextAction.START
    assert engine.get_status(created.run_id).state_kind == "prepared"
    _wire_context_resolver(local, created.run_id, _origin_context_ref(engine, created.run_id))

    assert fake_codex.all_argv == []
    assert hybrid._side.write_kinds == []  # noqa: SLF001

    start = control.start(created.run_id)
    assert start.transition_applied is True
    final = _drive_to_completed(engine, worker, created.run_id, clock=clock)
    assert final == "completed"

    status = control.status(created.run_id)
    assert status.state_kind == "completed"
    assert status.cycle_number >= 2
    history = control.history(created.run_id, limit=200)
    kinds = [e.event_kind for e in history.entries]
    assert "start_requested" in kinds
    assert "effect_succeeded" in kinds
    assert "commit_patch" in hybrid._side.write_kinds  # noqa: SLF001
    assert "push_commit" in hybrid._side.write_kinds  # noqa: SLF001
    assert hybrid._side.write_kinds.count("commit_patch") == 2  # noqa: SLF001
    # Content-bound targets: initial vs fix commit must not share identity.
    commit_ids = [  # noqa: SLF001
        eid
        for eid, kind in zip(hybrid._side.write_effect_ids, hybrid._side.write_kinds, strict=True)
        if kind == "commit_patch"
    ]
    commit_keys = [  # noqa: SLF001
        key
        for key, kind in zip(
            hybrid._side.write_idempotency_keys, hybrid._side.write_kinds, strict=True
        )
        if kind == "commit_patch"
    ]
    assert len(commit_ids) == len(set(commit_ids)) == 2
    assert len(commit_keys) == len(set(commit_keys)) == 2
    assert any(  # noqa: SLF001
        k.endswith("cycle:02:request_bot_review") for k in hybrid._side.write_idempotency_keys
    )
    # Distinct push targets (initial vs fix commit SHAs) must not collide.
    push_keys = [k for k in hybrid._side.write_idempotency_keys if "push_commit" in k]  # noqa: SLF001
    assert len(push_keys) == len(set(push_keys)) == 2

    for argv in fake_codex.all_argv:
        assert "resume" in argv
        assert SESSION in argv
        assert "--last" not in argv
    assert carrier.seeded
    assert all(cid.startswith("prv2c-") for cid in carrier.seeded)


def test_existing_pr_multi_round_to_no_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heads = HeadTracker(SHA_A)
    clock, engine, _arts, prep, control, worker, hybrid, fake_codex, local, _carrier = _build_stack(
        tmp_path, monkeypatch, heads=heads
    )
    fake_codex._payloads = [  # noqa: SLF001
        {
            "decisions": [
                {
                    "thread_id": THREAD_ID,
                    "decision": "actionable",
                    "safe_summary": "needs fix",
                    "reply_body": None,
                }
            ],
            "fix_prompt_text": "Please fix thread PRRT_thread_1",
        },
        {
            "title": "Fix",
            "body": "Fix body",
            "commit_subject": "Fix subject",
            "commit_body": "Fix body",
        },
    ]
    binding = PullRequestBinding(
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        pr_number=42,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_A,
    )
    hybrid._side.binding = binding  # noqa: SLF001
    ctx = _ctx(prepared_from="existing_pr", source_run_id=None, head_sha=SHA_A)
    created = prep.prepare_existing_pr(
        ExistingPrSnapshot(
            binding=binding,
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=ctx,
            accepted_patch_bytes=None,
        )
    )
    assert created.origin_kind is OriginKind.EXISTING_PR
    assert created.next_action is SafeNextAction.START
    _wire_context_resolver(local, created.run_id, _origin_context_ref(engine, created.run_id))

    control.start(created.run_id)
    final = _drive_to_completed(engine, worker, created.run_id, clock=clock)
    assert final == "completed"
    status = control.status(created.run_id)
    assert status.cycle_number >= 2
    assert "commit_patch" in hybrid._side.write_kinds  # noqa: SLF001
    for argv in fake_codex.all_argv:
        assert SESSION in argv
        assert "--last" not in argv


def test_stale_claim_completion_is_fenced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    heads = HeadTracker(SHA_A)
    clock, engine, _arts, prep, _control, _worker, hybrid, _fake, local, _carrier = _build_stack(
        tmp_path, monkeypatch, heads=heads
    )
    ctx = _ctx(prepared_from="source_run", source_run_id="src-fence", head_sha=SHA_A)
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-fence",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"diff\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=ctx,
        )
    )
    _wire_context_resolver(local, created.run_id, _origin_context_ref(engine, created.run_id))
    engine.apply_event(
        EventSubmission(
            submission_id="start-1",
            run_id=created.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    w_a = EffectWorker(
        engine,
        hybrid,
        owner_id="owner-a",
        heartbeat_interval=timedelta(seconds=30),
        heartbeat_interval_wait=_BlockUntilStopWait(),
    )
    step_a = w_a.run_once(created.run_id)
    assert step_a.claimed is True
    assert step_a.completed is True

    lease_a = engine.acquire_lease(created.run_id, "owner-a")
    claim = engine.claim_next_effect(created.run_id, "owner-a", lease_a.generation).claim
    assert claim is not None
    clock.advance(120)
    lease_b = engine.acquire_lease(created.run_id, "owner-b")
    assert lease_b.generation > lease_a.generation

    late = engine.complete_claim(
        EffectCompletionRequest(
            submission_id="late-stale",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease_a.generation,
            event=EffectBlocked(
                occurred_at=clock.now(),
                token=claim.completion_token,
                reason=PauseReasonKind.REQUIRED_OPERATOR_ACTION,
                safe_action=SafeAction(kind=SafeActionKind.INSPECT_ARTIFACTS, condition="stale"),
                safe_summary="stale late result",
            ),
        )
    )
    assert late.disposition is EventDisposition.STALE


def test_abort_skips_unowned_launcher_metadata(tmp_path: Path) -> None:
    clock = FakeClock(T0)
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "engine.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="ab"),
        lease_ttl=timedelta(seconds=30),
    )
    arts = ProtectedResultStore(tmp_path / "artifacts")
    launchers = SupervisorLauncherStore(arts.root)
    prep = PreparationService(engine, arts, clock=clock)
    ctx = _ctx(prepared_from="source_run", source_run_id="src-ab", head_sha=SHA_A)
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-ab",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"diff\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=ctx,
        )
    )
    control = ControlPlaneService(
        engine,
        artifact_store=arts,
        launcher_store=launchers,
        spawner=lambda _rid: "spawned",
    )
    control.start(created.run_id)
    # Foreign run_id in launcher metadata must never be signaled.
    launchers.write(
        SupervisorLauncherMetadata(
            schema_version=1,
            run_id="other-run",
            token="tok",
            pid=999999,
            pgid=999999,
            process_start_time="0",
            executable="/bin/false",
            created_at=T0.isoformat(),
        )
    )
    result = control.abort(created.run_id)
    assert result.abort_persisted is True
    assert result.state_kind == "aborted"
    assert result.process_action is AbortProcessAction.UNNECESSARY


def test_existing_pr_two_actionable_cycles_preserve_exact_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3: null prepared chat → first accepted local-fix chat reused exactly."""

    heads = HeadTracker(SHA_A)
    clock, engine, _arts, prep, control, worker, hybrid, fake_codex, local, carrier = _build_stack(
        tmp_path, monkeypatch, heads=heads
    )
    hybrid._side.actionable_cycles = {1, 2}  # noqa: SLF001
    created_chat = {"id": None}

    def _local_fix(request):  # noqa: ANN001
        if created_chat["id"] is None:
            created_chat["id"] = "chat-e2e-continuity-exact"
        return LocalReviewFixResult(
            run_id=request.run_id,
            status="completed",
            chat_id=created_chat["id"],
            iteration_count=1,
            latest_staged_diff_path="git/diffs/01.patch",
            latest_review_path=None,
            result_message="ok",
            outcome=LocalReviewOutcome.ACCEPTED,
            needs_external_continuation=True,
        )

    monkeypatch.setattr(
        "ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter.run_local_review_fix",
        _local_fix,
    )
    fake_codex._payloads = [  # noqa: SLF001
        {
            "decisions": [
                {
                    "thread_id": THREAD_ID,
                    "decision": "actionable",
                    "safe_summary": "needs fix",
                    "reply_body": None,
                }
            ],
            "fix_prompt_text": "Please fix thread cycle 1",
        },
        {
            "title": "Fix1",
            "body": "Fix body 1",
            "commit_subject": "Fix subject 1",
            "commit_body": "Fix body 1",
        },
        {
            "decisions": [
                {
                    "thread_id": THREAD_ID,
                    "decision": "actionable",
                    "safe_summary": "needs fix again",
                    "reply_body": None,
                }
            ],
            "fix_prompt_text": "Please fix thread cycle 2",
        },
        {
            "title": "Fix2",
            "body": "Fix body 2",
            "commit_subject": "Fix subject 2",
            "commit_body": "Fix body 2",
        },
    ]
    binding = PullRequestBinding(
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        pr_number=42,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_A,
    )
    hybrid._side.binding = binding  # noqa: SLF001
    ctx = _ctx(
        prepared_from="existing_pr",
        source_run_id=None,
        head_sha=SHA_A,
        chat_id=None,
    )
    created = prep.prepare_existing_pr(
        ExistingPrSnapshot(
            binding=binding,
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=ctx,
            accepted_patch_bytes=None,
        )
    )
    _wire_context_resolver(local, created.run_id, _origin_context_ref(engine, created.run_id))
    control.start(created.run_id)
    final = _drive_to_completed(engine, worker, created.run_id, clock=clock)
    assert final == "completed"
    assert created_chat["id"] == "chat-e2e-continuity-exact"
    assert len(carrier.seeds) >= 2
    assert carrier.seeds[0].cursor_chat_id is None
    assert all(seed.cursor_chat_id == created_chat["id"] for seed in carrier.seeds[1:])
    assert len({seed.cursor_chat_id for seed in carrier.seeds[1:]}) == 1
