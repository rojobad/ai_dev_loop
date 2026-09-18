"""Subprocess environment helpers for offline integration wheel tests."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OFFLINE_WHEELHOUSE = REPO_ROOT / "tests" / "fixtures" / "integration_api" / "offline_wheelhouse"
OFFLINE_WHEELHOUSE_MARKER = OFFLINE_WHEELHOUSE / ".complete"


def offline_subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in (base or os.environ).items() if isinstance(value, str)}
    env.update(
        {
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "UV_NO_INDEX": "1",
            "UV_OFFLINE": "1",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "NO_PROXY": "*",
        }
    )
    env.pop("PYTHONPATH", None)
    return env


def wheelhouse_ready() -> bool:
    return OFFLINE_WHEELHOUSE_MARKER.is_file() and any(OFFLINE_WHEELHOUSE.glob("*.whl"))
