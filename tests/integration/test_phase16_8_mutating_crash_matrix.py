"""Phase 16.8 parameterized mutating-effect crash windows through production paths."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.phase16_4_matrix_helpers import make_engine
from tests.integration.phase16_8_checkpoint_helpers import RECONCILE_MATRIX
from tests.integration.phase16_8_helpers import MUTATING_CRASH_WINDOWS, policy_for_kind
from tests.integration.phase16_8_mutating_helpers import (
    assemble_write_production_stack,
    build_mutating_effect,
    execute_abort_during_mutating_claim,
    execute_ambiguous_apply_timeout_with_reconcile,
    execute_apply_before_complete,
    execute_complete_before_worker_failure,
    execute_mutating_lease_loss,
    execute_stale_authority_write,
    external_mutation_count,
    gateway_write,
    gh_mutation_total,
    make_prepared,
    queue_mutation_failure,
)
from tests.unit.pr_review_v2.durable_helpers import FakeClock

from ai_dev_loop.pr_review_v2.application.write_contracts import AmbiguousWriteError
from ai_dev_loop.pr_review_v2.domain.effects import MUTATING_KINDS
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import (
    GitTransportError as GitCliTransportError,
)

_MUTATING_MATRIX = [
    (effect_kind, crash_window)
    for effect_kind in sorted(MUTATING_KINDS)
    for crash_window in MUTATING_CRASH_WINDOWS
]

_GH_KINDS = frozenset(RECONCILE_MATRIX)


@pytest.mark.parametrize(
    ("effect_kind", "crash_window"),
    _MUTATING_MATRIX,
    ids=[f"{kind}-{window}" for kind, window in _MUTATING_MATRIX],
)
def test_mutating_effect_crash_window_contract(
    tmp_path: Path,
    effect_kind: str,
    crash_window: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = policy_for_kind(effect_kind)
    assert row.authority == "MUTATING"
    assert crash_window in MUTATING_CRASH_WINDOWS

    if crash_window == "before_write":
        _assert_before_write_zero_mutations(tmp_path, effect_kind)
        return
    if crash_window == "ambiguous_apply_timeout":
        _assert_ambiguous_apply_timeout(tmp_path, effect_kind, monkeypatch)
        return
    if crash_window == "apply_before_complete":
        _assert_apply_before_complete_reconciles_applied(tmp_path, effect_kind, monkeypatch)
        return
    if crash_window == "complete_before_worker_failure":
        _assert_complete_before_worker_failure_no_duplicate(tmp_path, effect_kind, monkeypatch)
        return
    if crash_window == "stale_authority":
        _assert_stale_authority_fences_write(tmp_path, effect_kind, monkeypatch)
        return
    if crash_window == "lease_loss":
        _assert_lease_loss_preserves_effect_identity(tmp_path, effect_kind, monkeypatch)
        return
    if crash_window == "abort":
        _assert_abort_durably_fences_mutating_claim(tmp_path, effect_kind, monkeypatch)


def _assert_before_write_zero_mutations(tmp_path: Path, effect_kind: str) -> None:
    stack = assemble_write_production_stack(tmp_path)
    if effect_kind in _GH_KINDS:
        queue_mutation_failure(stack.gh_bundle.controller, "dns")
        effect = build_mutating_effect(effect_kind, stack)
        with pytest.raises((GhTransportError, AmbiguousWriteError)):
            gateway_write(stack, effect)
        assert gh_mutation_total(stack.gh_bundle.controller) == 0
        return

    if effect_kind == "commit_patch":
        effect = build_mutating_effect(
            effect_kind,
            stack,
        ).model_copy(update={"expected_head_sha": "0" * 40})
        with pytest.raises(GitCliTransportError):
            gateway_write(stack, effect)
        head = _git_head(stack.git_repo["work"])
        assert head == stack.git_repo["parent"]
        return

    if effect_kind == "push_commit":
        effect = build_mutating_effect(effect_kind, stack).model_copy(
            update={"commit_sha": "0" * 40, "bound_head_sha": stack.git_repo["parent"]}
        )
        with pytest.raises(GitCliTransportError):
            gateway_write(stack, effect)
        return

    pytest.fail(f"before_write not implemented for {effect_kind}")


def _assert_ambiguous_apply_timeout(
    tmp_path: Path, effect_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    db_path = tmp_path / "ambiguous.sqlite3"
    prepared = make_prepared(clock, run_id=f"run-mut-ambiguous-{effect_kind}")
    execute_ambiguous_apply_timeout_with_reconcile(
        db_path,
        prepared,
        clock,
        effect_kind,
        tmp_path / "stack",
        engine_prefix=f"p168-ambiguous-{effect_kind}",
    )


def _assert_apply_before_complete_reconciles_applied(
    tmp_path: Path, effect_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    db_path = tmp_path / "apply.sqlite3"
    prepared = make_prepared(clock, run_id=f"run-mut-apply-{effect_kind}")
    stack = assemble_write_production_stack(tmp_path / "stack", run_id=prepared.run_id)
    execute_apply_before_complete(
        db_path,
        stack,
        prepared,
        clock,
        effect_kind,
        engine_prefix=f"p168-apply-{effect_kind}",
    )
    assert external_mutation_count(stack, effect_kind) >= 1


def _assert_complete_before_worker_failure_no_duplicate(
    tmp_path: Path, effect_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    db_path = tmp_path / "complete.sqlite3"
    prepared = make_prepared(clock, run_id=f"run-mut-complete-{effect_kind}")
    stack = assemble_write_production_stack(tmp_path / "stack", run_id=prepared.run_id)
    execute_complete_before_worker_failure(
        db_path,
        stack,
        prepared,
        clock,
        effect_kind,
        engine_prefix=f"p168-complete-{effect_kind}",
    )


def _assert_stale_authority_fences_write(
    tmp_path: Path,
    effect_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "fence.sqlite3", clock, prefix=f"p168-mut-{effect_kind}")
    prepared = make_prepared(clock, run_id=f"run-mut-fence-{effect_kind}")
    stack = assemble_write_production_stack(tmp_path / "stack", run_id=prepared.run_id)
    execute_stale_authority_write(stack, engine, prepared, clock, effect_kind)


def _assert_lease_loss_preserves_effect_identity(
    tmp_path: Path,
    effect_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "lease.sqlite3", clock, prefix=f"p168-lease-{effect_kind}")
    prepared = make_prepared(clock, run_id=f"run-mut-lease-{effect_kind}")
    execute_mutating_lease_loss(engine, prepared, clock, effect_kind)


def _assert_abort_durably_fences_mutating_claim(
    tmp_path: Path,
    effect_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "abort.sqlite3", clock, prefix=f"p168-abort-{effect_kind}")
    prepared = make_prepared(clock, run_id=f"run-mut-abort-{effect_kind}")
    execute_abort_during_mutating_claim(engine, prepared, clock, effect_kind)


def _git_head(work: Path) -> str:
    import subprocess

    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(work), text=True).strip()
