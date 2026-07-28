"""Phase 16.8: existing-PR prepare-time preimage adoption for first update_pr_text."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.github_write_helpers import (
    RUN_ID,
    SHA_A,
    SHA_B,
    FakeGhWriteTransport,
    FakeReadTransport,
    binding,
    pr_dict,
    rest_result,
    update_pr_text_effect,
    write_adopted_existing_pr_preimage,
    write_publication_text,
)

from ai_dev_loop.pr_review_v2.application.write_contracts import (
    GitHubWritePolicy,
    WriteProofKind,
    append_owned_marker,
    canonicalize_publication_text,
    derive_content_bound_marker,
)
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef, ExistingPrOrigin
from ai_dev_loop.pr_review_v2.domain.effects import UpdatePrTextEffect
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.github_write_gateway import GitHubWriteGateway
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    resolve_run_relative_path,
    run_artifact_root,
)
from ai_dev_loop.pr_review_v2.infrastructure.write_evidence_artifacts import WriteEvidenceStore

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


class _Auth:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _gateway(tmp_path: Path, writes: FakeGhWriteTransport) -> GitHubWriteGateway:
    art_root = tmp_path / "art"
    policy = GitHubWritePolicy(repository_cwd=str(tmp_path / "repo"))
    (tmp_path / "repo").mkdir(exist_ok=True)
    return GitHubWriteGateway(
        policy=policy,
        write_transport=writes,
        read_transport=FakeReadTransport(),
        input_reader=InputArtifactReader(art_root),
        write_evidence=WriteEvidenceStore(art_root),
    )


def _pr_marker(effect: UpdatePrTextEffect, *, title: str, body: str):
    return derive_content_bound_marker(
        operation="update_pr_text",
        target_kind="pr_body",
        idempotency_key=effect.idempotency_key,
        canonical_content=canonicalize_publication_text(title=title, body=body),
    )


def _adoption_setup(
    tmp_path: Path,
    *,
    live_title: str,
    live_body: str,
    prep_title: str | None = None,
    prep_body: str | None = None,
):
    del live_title, live_body  # live values are supplied by each test's FakeGh responses
    art = tmp_path / "art"
    pub = write_publication_text(art, RUN_ID, title="New title", body="new body")
    adopted = write_adopted_existing_pr_preimage(
        art,
        RUN_ID,
        title=prep_title if prep_title is not None else "Existing feature",
        body=prep_body if prep_body is not None else "Adopted PR body",
        head_sha=SHA_A,
    )
    effect = update_pr_text_effect(publication_text_ref=pub, adopted_preimage_ref=adopted)
    marker = _pr_marker(effect, title="New title", body="new body")
    intended_body = append_owned_marker("new body", marker.marker_text)
    return art, pub, adopted, effect, marker, intended_body


def test_update_pr_text_adopts_exact_unmarked_preimage(tmp_path: Path) -> None:
    _art, _pub, _adopted, effect, marker, intended_body = _adoption_setup(
        tmp_path, live_title="Existing feature", live_body="Adopted PR body"
    )
    writes = FakeGhWriteTransport(
        responses={
            "fetch_pr_text": rest_result(
                pr_dict(title="Existing feature", body="Adopted PR body", head_sha=SHA_B)
            ),
            "update_pr_text": rest_result(
                pr_dict(title="New title", body=intended_body, head_sha=SHA_B)
            ),
        }
    )
    gw = _gateway(tmp_path, writes)
    auth = _Auth()
    result = gw.update_pr_text(effect, run_id=RUN_ID, now=NOW, authorize=auth)
    assert result.already_applied is False
    assert auth.calls == 1
    assert writes.calls[0][0] == "fetch_pr_text"
    assert writes.calls[1][0] == "update_pr_text"
    assert marker.marker_text in writes.calls[1][1]["body"]


def test_update_pr_text_adoption_title_drift_blocks(tmp_path: Path) -> None:
    _art, _pub, _adopted, effect, _marker, _intended = _adoption_setup(
        tmp_path,
        live_title="Changed title",
        live_body="Adopted PR body",
        prep_title="Existing feature",
        prep_body="Adopted PR body",
    )
    writes = FakeGhWriteTransport(
        responses={
            "fetch_pr_text": rest_result(
                pr_dict(title="Changed title", body="Adopted PR body", head_sha=SHA_B)
            )
        }
    )
    with pytest.raises(GhTransportError, match="owned preimage"):
        _gateway(tmp_path, writes).update_pr_text(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())
    assert len(writes.calls) == 1
    assert writes.calls[0][0] == "fetch_pr_text"


def test_update_pr_text_adoption_body_whitespace_drift_blocks(tmp_path: Path) -> None:
    _art, _pub, _adopted, effect, _marker, _intended = _adoption_setup(
        tmp_path,
        live_title="Existing feature",
        live_body="Adopted PR body ",
        prep_title="Existing feature",
        prep_body="Adopted PR body",
    )
    writes = FakeGhWriteTransport(
        responses={
            "fetch_pr_text": rest_result(
                pr_dict(title="Existing feature", body="Adopted PR body ", head_sha=SHA_B)
            )
        }
    )
    with pytest.raises(GhTransportError, match="owned preimage"):
        _gateway(tmp_path, writes).update_pr_text(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())


def test_update_pr_text_without_adoption_ref_still_requires_marker(tmp_path: Path) -> None:
    art = tmp_path / "art"
    pub = write_publication_text(art, RUN_ID, title="T", body="text body")
    effect = update_pr_text_effect(publication_text_ref=pub)
    writes = FakeGhWriteTransport(
        responses={
            "fetch_pr_text": rest_result(
                pr_dict(title="Existing feature", body="Adopted PR body", head_sha=SHA_B)
            )
        }
    )
    with pytest.raises(GhTransportError, match="owned preimage"):
        _gateway(tmp_path, writes).update_pr_text(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())


def test_reconcile_adoption_exact_is_proven_not_applied(tmp_path: Path) -> None:
    _art, _pub, _adopted, effect, _marker, _intended = _adoption_setup(
        tmp_path, live_title="Existing feature", live_body="Adopted PR body"
    )
    writes = FakeGhWriteTransport(
        responses={
            "fetch_pr_text": rest_result(
                pr_dict(title="Existing feature", body="Adopted PR body", head_sha=SHA_B)
            )
        }
    )
    proof = _gateway(tmp_path, writes).reconcile_update_pr_text(effect, run_id=RUN_ID, now=NOW)
    assert proof.proof is WriteProofKind.PROVEN_NOT_APPLIED


def test_reconcile_adoption_applied_after_write(tmp_path: Path) -> None:
    _art, _pub, _adopted, effect, marker, intended_body = _adoption_setup(
        tmp_path, live_title="Existing feature", live_body="Adopted PR body"
    )
    writes = FakeGhWriteTransport(
        responses={
            "fetch_pr_text": rest_result(
                pr_dict(title="New title", body=intended_body, head_sha=SHA_B)
            )
        }
    )
    proof = _gateway(tmp_path, writes).reconcile_update_pr_text(effect, run_id=RUN_ID, now=NOW)
    assert proof.proof is WriteProofKind.APPLIED
    assert marker.marker_text in intended_body


def test_reconcile_adoption_drift_is_unresolved(tmp_path: Path) -> None:
    _art, _pub, _adopted, effect, _marker, _intended = _adoption_setup(
        tmp_path,
        live_title="Existing feature",
        live_body="human edited",
        prep_title="Existing feature",
        prep_body="Adopted PR body",
    )
    writes = FakeGhWriteTransport(
        responses={
            "fetch_pr_text": rest_result(
                pr_dict(title="Existing feature", body="human edited", head_sha=SHA_B)
            )
        }
    )
    proof = _gateway(tmp_path, writes).reconcile_update_pr_text(effect, run_id=RUN_ID, now=NOW)
    assert proof.proof is WriteProofKind.UNRESOLVED


def test_adopted_preimage_hash_tamper_fails_closed(tmp_path: Path) -> None:
    art, _pub, adopted, effect, _marker, _intended = _adoption_setup(
        tmp_path, live_title="Existing feature", live_body="Adopted PR body"
    )
    run_root = run_artifact_root(art, RUN_ID)
    target = resolve_run_relative_path(run_root, adopted.relative_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["body"] = "tampered"
    target.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    writes = FakeGhWriteTransport(
        responses={
            "fetch_pr_text": rest_result(
                pr_dict(title="Existing feature", body="Adopted PR body", head_sha=SHA_B)
            )
        }
    )
    with pytest.raises(GhTransportError, match="preimage"):
        _gateway(tmp_path, writes).update_pr_text(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())


def test_adopted_preimage_binding_mismatch_fails_closed(tmp_path: Path) -> None:
    art = tmp_path / "art"
    pub = write_publication_text(art, RUN_ID, title="New title", body="new body")
    adopted = write_adopted_existing_pr_preimage(
        art,
        RUN_ID,
        title="Existing feature",
        body="Adopted PR body",
        pr_number=99,
        head_sha=SHA_A,
    )
    effect = update_pr_text_effect(publication_text_ref=pub, adopted_preimage_ref=adopted)
    writes = FakeGhWriteTransport(
        responses={
            "fetch_pr_text": rest_result(
                pr_dict(title="Existing feature", body="Adopted PR body", head_sha=SHA_B)
            )
        }
    )
    with pytest.raises(GhTransportError, match="binding|preimage"):
        _gateway(tmp_path, writes).update_pr_text(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())


def test_legacy_origin_without_adopted_ref_loads() -> None:
    origin = ExistingPrOrigin(
        binding=binding(head_sha=SHA_A),
        execution_context_ref=ArtifactRef(
            relative_path="local/execution-context/x.json",
            sha256="a" * 64,
        ),
    )
    assert origin.adopted_preimage_ref is None
    effect = UpdatePrTextEffect(
        effect_id="pr-review:run-16-6:cycle:01:update_pr_text",
        idempotency_key="pr-review:run-16-6:cycle:01:update_pr_text",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=binding().repository,
        bound_head_sha=SHA_B,
        binding=binding(),
        publication_text_ref=ArtifactRef(
            relative_path="local/publication-text.json", sha256="b" * 64
        ),
    )
    assert effect.adopted_preimage_ref is None


def test_adoption_errors_do_not_leak_preimage_text(tmp_path: Path) -> None:
    secret_title = "SECRET_TITLE_SHOULD_NOT_LEAK"
    secret_body = "SECRET_BODY_SHOULD_NOT_LEAK"
    _art, _pub, _adopted, effect, _marker, _intended = _adoption_setup(
        tmp_path,
        live_title="other",
        live_body="other",
        prep_title=secret_title,
        prep_body=secret_body,
    )
    writes = FakeGhWriteTransport(
        responses={
            "fetch_pr_text": rest_result(pr_dict(title="other", body="other", head_sha=SHA_B))
        }
    )
    with pytest.raises(GhTransportError) as exc_info:
        _gateway(tmp_path, writes).update_pr_text(effect, run_id=RUN_ID, now=NOW, authorize=_Auth())
    message = str(exc_info.value)
    assert secret_title not in message
    assert secret_body not in message
    assert "adl-v2" not in message
