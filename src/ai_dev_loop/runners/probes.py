"""Local CLI availability and authentication probes."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.process import require_success, run_process
from ai_dev_loop.redaction import redact_text


@dataclass(frozen=True)
class ProbeResult:
    command: str
    ok: bool
    detail: str


def executable_probe(command: str, *, label: str) -> ProbeResult | None:
    executable = command.split()[0]
    if shutil.which(executable) is None:
        return ProbeResult(
            command=command,
            ok=False,
            detail=f"{label} executable not found: {command}",
        )
    return None


def verify_executable(command: str, *, label: str) -> None:
    failure = executable_probe(command, label=label)
    if failure is not None:
        raise ValidationError(failure.detail)


def _parse_cursor_models(stdout: str) -> set[str]:
    models: set[str] = set()
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith("available"):
            continue
        model_id = stripped.split(" - ", 1)[0].strip()
        if model_id:
            models.add(model_id)
    return models


def probe_cursor_auth(cursor_command: str) -> ProbeResult:
    result = run_process([cursor_command, "status", "--format", "json"])
    if result.returncode != 0:
        detail = redact_text(result.stderr.strip() or result.stdout.strip() or "auth probe failed")
        return ProbeResult(command=cursor_command, ok=False, detail=detail)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ProbeResult(
            command=cursor_command,
            ok=False,
            detail="Cursor auth probe returned invalid JSON",
        )
    authenticated = payload.get("authenticated")
    if authenticated is None:
        authenticated = payload.get("isAuthenticated")
    if authenticated is True or payload.get("status") == "authenticated":
        return ProbeResult(command=cursor_command, ok=True, detail="authenticated")
    return ProbeResult(command=cursor_command, ok=False, detail="Cursor is not authenticated")


def probe_cursor_model(cursor_command: str, model: str) -> ProbeResult:
    result = run_process([cursor_command, "models"])
    if result.returncode != 0:
        detail = redact_text(
            result.stderr.strip() or result.stdout.strip() or "models probe failed"
        )
        return ProbeResult(command=cursor_command, ok=False, detail=detail)
    models = _parse_cursor_models(result.stdout)
    if model in models:
        return ProbeResult(command=cursor_command, ok=True, detail=f"model available: {model}")
    return ProbeResult(
        command=cursor_command,
        ok=False,
        detail=f"Cursor model not available: {model}",
    )


def probe_codex_auth(codex_command: str) -> ProbeResult:
    result = run_process([codex_command, "login", "status"])
    if result.returncode != 0:
        detail = redact_text(result.stderr.strip() or result.stdout.strip() or "auth probe failed")
        return ProbeResult(command=codex_command, ok=False, detail=detail)
    combined = (result.stdout + result.stderr).lower()
    if "logged in" in combined or "authenticated" in combined:
        return ProbeResult(command=codex_command, ok=True, detail="authenticated")
    return ProbeResult(command=codex_command, ok=False, detail="Codex is not authenticated")


def run_start_probes(
    *,
    git_command: str = "git",
    cursor_command: str,
    cursor_model: str,
    codex_command: str,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for command, label in (
        (git_command, "Git"),
        (cursor_command, "Cursor"),
        (codex_command, "Codex"),
    ):
        failure = executable_probe(command, label=label)
        if failure is not None:
            results.append(failure)
    if results:
        return results
    return [
        probe_cursor_auth(cursor_command),
        probe_cursor_model(cursor_command, cursor_model),
        probe_codex_auth(codex_command),
    ]


def require_probe_success(results: list[ProbeResult]) -> None:
    failures = [result for result in results if not result.ok]
    if failures:
        details = "; ".join(result.detail for result in failures)
        raise ValidationError(f"local CLI probe failed: {details}")


def capture_git_status(repo_root: str) -> str:
    result = run_process(
        ["git", "status", "--porcelain=v2", "--untracked-files=all"],
        cwd=repo_root,
    )
    require_success(result, context="git status")
    return result.stdout
