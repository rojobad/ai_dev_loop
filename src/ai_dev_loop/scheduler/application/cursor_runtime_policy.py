"""Final Cursor runtime cutover policy.

Historical Cursor CLI bindings stay parseable for inspection, but they no longer
authorize model execution.  The effective implementer provider is the important
discriminator: Codex-only records may retain an inert historical ``cursor``
binding without becoming Cursor CLI work.
"""

from __future__ import annotations

from ai_dev_loop.config import CursorSection, ProjectConfig
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.scheduler.domain.sequence import FrozenSequenceEntry, PreparedSequenceDefinition
from ai_dev_loop.scheduler.domain.state import (
    CursorBinding,
    CursorSdkBinding,
    SubmittedRunContext,
    resolved_implementer_provider,
)

CURSOR_CLI_RETIRED_SUMMARY = (
    "Cursor CLI execution is retired; submit Cursor work with project config version 2 "
    "and runtime sdk-local, or prepare a fresh named manifest v4. Historical records "
    "remain readable and Codex-only execution remains supported."
)


def effective_config_uses_cursor_cli(config: ProjectConfig) -> bool:
    """Return whether a new admission selects Cursor through the retired CLI."""

    return config.implementer.provider == "cursor" and isinstance(config.cursor, CursorSection)


def context_uses_cursor_cli(context: SubmittedRunContext) -> bool:
    """Classify frozen execution authority without trusting config/schema age alone."""

    return resolved_implementer_provider(context) == "cursor" and isinstance(
        context.cursor, CursorBinding
    )


def entry_uses_cursor_cli(entry: FrozenSequenceEntry) -> bool:
    """Classify one frozen sequence entry by effective provider and runtime."""

    provider = "cursor" if entry.implementer is None else entry.implementer.provider
    return provider == "cursor" and isinstance(entry.cursor, CursorBinding)


def require_supported_effective_config(config: ProjectConfig, *, action: str) -> None:
    if effective_config_uses_cursor_cli(config):
        raise ValidationError(f"{action}: {CURSOR_CLI_RETIRED_SUMMARY}")


def require_supported_context(context: SubmittedRunContext, *, action: str) -> None:
    if context_uses_cursor_cli(context):
        raise ValidationError(f"{action}: {CURSOR_CLI_RETIRED_SUMMARY}")
    if resolved_implementer_provider(context) == "cursor" and not isinstance(
        context.cursor, CursorSdkBinding
    ):
        raise ValidationError(f"{action}: unsupported frozen Cursor runtime")


def unsupported_sequence_cursor_cli_ordinals(
    definition: PreparedSequenceDefinition,
) -> tuple[int, ...]:
    return tuple(entry.ordinal for entry in definition.entries if entry_uses_cursor_cli(entry))


def require_supported_sequence(
    definition: PreparedSequenceDefinition,
    *,
    action: str,
) -> None:
    ordinals = unsupported_sequence_cursor_cli_ordinals(definition)
    if not ordinals:
        return
    rendered = ", ".join(str(value) for value in ordinals)
    raise ValidationError(
        f"{action}: {CURSOR_CLI_RETIRED_SUMMARY} Unsupported sequence ordinals: {rendered}."
    )


def require_supported_entry(entry: FrozenSequenceEntry, *, action: str) -> None:
    if entry_uses_cursor_cli(entry):
        raise ValidationError(
            f"{action}: {CURSOR_CLI_RETIRED_SUMMARY} Unsupported sequence ordinal: {entry.ordinal}."
        )
