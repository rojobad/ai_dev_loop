"""Public Integration API client for Phase 21.6 acceptance (CLI argv only)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_dev_loop.integration_api.consumer import (
    ConsumerGate,
    evaluate_envelope_major,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class PublicIntegrationBridgeClient:
    """Minimal fake Bridge consumer that may only invoke the public integration CLI."""

    env: Mapping[str, str]
    cwd: Path = REPO_ROOT
    python: Sequence[str] = field(default_factory=lambda: (sys.executable, "-m", "ai_dev_loop.cli"))
    resource_requests: int = 0
    _gate: ConsumerGate | None = field(default=None, init=False)

    def _argv(self, integration_args: Sequence[str]) -> list[str]:
        return [*self.python, "integration", *integration_args, "--output", "json"]

    def invoke(self, integration_args: Sequence[str]) -> dict[str, Any]:
        if self._gate is ConsumerGate.UPDATE_REQUIRED:
            raise AssertionError("client blocked after UPDATE_REQUIRED")
        proc = subprocess.run(
            self._argv(integration_args),
            cwd=self.cwd,
            capture_output=True,
            text=True,
            env=dict(self.env),
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        if integration_args and integration_args[0] != "info":
            self.resource_requests += 1
        if integration_args[:1] == ["info"] and self._gate is None:
            gate = evaluate_envelope_major(payload)
            self._gate = gate
            if gate is ConsumerGate.UPDATE_REQUIRED:
                return payload
        elif self._gate is None:
            self._gate = evaluate_envelope_major(payload)
        return payload

    def fetch_artifact_bytes(
        self,
        integration_args: Sequence[str],
        *,
        chunk_size: int = 65536,
    ) -> bytes:
        offset = 0
        parts: list[bytes] = []
        while True:
            args = [
                *integration_args,
                "--offset",
                str(offset),
                "--limit",
                str(chunk_size),
            ]
            payload = self.invoke(args)
            data = payload["data"]
            assert isinstance(data, dict)
            if not data.get("available"):
                break
            import base64

            parts.append(base64.b64decode(str(data["contentBase64"])))
            if not data.get("hasMore"):
                break
            next_offset = data.get("nextOffset")
            assert next_offset is not None
            offset = int(next_offset)
        return b"".join(parts)
