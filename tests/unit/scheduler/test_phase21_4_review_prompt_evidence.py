"""Phase 21.4 scheduler Codex review prompt evidence tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest
from pydantic import ValidationError

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler import codex_attempt_runner
from ai_dev_loop.scheduler.application.codex_review_prompt_evidence import (
    CodexReviewPromptEvidenceError,
    publish_review_prompt_before_launch,
)
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    REVIEW_RETRY_OPERATIONAL_ENVELOPE,
    SCHEDULER_CODEX_REVIEW_SANDBOX,
    codex_review_prompt_evidence_rel,
    codex_review_prompt_rel,
)
from ai_dev_loop.scheduler.domain.review_prompt_evidence import SchedulerReviewPromptEvidenceV1
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.state import sha256_bytes


def _sample_evidence(
    tmp_path: Path, run_id: str, *, operational_retry: bool = False
) -> dict[str, object]:
    artifact_root = tmp_path / "artifacts"
    run_root = run_artifact_root(artifact_root, run_id)
    run_root.mkdir(parents=True)
    plan_rel = "plan/plan.md"
    prompt_rel = "prompts/cursor-initial.txt"
    plan_path = run_root / plan_rel
    prompt_path = run_root / prompt_rel
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# plan\n", encoding="utf-8")
    prompt_path.write_text("initial prompt\n", encoding="utf-8")
    evidence: dict[str, object] = {
        "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        "attempt_id": "att-" + "b" * 32,
        "run_id": run_id,
        "dispatch_id": "fx-prompt-evidence",
        "repository_root": str(tmp_path / "repo"),
        "repository_git_common_dir": str(tmp_path / "repo" / ".git"),
        "repository_git_dir": str(tmp_path / "repo" / ".git"),
        "repository_branch": "main",
        "repository_initial_head": "abc123",
        "codex_timeout_minutes": 5,
        "review_iteration": 1,
        "codex_command": "codex",
        "codex_sandbox": SCHEDULER_CODEX_REVIEW_SANDBOX,
        "review_model": "gpt-5.6-sol",
        "review_reasoning_effort": "high",
        "review_skill": "review-staged-cursor-execution",
        "plan_repository_path": "docs/plans/sample-plan.md",
        "plan_artifact_path": plan_rel,
        "plan_sha256": sha256_bytes(plan_path.read_bytes()),
        "prompt_source_repository_path": "docs/plans/prompt.txt",
        "prompt_artifact_path": prompt_rel,
        "prompt_sha256": sha256_bytes(prompt_path.read_bytes()),
        "max_review_iterations": 3,
    }
    if operational_retry:
        evidence["operational_review_retry"] = True
    (tmp_path / "repo").mkdir(parents=True)
    return evidence


def test_c01_prompt_evidence_matches_stdin_before_fake_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-prompt-evidence"
    evidence = _sample_evidence(tmp_path, run_id)
    artifact_root = tmp_path / "artifacts"
    run_root = run_artifact_root(artifact_root, run_id)
    attempt_id = str(evidence["attempt_id"])
    captured: dict[str, str] = {}

    def fake_streaming(*args: object, **kwargs: object) -> object:
        from ai_dev_loop.process import StreamingProcessResult

        stdin_prompt = kwargs.get("stdin_text")
        assert isinstance(stdin_prompt, str)
        captured["stdin"] = stdin_prompt
        prompt_rel = codex_review_prompt_rel(1, attempt_id)
        evidence_rel = codex_review_prompt_evidence_rel(1, attempt_id)
        assert (run_root / prompt_rel).is_file()
        assert (run_root / evidence_rel).is_file()
        on_disk = (run_root / prompt_rel).read_bytes()
        assert (
            hashlib.sha256(on_disk).hexdigest()
            == hashlib.sha256(stdin_prompt.encode("utf-8")).hexdigest()
        )
        return StreamingProcessResult(
            args=list(args[0]) if args else [],
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"019def00-0000-0000-0000-0000000000bb"}\n',
            stderr="",
            timed_out=False,
            elapsed_seconds=0.1,
        )

    monkeypatch.setattr(codex_attempt_runner, "run_process_streaming", fake_streaming)
    codex_attempt_runner._run_codex_review(
        evidence,
        run_root,
        run_id,
        artifact_root=artifact_root,
    )
    assert captured["stdin"]


def test_c01_retry_prefix_in_published_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-retry-prefix"
    evidence = _sample_evidence(tmp_path, run_id, operational_retry=True)
    artifact_root = tmp_path / "artifacts"
    run_root = run_artifact_root(artifact_root, run_id)
    attempt_id = str(evidence["attempt_id"])

    def fake_streaming(*args: object, **kwargs: object) -> object:
        from ai_dev_loop.process import StreamingProcessResult

        stdin_prompt = str(kwargs.get("stdin_text", ""))
        assert stdin_prompt.startswith(REVIEW_RETRY_OPERATIONAL_ENVELOPE)
        prompt_rel = codex_review_prompt_rel(1, attempt_id)
        assert (run_root / prompt_rel).read_text(encoding="utf-8") == stdin_prompt
        return StreamingProcessResult(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            elapsed_seconds=0.0,
        )

    monkeypatch.setattr(codex_attempt_runner, "run_process_streaming", fake_streaming)
    codex_attempt_runner._run_codex_review(
        evidence,
        run_root,
        run_id,
        artifact_root=artifact_root,
    )


def test_c02_fault_after_prompt_leaves_prompt_without_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-fault-after-prompt"
    evidence = _sample_evidence(tmp_path, run_id)
    artifact_root = tmp_path / "artifacts"
    run_root = run_artifact_root(artifact_root, run_id)
    attempt_id = str(evidence["attempt_id"])
    monkeypatch.setenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", "after_prompt")

    def _no_launch(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("no launch")

    monkeypatch.setattr(codex_attempt_runner, "run_process_streaming", _no_launch)
    with pytest.raises(RuntimeError):
        codex_attempt_runner._run_codex_review(
            evidence,
            run_root,
            run_id,
            artifact_root=artifact_root,
        )
    assert (run_root / codex_review_prompt_rel(1, attempt_id)).is_file()
    assert not (run_root / codex_review_prompt_evidence_rel(1, attempt_id)).is_file()


def test_c02_fault_before_evidence_leaves_incomplete_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-fault-before-evidence"
    evidence = _sample_evidence(tmp_path, run_id)
    artifact_root = tmp_path / "artifacts"
    run_root = run_artifact_root(artifact_root, run_id)
    attempt_id = str(evidence["attempt_id"])
    launched = {"called": False}

    def fake_streaming(*args: object, **kwargs: object) -> object:
        launched["called"] = True
        raise AssertionError("codex should not launch")

    monkeypatch.setenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", "before_evidence")
    monkeypatch.setattr(codex_attempt_runner, "run_process_streaming", fake_streaming)
    with pytest.raises(RuntimeError):
        codex_attempt_runner._run_codex_review(
            evidence,
            run_root,
            run_id,
            artifact_root=artifact_root,
        )
    assert launched["called"] is False
    assert (run_root / codex_review_prompt_rel(1, attempt_id)).is_file()
    assert not (run_root / codex_review_prompt_evidence_rel(1, attempt_id)).is_file()


def test_c02_fault_before_launch_blocks_codex_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-fault-before-launch"
    evidence = _sample_evidence(tmp_path, run_id)
    artifact_root = tmp_path / "artifacts"
    run_root = run_artifact_root(artifact_root, run_id)
    launched = {"called": False}

    def fake_streaming(*args: object, **kwargs: object) -> object:
        launched["called"] = True
        raise AssertionError("codex should not launch")

    monkeypatch.setenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", "before_launch")
    monkeypatch.setattr(codex_attempt_runner, "run_process_streaming", fake_streaming)
    with pytest.raises(RuntimeError):
        codex_attempt_runner._run_codex_review(
            evidence,
            run_root,
            run_id,
            artifact_root=artifact_root,
        )
    assert launched["called"] is False


def test_c06_prompt_artifact_private_permissions(tmp_path: Path) -> None:
    import os
    import stat

    if os.name == "nt":
        pytest.skip("POSIX permission bits required")
    run_id = "run-private-prompt"
    artifact_root = tmp_path / "artifacts"
    store = ProtectedArtifactStore(artifact_root)
    attempt_id = "att-" + "9" * 32
    publish_review_prompt_before_launch(
        store,
        run_id=run_id,
        attempt_id=attempt_id,
        review_iteration=1,
        prompt="private prompt bytes\n",
    )
    run_root = run_artifact_root(artifact_root, run_id)
    prompt_path = run_root / codex_review_prompt_rel(1, attempt_id)
    mode = stat.S_IMODE(prompt_path.stat().st_mode)
    assert mode == 0o600


def test_f13_legacy_publish_env_does_not_bypass_runner_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-legacy-publish-env-ignored"
    evidence = _sample_evidence(tmp_path, run_id)
    artifact_root = tmp_path / "artifacts"
    run_root = run_artifact_root(artifact_root, run_id)
    attempt_id = str(evidence["attempt_id"])
    monkeypatch.setenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_PUBLISH", "disabled")

    def fake_streaming(*args: object, **kwargs: object) -> object:
        from ai_dev_loop.process import StreamingProcessResult

        prompt_rel = codex_review_prompt_rel(1, attempt_id)
        evidence_rel = codex_review_prompt_evidence_rel(1, attempt_id)
        assert (run_root / prompt_rel).is_file()
        assert (run_root / evidence_rel).is_file()
        return StreamingProcessResult(
            args=[],
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"019def00-0000-0000-0000-0000000000bb"}\n',
            stderr="",
            timed_out=False,
            elapsed_seconds=0.0,
        )

    monkeypatch.setattr(codex_attempt_runner, "run_process_streaming", fake_streaming)
    codex_attempt_runner._run_codex_review(
        evidence,
        run_root,
        run_id,
        artifact_root=artifact_root,
    )


def test_c02_fault_before_prompt_prevents_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-fault-before-prompt"
    evidence = _sample_evidence(tmp_path, run_id)
    artifact_root = tmp_path / "artifacts"
    run_root = run_artifact_root(artifact_root, run_id)
    launched = {"called": False}

    def fake_streaming(*args: object, **kwargs: object) -> object:
        launched["called"] = True
        raise AssertionError("codex should not launch")

    monkeypatch.setenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", "before_prompt")
    monkeypatch.setattr(codex_attempt_runner, "run_process_streaming", fake_streaming)
    with pytest.raises(RuntimeError):
        codex_attempt_runner._run_codex_review(
            evidence,
            run_root,
            run_id,
            artifact_root=artifact_root,
        )
    assert launched["called"] is False
    attempt_id = str(evidence["attempt_id"])
    assert not (run_root / codex_review_prompt_rel(1, attempt_id)).exists()


def test_c02_identical_prelaunch_replay_is_idempotent(tmp_path: Path) -> None:
    run_id = "run-replay-idempotent"
    artifact_root = tmp_path / "artifacts"
    store = ProtectedArtifactStore(artifact_root)
    attempt_id = "att-" + "c" * 32
    prompt = "same bytes\n"
    first = publish_review_prompt_before_launch(
        store,
        run_id=run_id,
        attempt_id=attempt_id,
        review_iteration=1,
        prompt=prompt,
    )
    second = publish_review_prompt_before_launch(
        store,
        run_id=run_id,
        attempt_id=attempt_id,
        review_iteration=1,
        prompt=prompt,
    )
    assert first[0].sha256 == second[0].sha256


def _valid_evidence_sample() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "run-schema",
        "attempt_id": "att-" + "f" * 32,
        "review_iteration": 1,
        "prompt_path": "codex/reviews/01.att-ffffffffffffffffffffffffffffffff.prompt.txt",
        "prompt_sha256": "a" * 64,
        "prompt_size_bytes": 12,
    }


def test_c06_evidence_schema_and_model_alignment() -> None:
    schema = json.loads(schema_path("scheduler-review-prompt-evidence-v1.json").read_text())
    sample = _valid_evidence_sample()
    jsonschema.validate(sample, schema)
    model = SchedulerReviewPromptEvidenceV1.model_validate(sample)
    assert model.prompt_size_bytes == 12
    with pytest.raises(ValidationError):
        SchedulerReviewPromptEvidenceV1.model_validate({**sample, "schema_version": 2})


@pytest.mark.parametrize(
    ("mutation", "expect_model"),
    [
        ({"schema_version": 2}, True),
        ({"review_iteration": "1"}, True),
        ({"review_iteration": True}, True),
        ({"prompt_sha256": "A" * 64}, True),
        ({"prompt_sha256": "short"}, True),
        ({"prompt_size_bytes": -1}, True),
        ({"prompt_size_bytes": "12"}, True),
        ({"extra_field": "x"}, False),
        ({"attempt_id": None}, True),
        ({"prompt_path": None}, True),
    ],
)
def test_c06_evidence_schema_and_model_reject_invalid_payloads(
    mutation: dict[str, object],
    expect_model: bool,
) -> None:
    schema = json.loads(schema_path("scheduler-review-prompt-evidence-v1.json").read_text())
    payload = {**_valid_evidence_sample(), **mutation}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)
    if expect_model:
        with pytest.raises(ValidationError):
            SchedulerReviewPromptEvidenceV1.model_validate(payload)


def test_f04_publish_or_verify_does_not_use_read_bytes_on_prompt(tmp_path: Path) -> None:
    run_id = "run-streaming-verify"
    artifact_root = tmp_path / "artifacts"
    store = ProtectedArtifactStore(artifact_root)
    attempt_id = "att-" + "e" * 32
    prompt_bytes = b"y" * (512 * 1024)
    prompt_rel = codex_review_prompt_rel(1, attempt_id)
    read_bytes_calls: list[Path] = []
    original_read_bytes = Path.read_bytes

    def tracked_read_bytes(self: Path) -> bytes:
        if prompt_rel in self.as_posix():
            read_bytes_calls.append(self)
        return original_read_bytes(self)

    with patch.object(Path, "read_bytes", tracked_read_bytes):
        store.publish_or_verify_bytes(
            run_id,
            prompt_rel,
            prompt_bytes,
            max_bytes=len(prompt_bytes),
        )
        read_bytes_calls.clear()
        store.publish_or_verify_bytes(
            run_id,
            prompt_rel,
            prompt_bytes,
            max_bytes=len(prompt_bytes),
        )
    assert read_bytes_calls == []


def test_c02_conflicting_replay_raises(tmp_path: Path) -> None:
    run_id = "run-replay-conflict"
    artifact_root = tmp_path / "artifacts"
    store = ProtectedArtifactStore(artifact_root)
    attempt_id = "att-" + "d" * 32
    publish_review_prompt_before_launch(
        store,
        run_id=run_id,
        attempt_id=attempt_id,
        review_iteration=1,
        prompt="first\n",
    )
    with pytest.raises(CodexReviewPromptEvidenceError):
        publish_review_prompt_before_launch(
            store,
            run_id=run_id,
            attempt_id=attempt_id,
            review_iteration=1,
            prompt="second\n",
        )
