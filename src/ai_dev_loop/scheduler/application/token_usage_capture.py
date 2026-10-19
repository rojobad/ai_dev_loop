"""Best-effort publication of immutable token-usage observations."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ai_dev_loop.scheduler.domain.token_usage import (
    NormalizedUsage,
    ProviderNativeUsage,
    UsageObservationV1,
    normalize_codex_usage,
    normalize_cursor_usage,
    usage_observation_id,
    usage_observation_rel,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)

MAX_USAGE_OBSERVATION_BYTES = 32 * 1024
MAX_USAGE_DIAGNOSTIC_BYTES = 4 * 1024


@dataclass(frozen=True)
class UsageCaptureResult:
    bindings: tuple[dict[str, str], ...] = ()
    diagnostics: tuple[str, ...] = ()
    completed_contributions: int = 0
    observed_contributions: int = 0
    contribution_complete: bool = False

    def outcome_fields(self) -> dict[str, object]:
        return {
            "usage_observations": list(self.bindings),
            "usage_diagnostics": list(self.diagnostics),
            "usage_capture": {
                "completed_contributions": self.completed_contributions,
                "observed_contributions": self.observed_contributions,
                "contribution_complete": self.contribution_complete,
            },
        }


@dataclass(frozen=True)
class CursorUsageEvent:
    """One provider-native per-turn usage contribution from the SDK stream."""

    offset: str
    ordinal: int
    usage: object


def _artifact_root_from_run_root(run_root: Path) -> Path:
    if run_root.parent.name != "runs":
        raise ValueError("run_root is not below the scheduler runs artifact directory")
    return run_root.parent.parent


def _publish_diagnostic(
    *,
    store: ProtectedArtifactStore,
    run_id: str,
    scheduler_attempt_id: str,
    publisher_attempt_id: str,
    code: str,
) -> str:
    key = hashlib.sha256(
        f"{scheduler_attempt_id}\0{publisher_attempt_id}\0{code}".encode()
    ).hexdigest()
    rel = f"usage/diagnostics/{key[:2]}/{key}.json"
    payload = json.dumps(
        {
            "schema_version": 1,
            "scheduler_attempt_id": scheduler_attempt_id,
            "publisher_attempt_id": publisher_attempt_id,
            "code": code,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    with suppress(Exception):
        store.publish_or_verify_bytes(run_id, rel, payload, max_bytes=MAX_USAGE_DIAGNOSTIC_BYTES)
    return code


def _publish_observation(
    *,
    run_root: Path,
    observation: UsageObservationV1,
) -> tuple[dict[str, str] | None, str | None]:
    store = ProtectedArtifactStore(_artifact_root_from_run_root(run_root))
    rel = usage_observation_rel(observation.observation_id)
    payload = observation.model_dump_json(exclude_none=False).encode()
    try:
        stored = store.publish_or_verify_bytes(
            observation.run_id,
            rel,
            payload,
            max_bytes=MAX_USAGE_OBSERVATION_BYTES,
        )
        return {"path": stored.relative_path, "sha256": stored.sha256}, None
    except ProtectedArtifactError:
        # A replay can carry the same native counters through a different capture
        # artifact. Preserve the first immutable evidence while deduplicating the
        # semantic observation. A real disagreement is diagnosed and never added.
        path = run_root / rel
        try:
            existing = UsageObservationV1.model_validate_json(path.read_bytes())
            if _observations_are_replay_compatible(existing, observation):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                return {"path": rel, "sha256": digest}, None
        except Exception:
            pass
        code = _publish_diagnostic(
            store=store,
            run_id=observation.run_id,
            scheduler_attempt_id=observation.scheduler_attempt_id,
            publisher_attempt_id=observation.publisher_attempt_id,
            code="conflicting_usage_observation",
        )
        return None, code


def _observations_are_replay_compatible(
    existing: UsageObservationV1,
    candidate: UsageObservationV1,
) -> bool:
    if existing.semantic_fingerprint() == candidate.semantic_fingerprint():
        return True
    if existing.provider != "cursor" or candidate.provider != "cursor":
        return False
    identity_fields = (
        "observation_id",
        "run_id",
        "role",
        "stage",
        "scheduler_attempt_id",
        "source_dispatch_id",
        "native_key",
    )
    if any(getattr(existing, name) != getattr(candidate, name) for name in identity_fields):
        return False
    native_fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
    )
    if any(
        getattr(existing.native, name) != getattr(candidate.native, name) for name in native_fields
    ):
        return False
    existing_reasoning = existing.native.reasoning_tokens
    candidate_reasoning = candidate.native.reasoning_tokens
    return (
        existing_reasoning is None
        or candidate_reasoning is None
        or existing_reasoning == candidate_reasoning
    )


def capture_cursor_usage_safely(
    *,
    run_root: Path,
    run_id: str,
    role: Literal["implementer"] = "implementer",
    stage: Literal["initial", "correction"],
    scheduler_attempt_id: str,
    publisher_attempt_id: str,
    source_dispatch_id: str,
    native_run_id: str,
    source_artifact_path: str,
    source_artifact_sha256: str,
    usage: object,
    capture_complete: bool,
) -> UsageCaptureResult:
    return capture_cursor_usage_evidence_safely(
        run_root=run_root,
        run_id=run_id,
        role=role,
        stage=stage,
        scheduler_attempt_id=scheduler_attempt_id,
        publisher_attempt_id=publisher_attempt_id,
        source_dispatch_id=source_dispatch_id,
        native_run_id=native_run_id,
        source_artifact_path=source_artifact_path,
        source_artifact_sha256=source_artifact_sha256,
        cumulative_usages=(usage,),
        capture_complete=capture_complete,
    )


def _sum_cursor_usage(
    events: tuple[tuple[CursorUsageEvent, ProviderNativeUsage, NormalizedUsage], ...],
) -> tuple[ProviderNativeUsage, NormalizedUsage]:
    required = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
    )
    raw: dict[str, int | None] = {
        name: sum(int(getattr(native, name)) for _, native, _ in events) for name in required
    }
    reasoning = [native.reasoning_tokens for _, native, _ in events]
    raw["reasoning_tokens"] = (
        sum(value for value in reasoning if value is not None)
        if all(value is not None for value in reasoning)
        else None
    )
    return normalize_cursor_usage(raw)


def _reconcile_cursor_usage(
    aggregate: tuple[ProviderNativeUsage, NormalizedUsage],
    cumulative: tuple[ProviderNativeUsage, NormalizedUsage],
) -> tuple[tuple[ProviderNativeUsage, NormalizedUsage], bool]:
    required = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
    )
    if any(getattr(aggregate[0], name) != getattr(cumulative[0], name) for name in required):
        return aggregate, False
    aggregate_reasoning = aggregate[0].reasoning_tokens
    cumulative_reasoning = cumulative[0].reasoning_tokens
    if (
        aggregate_reasoning is not None
        and cumulative_reasoning is not None
        and aggregate_reasoning != cumulative_reasoning
    ):
        return aggregate, False
    reasoning = aggregate_reasoning if aggregate_reasoning is not None else cumulative_reasoning
    reconciled = {name: getattr(aggregate[0], name) for name in required}
    reconciled["reasoning_tokens"] = reasoning
    return normalize_cursor_usage(reconciled), True


def _publish_cursor_observation(
    *,
    run_root: Path,
    run_id: str,
    role: Literal["implementer"],
    stage: Literal["initial", "correction"],
    scheduler_attempt_id: str,
    publisher_attempt_id: str,
    source_dispatch_id: str,
    native_run_id: str,
    source_artifact_path: str,
    source_artifact_sha256: str,
    usage: tuple[ProviderNativeUsage, NormalizedUsage],
    source_event_offsets: tuple[str, ...],
    contribution_count: int,
    contribution_complete: bool,
    diagnostics: list[str],
) -> UsageCaptureResult:
    binding: dict[str, str] | None = None
    try:
        observation = UsageObservationV1(
            observation_id=usage_observation_id(
                provider="cursor",
                role=role,
                scheduler_attempt_id=scheduler_attempt_id,
                native_key=native_run_id,
            ),
            run_id=run_id,
            provider="cursor",
            role=role,
            stage=stage,
            scheduler_attempt_id=scheduler_attempt_id,
            publisher_attempt_id=publisher_attempt_id,
            source_dispatch_id=source_dispatch_id,
            native_key=native_run_id,
            source_artifact_path=source_artifact_path,
            source_artifact_sha256=source_artifact_sha256,
            source_format="cursor-sdk-usage-reconciled-v1",
            source_event_ordinal=1,
            source_event_offsets=source_event_offsets,
            capture_complete=contribution_complete,
            availability="known" if contribution_complete else "partial",
            native=usage[0],
            normalized=usage[1],
        )
        binding, diagnostic = _publish_observation(run_root=run_root, observation=observation)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    except Exception:
        diagnostics.append(
            _publish_diagnostic(
                store=ProtectedArtifactStore(_artifact_root_from_run_root(run_root)),
                run_id=run_id,
                scheduler_attempt_id=scheduler_attempt_id,
                publisher_attempt_id=publisher_attempt_id,
                code="cursor_usage_publication_failed",
            )
        )
    return UsageCaptureResult(
        bindings=() if binding is None else (binding,),
        diagnostics=tuple(diagnostics),
        completed_contributions=contribution_count,
        observed_contributions=contribution_count if binding is not None else 0,
        contribution_complete=contribution_complete and binding is not None,
    )


def capture_cursor_usage_evidence_safely(
    *,
    run_root: Path,
    run_id: str,
    role: Literal["implementer"] = "implementer",
    stage: Literal["initial", "correction"],
    scheduler_attempt_id: str,
    publisher_attempt_id: str,
    source_dispatch_id: str,
    native_run_id: str,
    source_artifact_path: str,
    source_artifact_sha256: str,
    stream_usages: tuple[CursorUsageEvent, ...] = (),
    cumulative_usages: tuple[object, ...] = (),
    capture_complete: bool,
) -> UsageCaptureResult:
    """Reconcile distinct stream turns with cumulative result/replay evidence."""

    store = ProtectedArtifactStore(_artifact_root_from_run_root(run_root))
    diagnostics: list[str] = []
    invalid_evidence = False
    events_by_offset: dict[str, tuple[CursorUsageEvent, ProviderNativeUsage, NormalizedUsage]] = {}
    conflicting_event = False
    for event in stream_usages:
        if not event.offset or len(event.offset) > 512 or event.ordinal < 1:
            invalid_evidence = True
            continue
        try:
            native, normalized = normalize_cursor_usage(event.usage)
        except Exception:
            invalid_evidence = True
            continue
        candidate = (event, native, normalized)
        existing = events_by_offset.get(event.offset)
        if existing is None:
            events_by_offset[event.offset] = candidate
        elif (existing[1], existing[2]) != (native, normalized):
            conflicting_event = True

    cumulative: list[tuple[ProviderNativeUsage, NormalizedUsage]] = []
    for usage in cumulative_usages:
        try:
            cumulative_candidate = normalize_cursor_usage(usage)
        except Exception:
            invalid_evidence = True
            continue
        if cumulative_candidate not in cumulative:
            cumulative.append(cumulative_candidate)

    events = tuple(
        sorted(events_by_offset.values(), key=lambda item: (item[0].ordinal, item[0].offset))
    )
    if conflicting_event or len(cumulative) > 1:
        code = _publish_diagnostic(
            store=store,
            run_id=run_id,
            scheduler_attempt_id=scheduler_attempt_id,
            publisher_attempt_id=publisher_attempt_id,
            code="conflicting_cursor_usage_evidence",
        )
        return UsageCaptureResult(
            diagnostics=(code,),
            completed_contributions=len(events),
            contribution_complete=False,
        )

    cumulative_usage = cumulative[0] if cumulative else None
    aggregate = _sum_cursor_usage(events) if events else None
    reconciled_usage: tuple[ProviderNativeUsage, NormalizedUsage] | None
    if aggregate is not None and cumulative_usage is not None:
        reconciled_usage, cumulative_matches = _reconcile_cursor_usage(aggregate, cumulative_usage)
    else:
        reconciled_usage = aggregate if aggregate is not None else cumulative_usage
        cumulative_matches = False
    if aggregate is not None and cumulative_usage is not None and not cumulative_matches:
        diagnostics.append(
            _publish_diagnostic(
                store=store,
                run_id=run_id,
                scheduler_attempt_id=scheduler_attempt_id,
                publisher_attempt_id=publisher_attempt_id,
                code="conflicting_cursor_usage_evidence",
            )
        )
    if events and capture_complete and cumulative_usage is None:
        diagnostics.append(
            _publish_diagnostic(
                store=store,
                run_id=run_id,
                scheduler_attempt_id=scheduler_attempt_id,
                publisher_attempt_id=publisher_attempt_id,
                code="missing_cursor_cumulative_usage",
            )
        )
    if not events and cumulative_usage is None:
        code = _publish_diagnostic(
            store=store,
            run_id=run_id,
            scheduler_attempt_id=scheduler_attempt_id,
            publisher_attempt_id=publisher_attempt_id,
            code=(
                "missing_cursor_usage"
                if not stream_usages and not cumulative_usages
                else "invalid_cursor_usage"
            ),
        )
        return UsageCaptureResult(
            diagnostics=(code,),
            contribution_complete=False,
        )
    if invalid_evidence:
        diagnostics.append(
            _publish_diagnostic(
                store=store,
                run_id=run_id,
                scheduler_attempt_id=scheduler_attempt_id,
                publisher_attempt_id=publisher_attempt_id,
                code="invalid_cursor_usage_evidence",
            )
        )
    contribution_complete = (
        capture_complete
        and not invalid_evidence
        and ((bool(events) and cumulative_matches) or (not events and cumulative_usage is not None))
    )
    assert reconciled_usage is not None
    return _publish_cursor_observation(
        run_root=run_root,
        run_id=run_id,
        role=role,
        stage=stage,
        scheduler_attempt_id=scheduler_attempt_id,
        publisher_attempt_id=publisher_attempt_id,
        source_dispatch_id=source_dispatch_id,
        native_run_id=native_run_id,
        source_artifact_path=source_artifact_path,
        source_artifact_sha256=source_artifact_sha256,
        usage=reconciled_usage,
        source_event_offsets=tuple(event.offset for event, _, _ in events),
        contribution_count=len(events) if events else 1,
        contribution_complete=contribution_complete,
        diagnostics=diagnostics,
    )


def capture_codex_usage_safely(
    *,
    run_root: Path,
    run_id: str,
    role: Literal["implementer", "reviewer"],
    stage: Literal["initial", "correction", "review"],
    scheduler_attempt_id: str,
    publisher_attempt_id: str,
    source_dispatch_id: str,
    source_artifact_path: str,
    source_artifact_sha256: str,
    capture_complete: bool,
) -> UsageCaptureResult:
    bindings: list[dict[str, str]] = []
    diagnostics: list[str] = []
    store = ProtectedArtifactStore(_artifact_root_from_run_root(run_root))
    try:
        data = (run_root / source_artifact_path).read_text(encoding="utf-8")
    except Exception:
        code = _publish_diagnostic(
            store=store,
            run_id=run_id,
            scheduler_attempt_id=scheduler_attempt_id,
            publisher_attempt_id=publisher_attempt_id,
            code="codex_usage_source_unavailable",
        )
        return UsageCaptureResult(diagnostics=(code,), contribution_complete=False)
    completed_ordinal = 0
    valid: list[tuple[int, ProviderNativeUsage, NormalizedUsage]] = []
    for line in data.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        completed_ordinal += 1
        usage = event.get("usage")
        if usage is None:
            diagnostics.append(
                _publish_diagnostic(
                    store=store,
                    run_id=run_id,
                    scheduler_attempt_id=scheduler_attempt_id,
                    publisher_attempt_id=publisher_attempt_id,
                    code=f"missing_codex_usage_turn_{completed_ordinal}",
                )
            )
            continue
        try:
            native, normalized = normalize_codex_usage(usage)
            valid.append((completed_ordinal, native, normalized))
        except Exception:
            diagnostics.append(
                _publish_diagnostic(
                    store=store,
                    run_id=run_id,
                    scheduler_attempt_id=scheduler_attempt_id,
                    publisher_attempt_id=publisher_attempt_id,
                    code=f"invalid_codex_usage_turn_{completed_ordinal}",
                )
            )
    contribution_complete = (
        capture_complete and completed_ordinal > 0 and completed_ordinal == len(valid)
    )
    for ordinal, native, normalized in valid:
        try:
            native_key = str(ordinal)
            observation_id = usage_observation_id(
                provider="codex",
                role=role,
                scheduler_attempt_id=scheduler_attempt_id,
                native_key=native_key,
            )
            observation = UsageObservationV1(
                observation_id=observation_id,
                run_id=run_id,
                provider="codex",
                role=role,
                stage=stage,
                scheduler_attempt_id=scheduler_attempt_id,
                publisher_attempt_id=publisher_attempt_id,
                source_dispatch_id=source_dispatch_id,
                native_key=native_key,
                source_artifact_path=source_artifact_path,
                source_artifact_sha256=source_artifact_sha256,
                source_format="codex-exec-jsonl-v1",
                source_event_ordinal=ordinal,
                capture_complete=contribution_complete,
                availability="known" if contribution_complete else "partial",
                native=native,
                normalized=normalized,
            )
            binding, diagnostic = _publish_observation(run_root=run_root, observation=observation)
            if binding is not None:
                bindings.append(binding)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        except Exception:
            diagnostics.append(
                _publish_diagnostic(
                    store=store,
                    run_id=run_id,
                    scheduler_attempt_id=scheduler_attempt_id,
                    publisher_attempt_id=publisher_attempt_id,
                    code=f"codex_usage_publication_failed_turn_{ordinal}",
                )
            )
    return UsageCaptureResult(
        bindings=tuple(bindings),
        diagnostics=tuple(diagnostics),
        completed_contributions=completed_ordinal,
        observed_contributions=len(bindings),
        contribution_complete=contribution_complete and len(bindings) == len(valid),
    )
