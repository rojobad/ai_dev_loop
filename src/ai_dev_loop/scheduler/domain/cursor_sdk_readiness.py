"""Typed creation-readiness facts for one Cursor SDK agent.

Public status, empty history, or timestamps alone do not prove that a native run
is creation-time work. Callers supply an authenticated creation inventory and a
fresh public observation; this module only classifies those facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MAX_CREATION_READINESS_OBSERVATIONS = 3
MAX_CREATION_READINESS_SECONDS = 10.0

TERMINAL_PUBLIC_RUN_STATUSES = frozenset(
    {"finished", "cancelled", "canceled", "expired", "error", "failed"}
)
CREATION_PLACEHOLDER_STATUSES = frozenset({"queued", "running"})


@dataclass(frozen=True)
class PublicRunFact:
    run_id: str
    status: str


@dataclass(frozen=True)
class CreationInventoryFact:
    """Authenticated create-time public inventory for one exact agent."""

    runs: tuple[PublicRunFact, ...]
    message_count: int
    authorized_send_count: int


@dataclass(frozen=True)
class ReadinessDecision:
    action: Literal["send", "cancel", "hold"]
    occupant_id: str | None
    reason: str


def _hold(reason: str) -> ReadinessDecision:
    return ReadinessDecision(action="hold", occupant_id=None, reason=reason)


def classify_creation_readiness(
    inventory: CreationInventoryFact,
    observed_runs: tuple[PublicRunFact, ...],
    *,
    message_count: int,
) -> ReadinessDecision:
    """Decide whether one authenticated creation occupant may be cancelled.

    ``queued`` or public ``running`` (including SDK ``creating`` normalization)
    is eligible only together with exclusive ownership, no authorized send, and
    empty public agent history. Unknown status stays nonterminal and held.
    """

    if isinstance(inventory.message_count, bool) or not isinstance(inventory.message_count, int):
        return _hold("creation message count is not an integer")
    if isinstance(inventory.authorized_send_count, bool) or not isinstance(
        inventory.authorized_send_count, int
    ):
        return _hold("authorized send count is not an integer")
    if isinstance(message_count, bool) or not isinstance(message_count, int):
        return _hold("public message count is not an integer")
    if inventory.authorized_send_count != 0 or inventory.message_count != 0 or message_count != 0:
        return _hold("creation occupant has authorized work or public history")
    if len(inventory.runs) > 1:
        return _hold("creation inventory is not exclusive")
    observed_ids = [run.run_id for run in observed_runs]
    if any(not run.run_id or not run.status for run in observed_runs):
        return _hold("public run identity is incomplete")
    if len(set(observed_ids)) != len(observed_ids):
        return _hold("public run identities conflict")
    if not inventory.runs:
        if observed_runs:
            return _hold("public runs appeared without a creation occupant")
        return ReadinessDecision(action="send", occupant_id=None, reason="no creation occupant")
    occupant = inventory.runs[0]
    if occupant.run_id not in observed_ids or len(observed_runs) != 1:
        return _hold("creation occupant is missing, foreign, or no longer exclusive")
    current = observed_runs[0]
    if current.run_id != occupant.run_id:
        return _hold("creation occupant identity changed")
    status = current.status.strip().lower()
    if status in TERMINAL_PUBLIC_RUN_STATUSES:
        return ReadinessDecision(
            action="send",
            occupant_id=None,
            reason="creation occupant is already terminal",
        )
    if status in CREATION_PLACEHOLDER_STATUSES:
        return ReadinessDecision(
            action="cancel",
            occupant_id=current.run_id,
            reason="authenticated creation placeholder",
        )
    return _hold("creation occupant status is unknown")
