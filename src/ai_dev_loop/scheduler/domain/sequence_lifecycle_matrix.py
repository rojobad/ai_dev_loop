"""Table-driven sequence lifecycle transition authority for Phase 20.9."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SequenceStateKind = Literal[
    "prepared",
    "active",
    "abort_pending",
    "blocked",
    "aborted",
    "awaiting_finalization",
]

_TRANSITION_KIND = Literal["legal", "illegal"]


@dataclass(frozen=True)
class LifecycleTransitionCase:
    current_kind: SequenceStateKind
    new_kind: SequenceStateKind
    expectation: _TRANSITION_KIND
    reason: str


_LEGAL_TRANSITIONS: frozenset[tuple[SequenceStateKind, SequenceStateKind]] = frozenset(
    {
        ("prepared", "active"),
        ("prepared", "aborted"),
        ("active", "active"),
        ("active", "abort_pending"),
        ("active", "blocked"),
        ("active", "awaiting_finalization"),
        ("abort_pending", "aborted"),
        ("blocked", "active"),
        ("blocked", "aborted"),
        ("awaiting_finalization", "awaiting_finalization"),
    }
)


def iter_sequence_lifecycle_transition_matrix() -> tuple[LifecycleTransitionCase, ...]:
    kinds: tuple[SequenceStateKind, ...] = (
        "prepared",
        "active",
        "abort_pending",
        "blocked",
        "aborted",
        "awaiting_finalization",
    )
    cases: list[LifecycleTransitionCase] = []
    for current in kinds:
        for new in kinds:
            if current == new and new != "awaiting_finalization":
                continue
            if (current, new) in _LEGAL_TRANSITIONS:
                cases.append(
                    LifecycleTransitionCase(current, new, "legal", "supported lifecycle transition")
                )
            else:
                cases.append(
                    LifecycleTransitionCase(
                        current,
                        new,
                        "illegal",
                        "transition outside supported lifecycle contract",
                    )
                )
    return tuple(cases)


def transition_expectation(
    current_kind: SequenceStateKind,
    new_kind: SequenceStateKind,
) -> _TRANSITION_KIND:
    if (current_kind, new_kind) in _LEGAL_TRANSITIONS:
        return "legal"
    return "illegal"
