"""Native WSL Codex subprocess environment policy for scheduler children."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

_DRVFS_CONTAMINATION_PREFIX = "/mnt/"
_CODEX_HOME_KEYS = frozenset({"CODEX_HOME", "CODEX_SQLITE_HOME"})


def _path_resolves_under_drvfs(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    try:
        resolved = Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return text.startswith(_DRVFS_CONTAMINATION_PREFIX)
    return str(resolved).startswith(_DRVFS_CONTAMINATION_PREFIX)


def sanitize_codex_subprocess_env(
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment with DrvFS Codex home contamination removed.

    Inherited ``CODEX_HOME`` or ``CODEX_SQLITE_HOME`` values that resolve under
    ``/mnt/*`` are omitted so Codex falls back to native WSL defaults. Native WSL
    overrides and unrelated variables are preserved. Does not mutate ``os.environ``.
    """

    source = dict(base_env) if base_env is not None else dict(os.environ)
    sanitized: dict[str, str] = {}
    for key, value in source.items():
        if key in _CODEX_HOME_KEYS and _path_resolves_under_drvfs(value):
            continue
        sanitized[key] = value
    return sanitized
