"""Local CLI availability, authentication, version, and model-compatibility probes."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.process import require_success, run_process
from ai_dev_loop.redaction import redact_text
from ai_dev_loop.runners.tool_updates import write_compatibility_artifact

AUTH_PROBE_TIMEOUT_SECONDS = 30.0
VERSION_PROBE_TIMEOUT_SECONDS = 30.0
MODEL_CATALOG_TIMEOUT_SECONDS = 60.0


class CompatibilityClassification(StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE_MODEL = "incompatible_model"
    UNKNOWN = "unknown"
    PROBE_FAILED = "probe_failed"


@dataclass(frozen=True)
class ProbeResult:
    command: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class VersionProbeResult:
    tool: Literal["cursor", "codex"]
    command: str
    ok: bool
    version: str | None
    detail: str


@dataclass(frozen=True)
class ModelCompatibilityResult:
    tool: Literal["cursor", "codex"]
    command: str
    required_model: str | None
    classification: CompatibilityClassification
    installed_version: str | None
    detail: str
    catalog_source: str | None = None


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
    result = run_process(
        [cursor_command, "status", "--format", "json"],
        timeout=AUTH_PROBE_TIMEOUT_SECONDS,
    )
    if result.timed_out:
        return ProbeResult(
            command=cursor_command,
            ok=False,
            detail="Cursor auth probe timed out",
        )
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
    result = run_process(
        [cursor_command, "models"],
        timeout=AUTH_PROBE_TIMEOUT_SECONDS,
    )
    if result.timed_out:
        return ProbeResult(
            command=cursor_command,
            ok=False,
            detail="Cursor models probe timed out",
        )
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
    result = run_process(
        [codex_command, "login", "status"],
        timeout=AUTH_PROBE_TIMEOUT_SECONDS,
    )
    if result.timed_out:
        return ProbeResult(
            command=codex_command,
            ok=False,
            detail="Codex auth probe timed out",
        )
    if result.returncode != 0:
        detail = redact_text(result.stderr.strip() or result.stdout.strip() or "auth probe failed")
        return ProbeResult(command=codex_command, ok=False, detail=detail)
    combined = (result.stdout + result.stderr).lower()
    if "logged in" in combined or "authenticated" in combined:
        return ProbeResult(command=codex_command, ok=True, detail="authenticated")
    return ProbeResult(command=codex_command, ok=False, detail="Codex is not authenticated")


def probe_version(
    command: str,
    *,
    tool: Literal["cursor", "codex"],
) -> VersionProbeResult:
    missing = executable_probe(command, label=tool)
    if missing is not None:
        return VersionProbeResult(
            tool=tool,
            command=command,
            ok=False,
            version=None,
            detail=missing.detail,
        )
    result = run_process([command, "--version"], timeout=VERSION_PROBE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        detail = redact_text(
            result.stderr.strip() or result.stdout.strip() or "version probe failed"
        )
        return VersionProbeResult(
            tool=tool,
            command=command,
            ok=False,
            version=None,
            detail=detail,
        )
    text = (result.stdout or result.stderr).strip()
    version = text.splitlines()[0].strip() if text else None
    return VersionProbeResult(
        tool=tool,
        command=command,
        ok=True,
        version=version,
        detail=f"version {version}" if version else "version unavailable",
    )


def _extract_model_ids_from_catalog(payload: Any) -> set[str] | None:
    """Parse documented Codex model-catalog JSON variants into model id strings."""

    models: set[str] = set()

    def add_model(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            models.add(value.strip())
        elif isinstance(value, dict):
            for key in ("id", "slug", "name", "model"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    models.add(candidate.strip())
                    return

    if isinstance(payload, list):
        for item in payload:
            add_model(item)
        return models
    if isinstance(payload, dict):
        for key in ("models", "data", "items", "available_models"):
            nested = payload.get(key)
            if isinstance(nested, list):
                for item in nested:
                    add_model(item)
                return models
        # Flat map of model id -> metadata
        string_keys = [key for key in payload if isinstance(key, str) and key.strip()]
        if string_keys and all(
            isinstance(payload[key], (dict, str, type(None))) for key in string_keys
        ):
            for key in string_keys:
                if re.match(r"^[A-Za-z0-9._:-]+$", key) and key not in {
                    "schema_version",
                    "object",
                    "type",
                }:
                    models.add(key)
            if models:
                return models
    return None


def probe_codex_model_compatibility(
    codex_command: str,
    *,
    required_model: str | None,
    installed_version: str | None = None,
) -> ModelCompatibilityResult:
    if required_model is None:
        return ModelCompatibilityResult(
            tool="codex",
            command=codex_command,
            required_model=None,
            classification=CompatibilityClassification.UNKNOWN,
            installed_version=installed_version,
            detail=(
                "Codex required model is unset (legacy Phase 9 inherit); "
                "re-prepare the run to capture session runtime metadata"
            ),
            catalog_source=None,
        )

    refreshed = run_process(
        [codex_command, "debug", "models"],
        timeout=MODEL_CATALOG_TIMEOUT_SECONDS,
    )
    catalog_source = "debug_models"
    catalog_text = refreshed.stdout
    if refreshed.returncode != 0 or not catalog_text.strip():
        bundled = run_process(
            [codex_command, "debug", "models", "--bundled"],
            timeout=MODEL_CATALOG_TIMEOUT_SECONDS,
        )
        catalog_source = "debug_models_bundled"
        catalog_text = bundled.stdout
        if bundled.returncode != 0:
            detail = redact_text(
                bundled.stderr.strip()
                or refreshed.stderr.strip()
                or "codex model catalog probe failed"
            )
            return ModelCompatibilityResult(
                tool="codex",
                command=codex_command,
                required_model=required_model,
                classification=CompatibilityClassification.PROBE_FAILED,
                installed_version=installed_version,
                detail=detail,
                catalog_source=catalog_source,
            )

    try:
        payload = json.loads(catalog_text)
    except json.JSONDecodeError:
        return ModelCompatibilityResult(
            tool="codex",
            command=codex_command,
            required_model=required_model,
            classification=CompatibilityClassification.UNKNOWN,
            installed_version=installed_version,
            detail="Codex model catalog was not valid JSON; compatibility is unknown",
            catalog_source=catalog_source,
        )

    model_ids = _extract_model_ids_from_catalog(payload)
    if model_ids is None:
        return ModelCompatibilityResult(
            tool="codex",
            command=codex_command,
            required_model=required_model,
            classification=CompatibilityClassification.UNKNOWN,
            installed_version=installed_version,
            detail="Codex model catalog shape was unrecognized; compatibility is unknown",
            catalog_source=catalog_source,
        )
    if required_model in model_ids:
        return ModelCompatibilityResult(
            tool="codex",
            command=codex_command,
            required_model=required_model,
            classification=CompatibilityClassification.COMPATIBLE,
            installed_version=installed_version,
            detail=f"model available: {required_model}",
            catalog_source=catalog_source,
        )
    return ModelCompatibilityResult(
        tool="codex",
        command=codex_command,
        required_model=required_model,
        classification=CompatibilityClassification.INCOMPATIBLE_MODEL,
        installed_version=installed_version,
        detail=(
            f"required Codex model is not listed by the installed CLI: {required_model}; "
            "updating may add support"
        ),
        catalog_source=catalog_source,
    )


def probe_cursor_model_compatibility(
    cursor_command: str,
    *,
    required_model: str,
    installed_version: str | None = None,
) -> ModelCompatibilityResult:
    result = run_process([cursor_command, "models"], timeout=MODEL_CATALOG_TIMEOUT_SECONDS)
    if result.returncode != 0:
        detail = redact_text(
            result.stderr.strip() or result.stdout.strip() or "models probe failed"
        )
        return ModelCompatibilityResult(
            tool="cursor",
            command=cursor_command,
            required_model=required_model,
            classification=CompatibilityClassification.PROBE_FAILED,
            installed_version=installed_version,
            detail=detail,
            catalog_source="agent_models",
        )
    models = _parse_cursor_models(result.stdout)
    if required_model in models:
        return ModelCompatibilityResult(
            tool="cursor",
            command=cursor_command,
            required_model=required_model,
            classification=CompatibilityClassification.COMPATIBLE,
            installed_version=installed_version,
            detail=f"model available: {required_model}",
            catalog_source="agent_models",
        )
    return ModelCompatibilityResult(
        tool="cursor",
        command=cursor_command,
        required_model=required_model,
        classification=CompatibilityClassification.INCOMPATIBLE_MODEL,
        installed_version=installed_version,
        detail=(
            f"required Cursor model is not listed by the installed CLI: {required_model}; "
            "updating may add support"
        ),
        catalog_source="agent_models",
    )


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


def run_tool_compatibility_probes(
    *,
    cursor_command: str,
    cursor_model: str,
    codex_command: str,
    codex_model: str | None,
    run_directory: Path | None = None,
) -> tuple[list[ModelCompatibilityResult], list[VersionProbeResult]]:
    cursor_version = probe_version(cursor_command, tool="cursor")
    codex_version = probe_version(codex_command, tool="codex")
    cursor_compat = probe_cursor_model_compatibility(
        cursor_command,
        required_model=cursor_model,
        installed_version=cursor_version.version,
    )
    codex_compat = probe_codex_model_compatibility(
        codex_command,
        required_model=codex_model,
        installed_version=codex_version.version,
    )
    results = [cursor_compat, codex_compat]
    versions = [cursor_version, codex_version]
    if run_directory is not None:
        for item in results:
            write_compatibility_artifact(
                run_directory,
                tool=item.tool,
                payload={
                    "tool": item.tool,
                    "command": item.command,
                    "required_model": item.required_model,
                    "classification": item.classification.value,
                    "installed_version": item.installed_version,
                    "detail": item.detail,
                    "catalog_source": item.catalog_source,
                    "probed_at": datetime.now(tz=UTC).isoformat(),
                },
            )
    return results, versions


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
