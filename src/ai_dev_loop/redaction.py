"""Basic secret and environment redaction helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[a-z0-9\-._~+/]+=*"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{20,}"),
)

_SENSITIVE_ENV_KEYS = frozenset(
    {
        "API_KEY",
        "OPENAI_API_KEY",
        "CURSOR_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)


def redact_text(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    return redacted


def redact_env(env: Mapping[str, str]) -> dict[str, str]:
    return {
        key: ("<redacted>" if key in _SENSITIVE_ENV_KEYS else value) for key, value in env.items()
    }
