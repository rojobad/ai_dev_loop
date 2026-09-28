"""On-demand local Codex capacity observation for the Integration API."""

from __future__ import annotations

import shutil
from collections.abc import Callable

from ai_dev_loop.integration_api.capacity_projection import map_capacity_observation_to_data
from ai_dev_loop.integration_api.models import IntegrationCodexCapacityData
from ai_dev_loop.scheduler.application.codex_capacity_probe import (
    CodexCapacityObservation,
    CodexCapacityProbePort,
    CodexCapacityReason,
    CodexCapacityStatus,
    default_capacity_probe,
)


def resolve_local_codex_command() -> str | None:
    """Locate the locally installed ``codex`` executable on PATH."""

    return shutil.which("codex")


def observe_codex_capacity(
    *,
    probe_factory: Callable[[], CodexCapacityProbePort] | None = None,
    codex_command_resolver: Callable[[], str | None] | None = None,
) -> IntegrationCodexCapacityData:
    """Run one bounded app-server capacity probe under the operator's local Codex context."""

    resolve = codex_command_resolver or resolve_local_codex_command
    codex_command = resolve()
    if codex_command is None:
        unavailable = CodexCapacityObservation(
            status=CodexCapacityStatus.UNAVAILABLE,
            reason=CodexCapacityReason.PROCESS_FAILURE,
        )
        return map_capacity_observation_to_data(unavailable)

    factory = probe_factory or default_capacity_probe
    observation = factory().probe(codex_command)
    return map_capacity_observation_to_data(observation)


def default_capacity_read_service() -> CodexCapacityReadService:
    return CodexCapacityReadService()


class CodexCapacityReadService:
    """Read-only capacity observation boundary for CLI adapters."""

    def __init__(
        self,
        *,
        probe_factory: Callable[[], CodexCapacityProbePort] | None = None,
        codex_command_resolver: Callable[[], str | None] | None = None,
    ) -> None:
        self._probe_factory = probe_factory
        self._codex_command_resolver = codex_command_resolver

    def observe(self) -> IntegrationCodexCapacityData:
        return observe_codex_capacity(
            probe_factory=self._probe_factory,
            codex_command_resolver=self._codex_command_resolver,
        )
