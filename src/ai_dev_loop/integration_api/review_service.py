"""Read-only integration Codex review inspection service."""

# mypy: disable-error-code=call-arg

from __future__ import annotations

import sqlite3
from pathlib import Path

from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.models import (
    IntegrationReviewContentChunk,
    IntegrationReviewDetailData,
    IntegrationReviewListData,
)
from ai_dev_loop.integration_api.review_evidence_auth import (
    AuthenticatedReviewEvidence,
    ReviewEvidenceIntegrityError,
    load_authenticated_review_evidence,
)
from ai_dev_loop.integration_api.review_projection import (
    build_review_detail_data,
    build_review_list_item,
    confined_run_root,
)
from ai_dev_loop.integration_api.review_reader import (
    ReviewContentKind,
    is_codex_review_effect,
    read_review_content_chunk,
    review_content_chunk_to_wire,
)
from ai_dev_loop.integration_api.run_projection import collection_page
from ai_dev_loop.integration_api.run_service import _map_engine_error
from ai_dev_loop.integration_api.validation import (
    COLLECTION_DEFAULT_LIMIT,
    validate_collection_bounds,
)
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root, default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


class IntegrationReviewReadService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        *,
        artifact_root: Path | None = None,
    ) -> None:
        self.store = store
        self.artifact_root = artifact_root or default_artifact_root()

    def _require_scheduler_database(self) -> None:
        if not self.store.db_path.exists():
            raise IntegrationApiError.not_found("Scheduler database not found.")

    def _resolve_owned_review_attempt(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        attempt_id: str,
    ) -> tuple[sqlite3.Row, str]:
        self.store.load_validated_snapshot(conn, run_id)
        attempt = self.store.get_attempt_by_id(conn, attempt_id)
        if attempt is None:
            raise IntegrationApiError.not_found("Review attempt not found.")
        if str(attempt["run_id"]) != run_id:
            raise IntegrationApiError.not_found("Review attempt not found.")
        dispatch = self.store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
        effect_kind = str(dispatch["effect_kind"]) if dispatch is not None else ""
        if str(attempt["component"]) != "codex" or not is_codex_review_effect(effect_kind):
            raise IntegrationApiError.invalid_argument("Attempt is not a Codex review.")
        row = self.store.get_integration_codex_review_row(conn, run_id, attempt_id)
        if row is None:
            raise IntegrationApiError.not_found("Review attempt not found.")
        return row, effect_kind

    def _load_auth(
        self,
        run_id: str,
        attempt_row: sqlite3.Row,
        effect_kind: str,
    ) -> AuthenticatedReviewEvidence:
        run_root = confined_run_root(self.artifact_root, run_id)
        return load_authenticated_review_evidence(
            run_root,
            attempt_row,
            effect_kind=effect_kind,
        )

    def list_reviews(
        self,
        run_id: str,
        *,
        offset: int = 0,
        limit: int = COLLECTION_DEFAULT_LIMIT,
    ) -> IntegrationReviewListData:
        self._require_scheduler_database()
        validate_collection_bounds(offset, limit)
        try:
            with self.store.begin_read() as conn:
                self.store.load_validated_snapshot(conn, run_id)
                rows, has_more = self.store.list_integration_codex_review_rows(
                    conn,
                    run_id,
                    offset=offset,
                    limit=limit,
                )
                items = []
                for row in rows:
                    effect_kind = str(row["effect_kind"] or "")
                    try:
                        auth = self._load_auth(run_id, row, effect_kind)
                    except ReviewEvidenceIntegrityError as exc:
                        raise IntegrationApiError.data_integrity(
                            "Review attempt evidence failed verification."
                        ) from exc
                    items.append(
                        build_review_list_item(
                            run_id=run_id,
                            attempt_row=row,
                            effect_kind=effect_kind,
                            auth=auth,
                        )
                    )
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc
        return IntegrationReviewListData(
            run_id=run_id,
            items=tuple(items),
            page=collection_page(offset, limit, has_more),
        )

    def inspect_review(self, run_id: str, *, attempt_id: str) -> IntegrationReviewDetailData:
        self._require_scheduler_database()
        try:
            with self.store.begin_read() as conn:
                row, effect_kind = self._resolve_owned_review_attempt(conn, run_id, attempt_id)
                try:
                    auth = self._load_auth(run_id, row, effect_kind)
                except ReviewEvidenceIntegrityError as exc:
                    raise IntegrationApiError.data_integrity(
                        "Review attempt evidence failed verification."
                    ) from exc
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc
        return build_review_detail_data(
            run_id=run_id,
            attempt_row=row,
            effect_kind=effect_kind,
            auth=auth,
        )

    def read_review_content(
        self,
        run_id: str,
        *,
        attempt_id: str,
        content_kind: ReviewContentKind,
        byte_offset: int = 0,
        limit: int = 65536,
    ) -> IntegrationReviewContentChunk:
        self._require_scheduler_database()
        try:
            with self.store.begin_read() as conn:
                row, effect_kind = self._resolve_owned_review_attempt(conn, run_id, attempt_id)
                try:
                    auth = self._load_auth(run_id, row, effect_kind)
                except ReviewEvidenceIntegrityError as exc:
                    raise IntegrationApiError.data_integrity(
                        "Review attempt evidence failed verification."
                    ) from exc
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc
        chunk = read_review_content_chunk(
            artifact_root=self.artifact_root,
            run_id=run_id,
            attempt_row=row,
            effect_kind=effect_kind,
            auth=auth,
            content_kind=content_kind,
            byte_offset=byte_offset,
            limit=limit,
        )
        wire = review_content_chunk_to_wire(chunk)
        return IntegrationReviewContentChunk.model_validate(wire)


def default_review_read_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> IntegrationReviewReadService:
    path = db_path or default_engine_db_path()
    if not path.exists():
        store = SqliteSchedulerStore.__new__(SqliteSchedulerStore)
        store.db_path = path
        store.busy_timeout_ms = 5000
        store._migration_fault_hook = None
        store._read_only = True
        from ai_dev_loop.scheduler.infrastructure.paths import apply_database_permissions

        store._apply_database_permissions = apply_database_permissions
        return IntegrationReviewReadService(store, artifact_root=artifact_root)
    store = SqliteSchedulerStore.open_readonly(path)
    return IntegrationReviewReadService(store, artifact_root=artifact_root)
