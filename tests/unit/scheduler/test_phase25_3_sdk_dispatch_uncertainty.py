"""R-01 ambiguity and abort convergence tests: observation never becomes send."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase25_3_sdk_recovery import (
    AGENT,
    NATIVE,
    RecoveryAdapter,
    _evidence,
)

from ai_dev_loop.integrations.cursor_sdk.port import (
    CursorSdkOutcome,
    CursorSdkRunPage,
    CursorSdkTerminalRun,
)
from ai_dev_loop.scheduler.cursor_attempt_runner import _run_sdk_reconciliation_operation
from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import CursorSdkReconciliationRecordV1


class InventoryAdapter(RecoveryAdapter):
    def __init__(self, repo: Path, run_ids: tuple[str, ...]):
        super().__init__(repo)
        self.run_ids = run_ids
        self.statuses = {run_id: "running" for run_id in run_ids}

    def list_runs(self, agent_id: str, *, cursor=None, limit=None):  # type: ignore[no-untyped-def]
        del cursor, limit
        return CursorSdkOutcome.success(
            CursorSdkRunPage(
                tuple(
                    CursorSdkTerminalRun(AGENT, run_id, self.statuses[run_id], "", None)
                    for run_id in self.run_ids
                ),
                None,
            )
        )

    def get_run(self, agent_id: str, run_id: str):  # type: ignore[no-untyped-def]
        return CursorSdkOutcome.success(
            CursorSdkTerminalRun(AGENT, run_id, self.statuses[run_id], "", None)
        )

    def cancel(self, agent_id: str, run_id: str):  # type: ignore[no-untyped-def]
        self.statuses[run_id] = "cancelled"
        return super().cancel(agent_id, run_id)


@pytest.mark.parametrize(
    ("run_ids", "expected"),
    [
        ((), "ambiguous_missing"),
        ((NATIVE, "run-019abc00-1111-4222-8333-666666666666"), "ambiguous_multiple"),
    ],
)
def test_ambiguous_send_never_speculatively_redispatches(
    monkeypatch,
    tmp_path: Path,
    run_ids: tuple[str, ...],
    expected: str,
) -> None:  # type: ignore[no-untyped-def]
    evidence, root = _evidence(tmp_path, native=None)
    adapter = InventoryAdapter(Path(str(evidence["repository_root"])), run_ids)
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter", lambda value: adapter
    )

    outcome = _run_sdk_reconciliation_operation(
        evidence, root, attempt_id=str(evidence["attempt_id"])
    )

    assert outcome["reconciliation_outcome"] == expected
    assert adapter.send_calls == 0
    record = CursorSdkReconciliationRecordV1.model_validate_json(
        (root / str(outcome["reconciliation_record_path"])).read_bytes()
    )
    assert record.native_run_id is None


def test_unique_post_baseline_run_is_adopted_then_cancelled_without_redispatch(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    evidence, root = _evidence(tmp_path, native=None)
    adapter = InventoryAdapter(Path(str(evidence["repository_root"])), (NATIVE,))
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter", lambda value: adapter
    )

    outcome = _run_sdk_reconciliation_operation(
        evidence, root, attempt_id=str(evidence["attempt_id"])
    )

    assert outcome["reconciliation_outcome"] == "continuation_ready"
    assert adapter.cancel_calls == [NATIVE]
    assert adapter.send_calls == 0
    record = CursorSdkReconciliationRecordV1.model_validate_json(
        (root / str(outcome["reconciliation_record_path"])).read_bytes()
    )
    assert record.candidate_run_ids == (NATIVE,)
    assert record.native_run_id == NATIVE


def test_abort_cancels_and_confirms_every_post_baseline_candidate(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    evidence, root = _evidence(tmp_path, native=None)
    run_ids = (NATIVE, "run-019abc00-1111-4222-8333-666666666666")
    evidence["abort_requested"] = True
    adapter = InventoryAdapter(Path(str(evidence["repository_root"])), run_ids)
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter", lambda value: adapter
    )

    outcome = _run_sdk_reconciliation_operation(
        evidence, root, attempt_id=str(evidence["attempt_id"])
    )

    assert outcome["reconciliation_outcome"] == "abort_reconciled"
    assert set(adapter.cancel_calls) == set(run_ids)
    assert adapter.send_calls == 0
    record = CursorSdkReconciliationRecordV1.model_validate_json(
        (root / str(outcome["reconciliation_record_path"])).read_bytes()
    )
    assert set(record.candidate_run_ids) == set(run_ids)
