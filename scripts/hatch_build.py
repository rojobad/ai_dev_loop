"""Hatch build guard for the generated Phase 26 dashboard distribution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ModuleNotFoundError:  # pragma: no cover - lets focused tests import the validator.

    class BuildHookInterface:  # type: ignore[no-redef]
        root: str


_OUTPUTS = ("assets/dashboard.css", "assets/dashboard.js", "index.html")
_DASHBOARD_SCHEMAS = (
    "integration-api-dashboard-activity-data-v1.json",
    "integration-api-dashboard-projects-data-v1.json",
    "integration-api-dashboard-runs-data-v1.json",
    "integration-api-dashboard-shared-v1.json",
    "integration-api-dashboard-summary-data-v1.json",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_phase26_build_inputs(root: Path) -> None:
    """Reject missing, stale, oversized, or source-mapped dashboard package inputs."""

    distribution = root / "src/ai_dev_loop/dashboard/web_dist"
    manifest_path = distribution / "build-manifest.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "dashboard build manifest is missing or invalid; run npm run build"
        ) from exc
    if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
        raise RuntimeError("dashboard build manifest schema is unsupported; run npm run build")
    inputs = raw.get("inputs")
    outputs = raw.get("outputs")
    if not isinstance(inputs, dict) or not inputs or not isinstance(outputs, dict):
        raise RuntimeError("dashboard build manifest is incomplete; run npm run build")

    for relative_name, expected_digest in inputs.items():
        if not isinstance(relative_name, str) or not isinstance(expected_digest, str):
            raise RuntimeError("dashboard build manifest inputs are invalid; run npm run build")
        path = (root / relative_name).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise RuntimeError("dashboard build manifest input escapes the repository") from exc
        if not path.is_file() or path.is_symlink() or _sha256(path) != expected_digest:
            raise RuntimeError(f"dashboard build input is missing or stale: {relative_name}")

    if set(outputs) != set(_OUTPUTS):
        raise RuntimeError(
            "dashboard build manifest output inventory is invalid; run npm run build"
        )
    for relative_name in _OUTPUTS:
        metadata = outputs.get(relative_name)
        path = distribution / relative_name
        if not isinstance(metadata, dict):
            raise RuntimeError("dashboard build manifest output metadata is invalid")
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"dashboard build output is missing: {relative_name}")
        size = path.stat().st_size
        if size <= 0 or size > 10 * 1024 * 1024:
            raise RuntimeError(f"dashboard build output size is invalid: {relative_name}")
        if metadata.get("size") != size or metadata.get("sha256") != _sha256(path):
            raise RuntimeError(f"dashboard build output is stale: {relative_name}")

    if any(path.is_file() for path in distribution.rglob("*.map")):
        raise RuntimeError("dashboard build must not package source maps")
    for name in _DASHBOARD_SCHEMAS:
        if not (root / "src/ai_dev_loop/schemas" / name).is_file():
            raise RuntimeError(f"dashboard public schema is missing: {name}")


class CustomBuildHook(BuildHookInterface):
    """Fail package creation when committed generated dashboard assets are stale."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        del version, build_data
        validate_phase26_build_inputs(Path(self.root))
