"""Shared helpers for Phase 21.4 integration acceptance tests."""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    codex_review_prompt_evidence_rel,
    codex_review_prompt_rel,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant
from ai_dev_loop.scheduler.domain.state import AwaitingCodexReviewState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
_ATTEMPT_COUNTER = itertools.count()
CLI = CliRunner()

FIX_PROMPT_ALPHA = "Fix issue ALPHA from first findings review."
FIX_PROMPT_BETA = "Fix issue BETA from second findings review."
MARKDOWN_ALPHA = "# Review ALPHA\n\nFound issue."
MARKDOWN_BETA_PREFIX = "# Review BETA\n\nFound issue."

# Pre-21.4 scheduler runs completed Codex reviews without per-attempt prompt capture.
PRE_21_4_HISTORICAL_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "phase21_4_pre_21_4_historical"
)


@dataclass
class PerTickClock:
    """Frozen wall clock within a tick; advance only at explicit test boundaries."""

    anchor: datetime
    step: timedelta = timedelta(minutes=1)
    tick_index: int = 0
    _frozen: datetime = field(init=False)

    def __post_init__(self) -> None:
        self._frozen = self.anchor

    def now(self) -> datetime:
        return self._frozen

    def advance_tick(self) -> None:
        self.tick_index += 1
        self._frozen = self.anchor + self.step * self.tick_index

    def instant_at_tick(self, tick_index: int) -> str:
        return encode_utc_instant(self.anchor + self.step * tick_index)


def materialize_pre_21_4_prompt_absence(
    run_root: Path,
    *,
    review_iteration: int,
    attempt_id: str,
) -> None:
    """Reproduce pre-21.4 durable absence by removing only the 21.4 writer artifacts."""

    (run_root / codex_review_prompt_rel(review_iteration, attempt_id)).unlink(missing_ok=True)
    (run_root / codex_review_prompt_evidence_rel(review_iteration, attempt_id)).unlink(
        missing_ok=True
    )


def codex_runner_attempt_ids(backend: FakeAgentProcessBackend) -> list[str]:
    ids: list[str] = []
    for call in backend.launch_calls:
        if any("codex_attempt_runner" in part for part in call.agent_argv):
            ids.append(call.attempt_id)
    return ids


def cursor_runner_attempt_ids(backend: FakeAgentProcessBackend) -> list[str]:
    ids: list[str] = []
    for call in backend.launch_calls:
        if any("cursor_attempt_runner" in part for part in call.agent_argv):
            ids.append(call.attempt_id)
    return ids


def cursor_turn_attempt_ids(
    backend: FakeAgentProcessBackend,
    *,
    artifact_root: Path,
    run_id: str,
) -> list[str]:
    from ai_dev_loop.scheduler.domain.cursor_contract import (
        RUN_CURSOR_TURN_EFFECT_KIND,
        invocation_evidence_rel,
    )
    from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root

    run_root = run_artifact_root(artifact_root, run_id)
    turn_ids: list[str] = []
    for attempt_id in cursor_runner_attempt_ids(backend):
        evidence_path = run_root / invocation_evidence_rel(attempt_id)
        if not evidence_path.is_file():
            continue
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        if str(payload.get("effect_kind", "")) == RUN_CURSOR_TURN_EFFECT_KIND:
            turn_ids.append(attempt_id)
    return turn_ids


def expected_reviewer_session_ref(session_id: str) -> str:
    material = f"ai_dev_loop:reviewer-session:v1:{session_id.strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def next_attempt_id() -> str:
    return f"att-{next(_ATTEMPT_COUNTER):032x}"


def submit_sample_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    *,
    max_reviews: int = 3,
    prompt_text: str | None = None,
) -> str:
    prompt = prompt_text or (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(
        encoding="utf-8"
    )
    options = SubmitOptions(
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        max_review_iterations=max_reviews,
    )
    with patch("sys.stdin", StringIO(prompt)):
        return submit_run(options).run_id


def make_tick_service(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    *,
    now: datetime | None = None,
    now_factory: Callable[[], datetime] | None = None,
    backend: FakeAgentProcessBackend | None = None,
    attempt_id_factory: Callable[[], str] | None = None,
) -> TickService:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    resolved_now_factory = now_factory or (lambda: now or datetime(2026, 9, 20, 12, 0, tzinfo=UTC))
    return TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=resolved_now_factory,
        tick_owner_factory=lambda: f"tick-21-4-acc-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=attempt_id_factory or next_attempt_id,
        attempt_backend=backend
        or FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )


def run_tick_once(tick: TickService, clock: PerTickClock | None = None) -> object:
    receipt = tick.run_once()
    if clock is not None:
        clock.advance_tick()
    return receipt


def build_bootstrap_codex_invocation_evidence(
    tick: TickService,
    run_id: str,
    *,
    attempt_id: str,
    dispatch_id: str,
) -> dict[str, object]:
    assert tick._attempt_service is not None
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        if not isinstance(state, AwaitingCodexReviewState):
            raise AssertionError(f"expected awaiting_codex_review, got {state.kind}")
        evidence = tick._attempt_service._codex_binding(
            state,
            effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
            run_id=run_id,
        )
    evidence.update(
        {
            "attempt_id": attempt_id,
            "run_id": run_id,
            "dispatch_id": dispatch_id,
        }
    )
    return evidence


def pending_bootstrap_dispatch_id(tick: TickService, run_id: str) -> str:
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        if not isinstance(state, AwaitingCodexReviewState):
            raise AssertionError(f"expected awaiting_codex_review, got {state.kind}")
        effects = tick.store.list_eligible_effects(conn, run_id=run_id, now=tick._now_factory())
        codex = [
            row for row in effects if str(row["effect_kind"]) == BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND
        ]
    if not codex:
        raise AssertionError("no eligible bootstrap codex effect")
    return str(codex[0]["dispatch_id"])


def install_pre_21_4_historical_review_fixture(
    scheduler_paths: dict[str, Path],
) -> dict[str, object]:
    fixture_root = PRE_21_4_HISTORICAL_FIXTURE
    manifest_path = fixture_root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing historical fixture manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    provenance = manifest.get("provenance", {})
    baseline = str(provenance.get("baseline_commit", ""))
    if baseline != "e4c6b08824b76d3ef7fdf38a6d30f6533b5aed02":
        raise AssertionError(f"unexpected historical fixture baseline: {baseline}")
    db_src = fixture_root / "engine.sqlite3"
    artifacts_src = fixture_root / "artifacts"
    if not db_src.is_file() or not artifacts_src.is_dir():
        raise FileNotFoundError("historical fixture missing engine.sqlite3 or artifacts/")
    scheduler_paths["artifact_root"].mkdir(parents=True, exist_ok=True)
    shutil.copytree(artifacts_src, scheduler_paths["artifact_root"], dirs_exist_ok=True)
    shutil.copy2(db_src, scheduler_paths["db_path"])
    return manifest


def run_until(
    tick: TickService,
    run_id: str,
    *,
    target_kind: str,
    max_ticks: int = 100,
    clock: PerTickClock | None = None,
) -> None:
    """Tick until ``target_kind``, authorizing Codex review retries at production boundaries."""
    retry_service = ReviewRetryService(tick.store, tick.artifacts)
    last_kind = "unknown"
    for _ in range(max_ticks):
        run_tick_once(tick, clock)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            last_kind = state.kind
            if state.kind == target_kind:
                return
            if state.kind == "waiting_codex_review_retry":
                retry_service.retry(run_id)
    raise AssertionError(
        f"did not reach {target_kind} within {max_ticks} ticks; last_state={last_kind}"
    )


def invoke_cli(args: list[str]) -> tuple[int, dict[str, object], str]:
    result = CLI.invoke(app, args)
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload, result.stderr


def fetch_review_content_chunks(
    run_id: str,
    attempt_id: str,
    content_kind: str,
    *,
    chunk_size: int = 4096,
) -> tuple[list[bytes], int]:
    offset = 0
    parts: list[bytes] = []
    chunk_count = 0
    total: int | None = None
    while True:
        code, payload, _ = invoke_cli(
            [
                "integration",
                "run",
                "review-content",
                run_id,
                "--attempt",
                attempt_id,
                "--kind",
                content_kind,
                "--offset",
                str(offset),
                "--limit",
                str(chunk_size),
                "--output",
                "json",
            ]
        )
        assert code == 0, payload
        data = payload["data"]
        if not data.get("available"):
            break
        chunk_count += 1
        if total is None:
            total = int(data["availableBytes"])
        blob = base64.b64decode(str(data["contentBase64"]))
        parts.append(blob)
        if not data.get("hasMore"):
            break
        next_offset = data.get("nextOffset")
        assert next_offset is not None, payload
        offset = int(next_offset)
    result = b"".join(parts)
    if total is not None:
        assert len(result) == total
    return parts, chunk_count


def fetch_review_content_bytes(
    run_id: str,
    attempt_id: str,
    content_kind: str,
    *,
    chunk_size: int = 4096,
) -> bytes:
    offset = 0
    parts: list[bytes] = []
    total: int | None = None
    while True:
        code, payload, _ = invoke_cli(
            [
                "integration",
                "run",
                "review-content",
                run_id,
                "--attempt",
                attempt_id,
                "--kind",
                content_kind,
                "--offset",
                str(offset),
                "--limit",
                str(chunk_size),
                "--output",
                "json",
            ]
        )
        assert code == 0, payload
        data = payload["data"]
        if not data.get("available"):
            break
        if total is None:
            total = int(data["availableBytes"])
        blob = base64.b64decode(str(data["contentBase64"]))
        parts.append(blob)
        if not data.get("hasMore"):
            break
        next_offset = data.get("nextOffset")
        assert next_offset is not None, payload
        offset = int(next_offset)
    result = b"".join(parts)
    if total is not None:
        assert len(result) == total
    return result


def codex_log_text(fake_clis: dict[str, Path]) -> str:
    path = Path(fake_clis["codex_log"])
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def codex_runner_launch_count(backend: FakeAgentProcessBackend) -> int:
    return sum(
        1
        for call in backend.launch_calls
        if any("codex_attempt_runner" in part for part in call.agent_argv)
    )


def codex_bootstrap_invocation_count(codex_log: str) -> int:
    return sum(
        1 for line in codex_log.splitlines() if line.startswith("ARGS:") and "'resume'" not in line
    )


def codex_resume_invocation_count(codex_log: str) -> int:
    return sum(
        1 for line in codex_log.splitlines() if line.startswith("ARGS:") and "'resume'" in line
    )
