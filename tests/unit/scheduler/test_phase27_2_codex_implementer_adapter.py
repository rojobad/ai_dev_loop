"""Phase 27.2 Codex implementer argv and artifact adapter tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from ai_dev_loop.process import StreamingProcessResult
from ai_dev_loop.scheduler.application.attempt_envelope import sha256_file
from ai_dev_loop.scheduler.application.codex_argv import (
    build_scheduler_codex_implementer_bootstrap_args,
    build_scheduler_codex_implementer_resume_args,
)
from ai_dev_loop.scheduler.codex_implementer_attempt_runner import _execute
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
    RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256

SESSION_ID = "019def00-0000-0000-0000-0000000000aa"


@pytest.mark.parametrize(
    ("case", "effect_kind", "turn", "prompt"),
    [
        (
            "initial",
            BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
            "initial",
            "Initial CRLF.\r\nUnicode: ñ雪.\r\n",
        ),
        (
            "correction",
            RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
            "correction",
            "Fix one.\r\nFix two.\nMixed: λ.\r\n\r\n",
        ),
        (
            "capacity-continuation",
            RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
            "initial",
            "Continue exact bytes.\r\nPartial stays.\n終\r\n",
        ),
    ],
)
def test_adapter_forwards_authenticated_prompt_bytes_without_newline_translation(
    git_repo: Path,
    tmp_path: Path,
    case: str,
    effect_kind: str,
    turn: str,
    prompt: str,
) -> None:
    run_root = tmp_path / f"run-{case}"
    prompt_path = run_root / f"prompts/{case}.txt"
    prompt_path.parent.mkdir(parents=True)
    expected = prompt.encode("utf-8")
    prompt_path.write_bytes(expected)
    witness = tmp_path / f"{case}.stdin.bin"
    fake_codex = tmp_path / f"fake-codex-{case}"
    fake_codex.write_text(
        f"""#!{sys.executable}
import json
import sys

payload = sys.stdin.buffer.read()
open({str(witness)!r}, "wb").write(payload)
if "resume" not in sys.argv[1:]:
    print(json.dumps({{"type": "thread.started", "thread_id": {SESSION_ID!r}}}))
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "done"}}}}))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    evidence: dict[str, object] = {
        "effect_kind": effect_kind,
        "iteration": 1,
        "attempt_id": "att-" + "b" * 32,
        "prompt_path": str(prompt_path.relative_to(run_root)),
        "prompt_sha256": payload_sha256(prompt),
        "repository_root": str(git_repo),
        "codex_command": str(fake_codex),
        "model": "gpt-5.5",
        "reasoning_effort": "xhigh",
        "timeout_seconds": 30,
        "turn": turn,
        "dispatch_id": f"dispatch-{case}",
    }
    if effect_kind == RESUME_CODEX_IMPLEMENTER_EFFECT_KIND:
        evidence["session_id"] = SESSION_ID

    outcome = _execute(evidence, run_root, "run-1")

    assert outcome["returncode"] == 0
    assert witness.read_bytes() == expected
    assert sha256_file(witness) == sha256_file(prompt_path)


def test_implementer_argv_freezes_configuration_and_never_uses_last(tmp_path: Path) -> None:
    bootstrap = build_scheduler_codex_implementer_bootstrap_args(
        command="codex",
        repo_root=str(tmp_path),
        model="gpt-5.5",
        reasoning_effort="xhigh",
    )
    resume = build_scheduler_codex_implementer_resume_args(
        command="codex",
        repo_root=str(tmp_path),
        model="gpt-5.5",
        reasoning_effort="xhigh",
        session_id=SESSION_ID,
    )

    for argv in (bootstrap, resume):
        assert argv[:4] == ["codex", "exec", "--cd", str(tmp_path)]
        assert argv[argv.index("--sandbox") + 1] == "workspace-write"
        assert 'approval_policy="never"' in argv
        assert argv[argv.index("--model") + 1] == "gpt-5.5"
        assert 'model_reasoning_effort="xhigh"' in argv
        assert "--last" not in argv
        assert "--output-schema" not in argv
    assert "resume" not in bootstrap
    assert resume[resume.index("--json") + 1] == SESSION_ID


def test_adapter_records_timeout_identity_and_private_attempt_artifacts(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    prompt_path = run_root / "prompts/initial.txt"
    prompt_path.parent.mkdir(parents=True)
    prompt = "implement the approved phase\n"
    prompt_path.write_text(prompt, encoding="utf-8")
    evidence: dict[str, object] = {
        "effect_kind": BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
        "iteration": 1,
        "attempt_id": "att-" + "a" * 32,
        "prompt_path": "prompts/initial.txt",
        "prompt_sha256": payload_sha256(prompt),
        "repository_root": str(git_repo),
        "codex_command": "codex",
        "model": "gpt-5.5",
        "reasoning_effort": "xhigh",
        "timeout_seconds": 30,
        "turn": "initial",
        "dispatch_id": "dispatch-1",
    }
    captured_argv: list[str] = []

    def fake_process(argv: list[str], **kwargs: object) -> StreamingProcessResult:
        captured_argv.extend(argv)
        stdout_path = kwargs["stdout_path"]
        stderr_path = kwargs["stderr_path"]
        assert isinstance(stdout_path, Path)
        assert isinstance(stderr_path, Path)
        stdout_path.write_text(
            json.dumps({"type": "thread.started", "thread_id": SESSION_ID}) + "\n",
            encoding="utf-8",
        )
        stderr_path.write_text("timed out\n", encoding="utf-8")
        return StreamingProcessResult(
            args=argv,
            returncode=-15,
            stdout="",
            stderr="timed out",
            timed_out=True,
            elapsed_seconds=30.0,
        )

    monkeypatch.setattr(
        "ai_dev_loop.scheduler.codex_implementer_attempt_runner.run_process_streaming",
        fake_process,
    )
    outcome = _execute(evidence, run_root, "run-1")

    assert outcome["timed_out"] is True
    assert outcome["session_id"] == SESSION_ID
    assert outcome["returncode"] == -15
    assert "--last" not in captured_argv
    metadata_path = run_root / str(outcome["metadata_path"])
    capture_path = run_root / str(outcome["session_capture_path"])
    assert outcome["session_capture_sha256"] == sha256_file(capture_path)
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["timed_out"] is True
    assert json.loads(capture_path.read_text(encoding="utf-8"))["session_id"] == SESSION_ID
    if os.name != "nt":
        assert metadata_path.stat().st_mode & 0o777 == 0o600
        assert capture_path.stat().st_mode & 0o777 == 0o600
