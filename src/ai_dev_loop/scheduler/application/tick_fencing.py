"""Shared tick lease and claim fencing helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ai_dev_loop.scheduler.domain.common import encode_utc_instant
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def parse_utc_instant(value: str) -> datetime:
    return datetime.fromisoformat(encode_utc_instant(value).replace("Z", "+00:00"))


def tick_lease_is_active(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    generation: int,
    now: datetime,
) -> bool:
    row = store.get_tick_lease_row(conn)
    if row["status"] != "active":
        return False
    if row["owner_id"] != owner_id or int(row["generation"]) != generation:
        return False
    if row["expires_at"] is None:
        return False
    expires_at = parse_utc_instant(str(row["expires_at"]))
    return expires_at > now


def admission_claim_matches(
    claim: sqlite3.Row,
    *,
    claim_id: str,
    owner_id: str,
    generation: int,
    expected_run_version: int,
) -> bool:
    if str(claim["claim_id"]) != claim_id:
        return False
    if str(claim["tick_owner_id"]) != owner_id:
        return False
    if int(claim["tick_lease_generation"]) != generation:
        return False
    return int(claim["expected_run_version"]) == expected_run_version
