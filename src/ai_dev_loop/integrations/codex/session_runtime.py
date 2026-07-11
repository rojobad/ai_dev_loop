"""Safe Codex rollout session-runtime metadata extraction.

Parses allowlisted JSONL event types only. Never retains transcript content,
tool payloads, prompts, or arbitrary dictionaries from rollout files.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_dev_loop.config import CODEX_REVIEW_REASONING_EFFORTS
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex.desktop_bridge import (
    ROLLOUT_FILENAME_PATTERN,
    WslBridgePaths,
    resolve_wsl_bridge_paths,
)

SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Allowlisted event types that may carry model / reasoning scalars.
ALLOWLISTED_SETTINGS_EVENT_TYPES = frozenset({"thread_settings_applied", "turn_context"})
SESSION_META_EVENT_TYPE = "session_meta"

# Nested wrappers that may carry allowlisted payload.type values.
WRAPPER_EVENT_TYPES = frozenset({"event_msg"})

MAX_JSONL_LINE_BYTES = 256 * 1024

SessionOrigin = Literal["native_wsl", "desktop_bridge"]


@dataclass(frozen=True)
class CodexSessionRuntime:
    """Safe effective runtime captured from an exact Codex session rollout."""

    session_id: str
    model: str
    reasoning_effort: str
    origin: SessionOrigin
    source_event_type: str
    source_timestamp: str | None


@dataclass(frozen=True)
class _ScalarUpdate:
    model: str | None
    reasoning_effort: str | None
    event_type: str
    timestamp: str | None


def is_valid_codex_session_id(value: str) -> bool:
    return bool(SESSION_ID_RE.fullmatch(value.strip()))


def require_codex_session_id(session_id: str | None) -> str:
    if session_id is None or not session_id.strip():
        raise ValidationError("codex session id is required (--codex-session-id)")
    cleaned = session_id.strip()
    if not is_valid_codex_session_id(cleaned):
        raise ValidationError(
            "codex session id must be a UUID "
            "(8-4-4-4-12 hexadecimal form); refusing unsafe session identity"
        )
    return cleaned


def read_codex_session_runtime(session_id: str) -> CodexSessionRuntime:
    """Locate the exact session rollout and extract effective model/reasoning."""

    cleaned = require_codex_session_id(session_id)
    rollout_path, origin = _locate_rollout(cleaned)
    return _parse_rollout_runtime(rollout_path, session_id=cleaned, origin=origin)


def _locate_rollout(session_id: str) -> tuple[Path, SessionOrigin]:
    paths = resolve_session_search_paths()
    sessions_dir = paths.wsl_sessions_dir
    bridge = paths.bridge_link
    bridge_resolved: Path | None = None
    if bridge.is_symlink() or bridge.is_dir():
        try:
            bridge_resolved = bridge.resolve()
        except OSError as exc:
            raise ValidationError(
                "failed to resolve the Codex desktop session bridge path"
            ) from exc

    search_roots: list[tuple[Path, SessionOrigin]] = []
    if sessions_dir.is_dir():
        search_roots.append((sessions_dir, "native_wsl"))
    if (
        bridge_resolved is not None
        and bridge_resolved.is_dir()
        and bridge_resolved != sessions_dir.resolve()
    ):
        # pathlib does not recurse through directory symlinks on supported Python
        # versions, so search the resolved, trusted bridge target explicitly.
        search_roots.append((bridge_resolved, "desktop_bridge"))

    matches: list[tuple[Path, SessionOrigin]] = []
    seen_resolved: set[Path] = set()

    for search_root, search_origin in search_roots:
        for path in search_root.rglob("rollout-*.jsonl"):
            if not path.is_file():
                continue
            match = ROLLOUT_FILENAME_PATTERN.search(path.name)
            if match is None:
                continue
            if match.group(2).lower() != session_id.lower():
                continue
            try:
                resolved = path.resolve()
            except OSError as exc:
                raise ValidationError(
                    "failed to resolve Codex rollout path for the requested session"
                ) from exc
            if resolved in seen_resolved:
                continue
            seen_resolved.add(resolved)
            origin = search_origin
            if bridge_resolved is not None and (
                resolved == bridge_resolved or bridge_resolved in resolved.parents
            ):
                origin = "desktop_bridge"
            matches.append((resolved, origin))

    if not matches:
        raise ValidationError(
            "no Codex rollout found for the exact session id under trusted native WSL "
            "or desktop-bridge session roots; confirm the session exists and, for "
            "Desktop sessions, that `ai_dev_loop integrations sessions install` is healthy"
        )
    if len(matches) > 1:
        raise ValidationError(
            "ambiguous Codex rollout match for the exact session id: multiple distinct "
            "rollout files were found under trusted session roots"
        )
    return matches[0]


def resolve_session_search_paths() -> WslBridgePaths:
    """Resolve trusted session roots for runtime metadata lookup.

    Codex Desktop may propagate ``CODEX_HOME=/mnt/c/.../.codex`` into WSL. Session
    lookup must still use the native WSL Codex home (and its ``from-desktop`` bridge)
    rather than rejecting the entire prepare. Bridge install/status continue to reject
    DrvFS ``CODEX_HOME`` values; this helper is intentionally read-path only.
    """

    env_value = os.environ.get("CODEX_HOME", "").strip()
    if env_value:
        env_home = Path(env_value).expanduser().resolve()
        if str(env_home).startswith("/mnt/"):
            native_home = (Path.home() / ".codex").resolve()
            return resolve_wsl_bridge_paths(wsl_codex_home=native_home)
    return resolve_wsl_bridge_paths()


def _parse_rollout_runtime(
    rollout_path: Path,
    *,
    session_id: str,
    origin: SessionOrigin,
) -> CodexSessionRuntime:
    latest_model: str | None = None
    latest_reasoning: str | None = None
    latest_model_meta: tuple[str, str | None] | None = None
    latest_reasoning_meta: tuple[str, str | None] | None = None
    matched_session_meta = False
    saw_session_meta = False
    line_number = 0

    try:
        with rollout_path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line_number += 1
                if not raw_line.strip():
                    continue
                encoded = raw_line.encode("utf-8", errors="replace")
                if len(encoded) > MAX_JSONL_LINE_BYTES:
                    # Bound memory; do not retain or report the oversized content.
                    continue
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise ValidationError(
                        f"malformed JSON in Codex rollout at line {line_number}"
                    ) from exc
                if not isinstance(payload, dict):
                    continue

                event_type, event_payload, timestamp = _unwrap_event(payload)
                if event_type is None or event_payload is None:
                    continue

                if event_type == SESSION_META_EVENT_TYPE:
                    saw_session_meta = True
                    meta_id = _extract_session_meta_id(event_payload)
                    if meta_id is not None and meta_id.lower() == session_id.lower():
                        matched_session_meta = True
                    continue

                if event_type not in ALLOWLISTED_SETTINGS_EVENT_TYPES:
                    continue

                update = _extract_scalar_update(event_type, event_payload, timestamp)
                if update.model is not None:
                    latest_model = update.model
                    latest_model_meta = (update.event_type, update.timestamp)
                if update.reasoning_effort is not None:
                    latest_reasoning = update.reasoning_effort
                    latest_reasoning_meta = (update.event_type, update.timestamp)
    except OSError as exc:
        raise ValidationError("failed to read Codex rollout for the requested session") from exc

    # Desktop rollouts may embed forked parent session_meta entries. Require that at
    # least one session_meta id matches the requested session when any are present.
    if saw_session_meta and not matched_session_meta:
        raise ValidationError(
            "Codex rollout session_meta id does not match the requested session id"
        )

    if latest_model is None or not latest_model.strip():
        raise ValidationError(
            "Codex session runtime is missing a model value in allowlisted rollout events"
        )
    if latest_reasoning is None:
        raise ValidationError(
            "Codex session runtime is missing a reasoning effort value in allowlisted "
            "rollout events"
        )
    if latest_reasoning not in CODEX_REVIEW_REASONING_EFFORTS:
        raise ValidationError(
            "Codex session runtime reasoning effort is not one of the supported values: "
            f"{sorted(CODEX_REVIEW_REASONING_EFFORTS)}"
        )

    source_event_type, source_timestamp = _choose_source_meta(
        latest_model_meta,
        latest_reasoning_meta,
    )
    return CodexSessionRuntime(
        session_id=session_id,
        model=latest_model.strip(),
        reasoning_effort=latest_reasoning,
        origin=origin,
        source_event_type=source_event_type,
        source_timestamp=source_timestamp,
    )


def _unwrap_event(
    payload: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Return (event_type, event_payload_dict, timestamp) for allowlisted shapes."""

    timestamp = _optional_string(payload.get("timestamp"))
    top_type = payload.get("type")
    if not isinstance(top_type, str):
        return None, None, timestamp

    nested = payload.get("payload")
    if top_type in WRAPPER_EVENT_TYPES and isinstance(nested, dict):
        nested_type = nested.get("type")
        if isinstance(nested_type, str):
            return nested_type, nested, timestamp or _optional_string(nested.get("timestamp"))
        return None, None, timestamp

    if top_type == SESSION_META_EVENT_TYPE:
        if isinstance(nested, dict):
            return SESSION_META_EVENT_TYPE, nested, timestamp
        return SESSION_META_EVENT_TYPE, payload, timestamp

    if top_type in ALLOWLISTED_SETTINGS_EVENT_TYPES:
        if isinstance(nested, dict):
            # Prefer nested payload when present; otherwise treat top-level as payload.
            return top_type, nested, timestamp
        return top_type, payload, timestamp

    return None, None, timestamp


def _extract_session_meta_id(payload: dict[str, Any]) -> str | None:
    for key in ("id", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_scalar_update(
    event_type: str,
    payload: dict[str, Any],
    timestamp: str | None,
) -> _ScalarUpdate:
    model = _optional_nonempty_string(payload.get("model"))
    reasoning = _extract_reasoning_scalar(payload)

    # Production Desktop shape: thread_settings_applied.thread_settings.{model,reasoning_effort}
    thread_settings = payload.get("thread_settings")
    if isinstance(thread_settings, dict):
        if model is None:
            model = _optional_nonempty_string(thread_settings.get("model"))
        if reasoning is None:
            reasoning = _extract_reasoning_scalar(thread_settings)

    # Some Codex shapes nest settings under a "settings" object with scalars only.
    settings = payload.get("settings")
    if isinstance(settings, dict):
        if model is None:
            model = _optional_nonempty_string(settings.get("model"))
        if reasoning is None:
            reasoning = _extract_reasoning_scalar(settings)

    if reasoning is not None and reasoning not in CODEX_REVIEW_REASONING_EFFORTS:
        # Ignore invalid reasoning scalars rather than retaining unknown values.
        reasoning = None
    return _ScalarUpdate(
        model=model,
        reasoning_effort=reasoning,
        event_type=event_type,
        timestamp=timestamp,
    )


def _extract_reasoning_scalar(payload: dict[str, Any]) -> str | None:
    """Extract allowlisted reasoning scalars from known Codex rollout shapes.

    Supported keys (first match wins):
    - ``reasoning_effort`` / ``model_reasoning_effort`` (generic / CLI-oriented)
    - ``effort`` (production Desktop ``turn_context``)
    """

    return _optional_nonempty_string(
        payload.get("reasoning_effort")
        or payload.get("model_reasoning_effort")
        or payload.get("effort")
    )


def _choose_source_meta(
    model_meta: tuple[str, str | None] | None,
    reasoning_meta: tuple[str, str | None] | None,
) -> tuple[str, str | None]:
    if model_meta is None and reasoning_meta is None:
        return "unknown", None
    if model_meta is None:
        assert reasoning_meta is not None
        return reasoning_meta
    if reasoning_meta is None:
        return model_meta
    # Prefer the later timestamp when both are present; otherwise prefer turn_context
    # over thread_settings_applied as the more recent turn-scoped event type name.
    model_ts = model_meta[1]
    reasoning_ts = reasoning_meta[1]
    if model_ts and reasoning_ts:
        return reasoning_meta if reasoning_ts >= model_ts else model_meta
    if reasoning_ts and not model_ts:
        return reasoning_meta
    if model_ts and not reasoning_ts:
        return model_meta
    if reasoning_meta[0] == "turn_context":
        return reasoning_meta
    return model_meta


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_nonempty_string(value: Any) -> str | None:
    return _optional_string(value)


def model_family(model: str) -> str | None:
    """Classify GPT-5.5 / GPT-5.6 families for operational warnings only."""

    lowered = model.strip().lower()
    if lowered.startswith("gpt-5.6") or lowered.startswith("gpt-5-6"):
        return "gpt-5.6"
    if lowered.startswith("gpt-5.5") or lowered.startswith("gpt-5-5"):
        return "gpt-5.5"
    return None


CROSS_FAMILY_COMPACTION_WARNING = (
    "Reanudar una sesion GPT-5.5 con GPT-5.6, o una GPT-5.6 con GPT-5.5, puede forzar "
    "compactacion previa por diferencias de contexto y restricciones entre familias. "
    "En las versiones validadas se observo `pre-sampling compact`. Evita cambiar de "
    "familia dentro del mismo run y usa el modelo original de la sesion."
)


def cross_family_compaction_warning(session_model: str, review_model: str) -> str | None:
    session_family = model_family(session_model)
    review_family = model_family(review_model)
    if session_family is None or review_family is None:
        return None
    if session_family == review_family:
        return None
    return CROSS_FAMILY_COMPACTION_WARNING
