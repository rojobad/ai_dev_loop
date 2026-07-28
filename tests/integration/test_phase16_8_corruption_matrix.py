"""Phase 16.8 Step 10 corruption and drift matrix over protected artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.integration.test_phase16_8_control_matrix import PLAN_BYTES, PROMPT_BYTES, _ctx
from tests.unit.pr_review_v2.github_read_helpers import MARKER, binding

from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExternalAdjudicationDecision,
    ExternalAdjudicationResultArtifact,
    LocalFixResultArtifact,
    PublicationGenerationResultArtifact,
)
from ai_dev_loop.pr_review_v2.application.github_read import (
    ObservationEvidenceKind,
    ObservationSnapshot,
    ObservedTriggerComment,
)
from ai_dev_loop.pr_review_v2.application.preparation import PreparationService, SourceRunSnapshot
from ai_dev_loop.pr_review_v2.application.write_contracts import DEFAULT_MAX_PATCH_BYTES
from ai_dev_loop.pr_review_v2.domain import (
    AdjudicationDecisionKind,
    ArtifactRef,
    LocalFixOutcomeKind,
)
from ai_dev_loop.pr_review_v2.infrastructure.paths import run_artifact_root
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    ProtectedResultStore,
    ProtectedResultStoreError,
)
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import (
    ArtifactStoreError,
    ReviewArtifactStore,
    sha256_text,
)
from ai_dev_loop.state import sha256_bytes


def _ctx_for(repo_root: str, *, source_run_id: str = "src-corrupt"):
    ctx = _ctx(repo_root=repo_root)
    return ctx.model_copy(
        update={"run_binding": ctx.run_binding.model_copy(update={"source_run_id": source_run_id})}
    )


SHA_A = "a" * 40
HASH_1 = "1" * 64
HASH_2 = "2" * 64

CORRUPTION_CASES = (
    ("execution_context", "wrong_hash"),
    ("execution_context", "missing"),
    ("execution_context", "malformed_json"),
    ("source_plan", "wrong_hash"),
    ("source_plan", "missing"),
    ("source_prompt", "wrong_hash"),
    ("patch", "truncated"),
    ("patch", "oversized"),
    ("patch", "missing"),
    ("fix_prompt", "empty"),
    ("observation", "wrong_hash"),
    ("observation", "wrong_binding"),
    ("adjudication", "wrong_thread_set"),
    ("adjudication", "wrong_cycle"),
    ("publication_text", "wrong_head"),
    ("publication_text", "malformed_json"),
    ("local_result", "wrong_schema"),
    ("commit_message", "missing"),
    ("reply", "truncated"),
    ("review_evidence", "absolute_path"),
    ("review_evidence", "traversal_path"),
)


@pytest.fixture(autouse=True)
def _native_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("TMP", "/tmp")
    monkeypatch.setenv("TEMP", "/tmp")


def _prepare(tmp_path: Path):
    from tests.integration.phase16_4_matrix_helpers import make_engine
    from tests.unit.pr_review_v2.durable_helpers import FakeClock

    clock = FakeClock()
    engine = make_engine(tmp_path / "engine.sqlite3", clock, prefix="p168-corrupt")
    store = ProtectedResultStore(tmp_path / "artifacts")
    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = _ctx_for(repo_root=str(repo.resolve()))
    patch_bytes = b"SENTINEL_PATCH_FOR_CORRUPTION\n"
    prep = PreparationService(engine, store, clock=clock)
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-corrupt",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=patch_bytes,
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=ctx,
        )
    )
    with engine.store.begin_read() as conn:
        prepared, _, _ = engine.store.load_validated_snapshot(conn, created.run_id)
    return store, prepared, patch_bytes, created.run_id


def _observation_snapshot() -> ObservationSnapshot:
    return ObservationSnapshot(
        binding=binding(),
        cycle_number=1,
        poll_sequence=1,
        trigger_marker=MARKER,
        observed_at=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
        evidence_kind=ObservationEvidenceKind.BOT_STILL_WAITING,
        trigger=ObservedTriggerComment(
            comment_id="101",
            author_login="orchestrator",
            created_at=datetime(2026, 7, 21, 11, 0, tzinfo=UTC),
            body_sha256=sha256_text("trigger"),
        ),
    )


def _publication_artifact(
    *, run_id: str = "run-corrupt", cycle: int = 1, head: str = SHA_A
) -> PublicationGenerationResultArtifact:
    return PublicationGenerationResultArtifact(
        title="Title",
        body="body",
        commit_subject="subject",
        commit_body="",
        run_id=run_id,
        cycle_number=cycle,
        effect_id="effect-pub",
        bound_head_sha=head,
        evidence_ref_sha256=HASH_1,
        patch_ref_sha256=HASH_2,
    )


def _adjudication_artifact(
    *,
    run_id: str = "run-corrupt",
    cycle: int = 1,
    threads: tuple[str, ...] = ("PRRT_1",),
) -> ExternalAdjudicationResultArtifact:
    return ExternalAdjudicationResultArtifact(
        decisions=tuple(
            ExternalAdjudicationDecision(
                thread_id=thread_id,
                decision=AdjudicationDecisionKind.NOT_APPLICABLE,
                safe_summary="n/a",
                reply_body="ack",
            )
            for thread_id in threads
        ),
        fix_prompt_text=None,
        run_id=run_id,
        cycle_number=cycle,
        effect_id="effect-adj",
        bound_head_sha=SHA_A,
        frozen_thread_ids=threads,
        snapshot_ref_sha256=HASH_1,
        execution_context_ref_sha256=HASH_2,
    )


@pytest.mark.parametrize(("artifact", "kind"), CORRUPTION_CASES)
def test_protected_artifact_corruption_fails_closed(
    tmp_path: Path,
    artifact: str,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    store, prepared, _patch_bytes, run_id = _prepare(tmp_path)
    run_root = run_artifact_root(store.root, run_id)

    if artifact == "execution_context" and kind == "wrong_hash":
        path = run_root / prepared.origin.execution_context_ref.relative_path
        path.write_bytes(path.read_bytes() + b"tamper")
        with pytest.raises(ProtectedResultStoreError, match="hash mismatch"):
            store.read_execution_context(
                run_id=run_id,
                ref=prepared.origin.execution_context_ref,
            )
        return

    if artifact == "execution_context" and kind == "missing":
        path = run_root / prepared.origin.execution_context_ref.relative_path
        path.unlink()
        with pytest.raises(ProtectedResultStoreError, match="missing or unsafe"):
            store.read_execution_context(
                run_id=run_id,
                ref=prepared.origin.execution_context_ref,
            )
        return

    if artifact == "execution_context" and kind == "malformed_json":
        path = run_root / prepared.origin.execution_context_ref.relative_path
        bad = b"{not-json"
        path.write_bytes(bad)
        os.chmod(path, 0o600)
        bad_ref = ArtifactRef(
            relative_path=prepared.origin.execution_context_ref.relative_path,
            sha256=hashlib.sha256(bad).hexdigest(),
        )
        with pytest.raises(ProtectedResultStoreError, match="schema validation"):
            store.read_execution_context(run_id=run_id, ref=bad_ref)
        return

    if artifact == "source_plan" and kind == "wrong_hash":
        plan_path = run_root / "local/source/plan.md"
        plan_path.write_bytes(b"tampered-plan\n")
        with pytest.raises(ProtectedResultStoreError, match="hash mismatch"):
            store.read_source_plan_bytes(
                run_id=run_id,
                expected_sha256=sha256_bytes(PLAN_BYTES),
            )
        return

    if artifact == "source_plan" and kind == "missing":
        plan_path = run_root / "local/source/plan.md"
        plan_path.unlink()
        with pytest.raises(ProtectedResultStoreError, match="missing"):
            store.read_source_plan_bytes(
                run_id=run_id,
                expected_sha256=sha256_bytes(PLAN_BYTES),
            )
        return

    if artifact == "source_prompt" and kind == "wrong_hash":
        prompt_path = run_root / "local/source/prompt.txt"
        prompt_path.write_bytes(b"tampered-prompt\n")
        with pytest.raises(ProtectedResultStoreError, match="hash mismatch"):
            store.read_source_prompt_bytes(
                run_id=run_id,
                expected_sha256=sha256_bytes(PROMPT_BYTES),
            )
        return

    if artifact == "patch" and kind == "truncated":
        patch_path = run_root / prepared.origin.accepted_patch.relative_path
        patch_path.write_bytes(b"x")
        digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        assert digest != prepared.origin.accepted_patch.sha256
        with pytest.raises(ProtectedResultStoreError, match="hash mismatch|missing or unsafe"):
            store.copy_verified_artifact(
                source_run_root=run_root,
                source_ref=prepared.origin.accepted_patch,
                dest_run_id=f"{run_id}-copy",
                dest_relative="artifacts/accepted.patch",
            )
        return

    if artifact == "patch" and kind == "oversized":
        with pytest.raises(ProtectedResultStoreError, match="size bound"):
            store.persist_patch_bytes(
                run_id=run_id,
                data=b"x" * (DEFAULT_MAX_PATCH_BYTES + 1),
            )
        return

    if artifact == "patch" and kind == "missing":
        patch_path = run_root / prepared.origin.accepted_patch.relative_path
        patch_path.unlink()
        with pytest.raises(ProtectedResultStoreError, match="missing or unsafe"):
            store.copy_verified_artifact(
                source_run_root=run_root,
                source_ref=prepared.origin.accepted_patch,
                dest_run_id=f"{run_id}-copy",
                dest_relative="artifacts/accepted.patch",
            )
        return

    if artifact == "fix_prompt" and kind == "empty":
        with pytest.raises(ProtectedResultStoreError, match="must not be empty"):
            store.persist_fix_prompt(run_id=run_id, text="   ")
        return

    if artifact == "observation" and kind == "wrong_hash":
        review = ReviewArtifactStore(store.root)
        ref = review.persist_observation_for_run(run_id=run_id, snapshot=_observation_snapshot())
        path = run_root / ref.relative_path
        path.write_bytes(path.read_bytes() + b"x")
        with pytest.raises(ArtifactStoreError):
            review.read_and_verify(run_id=run_id, ref=ref, expected_binding=binding())
        return

    if artifact == "observation" and kind == "wrong_binding":
        review = ReviewArtifactStore(store.root)
        ref = review.persist_observation_for_run(run_id=run_id, snapshot=_observation_snapshot())
        other = binding().model_copy(update={"pr_number": 999})
        with pytest.raises(ArtifactStoreError):
            review.read_and_verify(run_id=run_id, ref=ref, expected_binding=other)
        return

    if artifact == "adjudication" and kind == "wrong_thread_set":
        good = _adjudication_artifact(run_id=run_id, cycle=1)
        ref = store.persist_external_adjudication(run_id=run_id, artifact=good)
        path = run_root / ref.relative_path
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["frozen_thread_ids"] = ["PRRT_OTHER"]
        mutated = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
        path.write_bytes(mutated)
        os.chmod(path, 0o600)
        effect = type(
            "E",
            (),
            {
                "run_id": run_id,
                "effect_id": good.effect_id,
                "cycle_number": good.cycle_number,
                "bound_head_sha": good.bound_head_sha,
                "snapshot_ref": ArtifactRef(relative_path="snap.json", sha256=HASH_1),
                "execution_context_ref": ArtifactRef(relative_path="ctx.json", sha256=HASH_2),
                "frozen_thread_ids": good.frozen_thread_ids,
            },
        )()
        assert store.read_cached_external_adjudication(effect) is None
        return

    if artifact == "adjudication" and kind == "wrong_cycle":
        good = _adjudication_artifact(run_id=run_id, cycle=1)
        ref = store.persist_external_adjudication(run_id=run_id, artifact=good)
        path = run_root / ref.relative_path
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["cycle_number"] = 99
        mutated = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
        path.write_bytes(mutated)
        os.chmod(path, 0o600)
        effect = type(
            "E",
            (),
            {
                "run_id": run_id,
                "effect_id": good.effect_id,
                "cycle_number": 1,
                "bound_head_sha": good.bound_head_sha,
                "snapshot_ref": ArtifactRef(relative_path="snap.json", sha256=HASH_1),
                "execution_context_ref": ArtifactRef(relative_path="ctx.json", sha256=HASH_2),
                "frozen_thread_ids": good.frozen_thread_ids,
            },
        )()
        assert store.read_cached_external_adjudication(effect) is None
        return

    if artifact == "publication_text" and kind == "wrong_head":
        good = _publication_artifact(run_id=run_id, head=SHA_A)
        ref = store.persist_publication_generation(run_id=run_id, artifact=good)
        path = run_root / ref.relative_path
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["bound_head_sha"] = "b" * 40
        mutated = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
        path.write_bytes(mutated)
        os.chmod(path, 0o600)
        effect = type(
            "E",
            (),
            {
                "run_id": run_id,
                "effect_id": good.effect_id,
                "cycle_number": good.cycle_number,
                "bound_head_sha": SHA_A,
                "evidence_ref": ArtifactRef(relative_path="ev.json", sha256=HASH_1),
                "patch_ref": ArtifactRef(relative_path="p.patch", sha256=HASH_2),
            },
        )()
        assert store.read_cached_publication_generation(effect) is None
        return

    if artifact == "publication_text" and kind == "malformed_json":
        good = _publication_artifact(run_id=run_id)
        ref = store.persist_publication_generation(run_id=run_id, artifact=good)
        path = run_root / ref.relative_path
        bad = b"{bad"
        path.write_bytes(bad)
        os.chmod(path, 0o600)
        bad_ref = ArtifactRef(
            relative_path=ref.relative_path, sha256=hashlib.sha256(bad).hexdigest()
        )
        with pytest.raises(ProtectedResultStoreError, match="schema validation"):
            store.read_publication_generation(run_id=run_id, ref=bad_ref)
        return

    if artifact == "local_result" and kind == "wrong_schema":
        path_dir = run_root / "local/results/local-fix"
        path_dir.mkdir(parents=True, exist_ok=True)
        bad_path = path_dir / f"{HASH_1}.json"
        bad_path.write_text(
            json.dumps({"schema_name": "wrong", "schema_version": 1}), encoding="utf-8"
        )
        os.chmod(bad_path, 0o600)
        with pytest.raises(ProtectedResultStoreError, match="schema validation|hash mismatch"):
            store.read_local_fix_result(
                run_id=run_id,
                ref=ArtifactRef(
                    relative_path=f"local/results/local-fix/{HASH_1}.json",
                    sha256=hashlib.sha256(bad_path.read_bytes()).hexdigest(),
                ),
            )
        return

    if artifact == "commit_message" and kind == "missing":
        pub_ref, commit_ref = store.persist_publication_text_and_commit_message(
            run_id=run_id,
            title="title",
            body="body",
            subject="subject",
            commit_body="",
            effect_id="effect-pub",
            cycle_number=1,
            bound_head_sha=SHA_A,
            evidence_ref_sha256=HASH_1,
            patch_ref_sha256=HASH_2,
        )
        commit_path = run_root / commit_ref.relative_path
        commit_path.unlink()
        with pytest.raises(Exception, match="missing|unsafe|not found|hash"):
            store.verify_publication_readable(
                run_id=run_id,
                publication_ref=pub_ref,
                commit_ref=commit_ref,
            )
        return

    if artifact == "reply" and kind == "truncated":
        from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import (
            InputArtifactError,
            InputArtifactReader,
        )

        ref = store.persist_reply_text(
            run_id=run_id, relative_hint="reply01.txt", text="hello reply"
        )
        path = run_root / ref.relative_path
        path.write_bytes(b"x")
        os.chmod(path, 0o600)
        with pytest.raises(InputArtifactError) as exc:
            InputArtifactReader(store.root).read_reply_text(run_id=run_id, ref=ref)
        assert exc.value.kind.value == "hash_mismatch"
        return

    if artifact == "review_evidence" and kind == "absolute_path":
        with pytest.raises((ProtectedResultStoreError, ValueError, ArtifactStoreError)):
            ReviewArtifactStore(store.root).read_and_verify(
                run_id=run_id,
                ref=ArtifactRef(relative_path="/tmp/escape.json", sha256=HASH_1),
                expected_binding=binding(),
            )
        return

    if artifact == "review_evidence" and kind == "traversal_path":
        with pytest.raises((ProtectedResultStoreError, ValueError, ArtifactStoreError)):
            ReviewArtifactStore(store.root).read_and_verify(
                run_id=run_id,
                ref=ArtifactRef(relative_path="../escape.json", sha256=HASH_1),
                expected_binding=binding(),
            )
        return

    pytest.fail(f"unhandled corruption case {artifact}/{kind}")


def test_symlinked_artifact_path_is_rejected(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "artifacts")
    ctx = _ctx_for(repo_root=str(tmp_path / "repo"))
    (tmp_path / "repo").mkdir()
    ref = store.persist_execution_context(run_id="run-symlink", artifact=ctx)
    path = run_artifact_root(store.root, "run-symlink") / ref.relative_path
    payload = path.read_bytes()
    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(payload)
    path.symlink_to(outside)
    with pytest.raises(ProtectedResultStoreError, match="missing|unsafe"):
        store.read_execution_context(run_id="run-symlink", ref=ref)


def test_symlinked_parent_directory_is_rejected(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "artifacts")
    ctx = _ctx_for(repo_root=str(tmp_path / "repo"))
    (tmp_path / "repo").mkdir()
    ref = store.persist_execution_context(run_id="run-parent-link", artifact=ctx)
    run_root = run_artifact_root(store.root, "run-parent-link")
    path = run_root / ref.relative_path
    payload = path.read_bytes()
    parent = path.parent
    outside_parent = tmp_path / "outside-parent"
    outside_parent.mkdir()
    outside_file = outside_parent / path.name
    outside_file.write_bytes(payload)
    # Replace the immediate parent with a symlink escape.
    for child in parent.iterdir():
        child.unlink()
    parent.rmdir()
    parent.symlink_to(outside_parent)
    with pytest.raises(ProtectedResultStoreError, match="missing|unsafe"):
        store.read_execution_context(run_id="run-parent-link", ref=ref)


def test_non_regular_and_world_readable_files_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    store, prepared, _, run_id = _prepare(tmp_path)
    from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import (
        InputArtifactError,
        InputArtifactReader,
    )

    reader = InputArtifactReader(store.root)
    ref = store.persist_fix_prompt(run_id=run_id, text="fix body\n")
    path = run_artifact_root(store.root, run_id) / ref.relative_path
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR
    path.chmod(0o644)
    with pytest.raises((ProtectedResultStoreError, InputArtifactError), match="unsafe"):
        reader.read_reply_text(run_id=run_id, ref=ref)
    # Fix prompts are text artifacts; also cover store-bound source snapshots.
    plan_path = run_artifact_root(store.root, run_id) / "local/source/plan.md"
    plan_path.chmod(0o644)
    with pytest.raises(ProtectedResultStoreError, match="unsafe"):
        store.read_source_plan_bytes(
            run_id=run_id,
            expected_sha256=sha256_bytes(PLAN_BYTES),
        )
    prompt_path = run_artifact_root(store.root, run_id) / "local/source/prompt.txt"
    prompt_path.chmod(0o644)
    with pytest.raises(ProtectedResultStoreError, match="unsafe"):
        store.read_source_prompt_bytes(
            run_id=run_id,
            expected_sha256=sha256_bytes(PROMPT_BYTES),
        )
    ctx_path = (
        run_artifact_root(store.root, run_id) / prepared.origin.execution_context_ref.relative_path
    )
    ctx_path.chmod(0o644)
    with pytest.raises(ProtectedResultStoreError, match="unsafe"):
        store.read_execution_context(
            run_id=run_id,
            ref=prepared.origin.execution_context_ref,
        )
    patch_path = (
        run_artifact_root(store.root, run_id) / prepared.origin.accepted_patch.relative_path
    )
    patch_path.chmod(0o644)
    with pytest.raises((ProtectedResultStoreError, InputArtifactError), match="unsafe"):
        reader.read_patch_bytes(run_id=run_id, ref=prepared.origin.accepted_patch)
    pub_ref, commit_ref = store.persist_publication_text_and_commit_message(
        run_id=run_id,
        title="t",
        body="b",
        subject="s",
        commit_body="c",
        effect_id="effect-perm",
        cycle_number=1,
        bound_head_sha=SHA_A,
        evidence_ref_sha256=HASH_1,
        patch_ref_sha256=HASH_2,
    )
    commit_path = run_artifact_root(store.root, run_id) / commit_ref.relative_path
    commit_path.chmod(0o644)
    with pytest.raises((ProtectedResultStoreError, InputArtifactError), match="unsafe"):
        reader.read_commit_message(run_id=run_id, ref=commit_ref)
    reply_ref = store.persist_reply_text(run_id=run_id, relative_hint="r.txt", text="reply\n")
    reply_path = run_artifact_root(store.root, run_id) / reply_ref.relative_path
    reply_path.chmod(0o644)
    with pytest.raises((ProtectedResultStoreError, InputArtifactError), match="unsafe"):
        reader.read_reply_text(run_id=run_id, ref=reply_ref)
    # Non-regular paths must fail closed without blocking (directory standing in for the artifact).
    plan_path.chmod(0o600)
    plan_path.unlink()
    plan_path.mkdir()
    with pytest.raises(ProtectedResultStoreError, match="missing|unsafe|not a regular"):
        store.read_source_plan_bytes(
            run_id=run_id,
            expected_sha256=sha256_bytes(PLAN_BYTES),
        )
    # Restore a regular plan for FIFO coverage with O_NONBLOCK open.
    plan_path.rmdir()
    plan_path.write_bytes(PLAN_BYTES)
    os.chmod(plan_path, 0o600)
    plan_path.unlink()
    os.mkfifo(plan_path)
    with pytest.raises(ProtectedResultStoreError, match="missing|unsafe|not a regular"):
        store.read_source_plan_bytes(
            run_id=run_id,
            expected_sha256=sha256_bytes(PLAN_BYTES),
        )


def test_post_finalization_byte_mutation_fails_hash_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    store, prepared, _, run_id = _prepare(tmp_path)
    plan_path = run_artifact_root(store.root, run_id) / "local/source/plan.md"
    plan_path.write_bytes(plan_path.read_bytes() + b"mutated")
    with pytest.raises(ProtectedResultStoreError, match="hash mismatch"):
        store.read_source_plan_bytes(
            run_id=run_id,
            expected_sha256=sha256_bytes(PLAN_BYTES),
        )


def test_post_finalization_binding_valid_cache_mutation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    store, _prepared, _, run_id = _prepare(tmp_path)
    good = _publication_artifact(run_id=run_id, head=SHA_A)
    ref = store.persist_publication_generation(run_id=run_id, artifact=good)
    effect = type(
        "E",
        (),
        {
            "run_id": run_id,
            "effect_id": good.effect_id,
            "cycle_number": good.cycle_number,
            "bound_head_sha": SHA_A,
            "evidence_ref": ArtifactRef(relative_path="ev.json", sha256=HASH_1),
            "patch_ref": ArtifactRef(relative_path="p.patch", sha256=HASH_2),
        },
    )()
    assert store.read_cached_publication_generation(effect) is not None
    path = run_artifact_root(store.root, run_id) / ref.relative_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["body"] = "binding-valid mutated body"
    mutated = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    path.write_bytes(mutated)
    os.chmod(path, 0o600)
    assert store.read_cached_publication_generation(effect) is None


def test_newly_persisted_sensitive_artifacts_are_owner_only(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "artifacts")
    ref = store.persist_fix_prompt(run_id="run-perms", text="fix body\n")
    path = run_artifact_root(store.root, "run-perms") / ref.relative_path
    if hasattr(os, "stat"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == stat.S_IRUSR | stat.S_IWUSR

    local = LocalFixResultArtifact(
        outcome=LocalFixOutcomeKind.ACCEPTED,
        accepted_patch_sha256=HASH_1,
        new_head_sha=SHA_A,
        carrier_run_id="prv2c-abc",
        cursor_chat_id="chat-1",
        codex_session_id="11111111-1111-1111-1111-111111111111",
        iteration_count=1,
        result_message_safe="ok",
        needs_external_continuation=True,
        run_id="run-perms",
        cycle_number=1,
        effect_id="effect-local",
        bound_head_sha=SHA_A,
        fix_prompt_ref_sha256=HASH_1,
        execution_context_ref_sha256=HASH_2,
    )
    local_ref = store.persist_local_fix_result(run_id="run-perms", artifact=local)
    local_path = run_artifact_root(store.root, "run-perms") / local_ref.relative_path
    assert stat.S_IMODE(local_path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
