"""Mutable scheduler-owned presentation metadata."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from ai_dev_loop.errors import ValidationError

MAX_SHORT_NAME_CODEPOINTS = 35


def require_short_name(value: object, *, field_name: str = "short name") -> str:
    """Validate a chosen presentation name without coercion or truncation."""

    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValidationError(f"{field_name} must not contain control characters")
    cleaned = value.strip()
    if not cleaned:
        raise ValidationError(f"{field_name} must not be blank")
    if len(cleaned) > MAX_SHORT_NAME_CODEPOINTS:
        raise ValidationError(
            f"{field_name} must be at most {MAX_SHORT_NAME_CODEPOINTS} Unicode code points"
        )
    return cleaned


def legacy_short_name(value: object, *, fallback_id: str) -> str:
    """Project one historical label into the bounded presentation contract."""

    candidate = value.strip() if isinstance(value, str) else ""
    if not candidate:
        candidate = fallback_id.strip()
    # Existing identifiers are nonempty scheduler authority. Keep this defensive
    # fallback local to presentation so malformed labels never become identity.
    if not candidate:
        candidate = "unknown"
    cleaned = "".join(
        character for character in candidate if unicodedata.category(character) != "Cc"
    ).strip()
    if not cleaned:
        cleaned = fallback_id.strip() or "unknown"
    return cleaned[:MAX_SHORT_NAME_CODEPOINTS]


@dataclass(frozen=True)
class ShortNameUpdateResult:
    owner_id: str
    short_name: str
    previous_short_name: str
    changed: bool
