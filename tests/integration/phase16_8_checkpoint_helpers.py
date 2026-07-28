"""Executable checkpoint helpers for Phase 16.8 production-boundary tests."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.integration.phase16_4_matrix_helpers import make_engine
from tests.integration.stateful_fake_gh import StatefulFakeGhController
from tests.unit.pr_review_v2 import write_helpers as H
from tests.unit.pr_review_v2.durable_helpers import FakeClock

from ai_dev_loop.pr_review_v2.application.contracts import EventDisposition
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    GitHubWritePolicy,
    WriteProofKind,
    append_owned_marker,
    canonicalize_publication_text,
    derive_content_bound_marker,
    html_comment_marker,
)
from ai_dev_loop.pr_review_v2.domain.common import build_opaque_trigger_marker
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import (
    GhApiTransport,
    build_minimal_gh_env,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_write_transport import GhWriteTransport
from ai_dev_loop.pr_review_v2.infrastructure.github_write_gateway import GitHubWriteGateway
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
from ai_dev_loop.pr_review_v2.infrastructure.write_evidence_artifacts import WriteEvidenceStore

MUTATING_GH_EFFECT_KINDS = (
    "create_or_update_pr",
    "request_bot_review",
    "post_thread_reply",
    "update_pr_text",
    "resolve_thread",
)

GH_FAILURE_KINDS = (
    "timeout",
    "dns",
    "http_429",
    "primary_rate_limit",
    "http_500",
    "malformed",
    "apply_then_hang",
)


@dataclass(frozen=True, slots=True)
class WriteGatewayBundle:
    gateway: GitHubWriteGateway
    controller: StatefulFakeGhController
    repo: Path
    art: Path


def reopen_engine(
    db_path: Path,
    clock: FakeClock,
    *,
    prefix: str,
) -> PrReviewEngine:
    return make_engine(db_path, clock, prefix=prefix)


def assert_open_claim(
    engine: PrReviewEngine,
    run_id: str,
    *,
    dispatch_id: str,
    effect_id: str,
    effect_kind: str,
) -> None:
    with engine.store.begin_read() as conn:
        row = conn.execute(
            """
            SELECT dispatch_id, effect_id, effect_kind, status, claim_id
            FROM pr_review_effects
            WHERE run_id=? AND dispatch_id=?
            """,
            (run_id, dispatch_id),
        ).fetchone()
    assert row is not None
    assert row["effect_id"] == effect_id
    assert row["effect_kind"] == effect_kind
    assert row["status"] == "claimed"
    assert row["claim_id"] is not None


def assert_effect_identity_preserved(
    before: tuple,
    after: tuple,
    *,
    dispatch_id: str,
) -> None:
    _run_before, _timers_before, effects_before = before
    _run_after, timers_after, effects_after = after
    assert _run_before[0] <= _run_after[0]
    before_row = next(row for row in effects_before if row[0] == dispatch_id)
    after_row = next(row for row in effects_after if row[0] == dispatch_id)
    assert before_row[0] == after_row[0]
    assert before_row[3] == after_row[3]


def assert_timer_pending(
    engine: PrReviewEngine,
    run_id: str,
    *,
    status: str = "pending",
) -> list[tuple[Any, ...]]:
    with engine.store.begin_read() as conn:
        rows = conn.execute(
            """
            SELECT timer_id, status, due_at, target_effect_id
            FROM pr_review_timers WHERE run_id=? ORDER BY timer_id
            """,
            (run_id,),
        ).fetchall()
    pending = [tuple(r) for r in rows if r["status"] == status]
    return pending


def assemble_write_gateway(
    tmp_path: Path,
    controller: StatefulFakeGhController,
    *,
    per_call_timeout_seconds: float = 2.0,
) -> WriteGatewayBundle:
    gh_path = controller.install(tmp_path / "bin")
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    art = tmp_path / "artifacts"
    env = build_minimal_gh_env()
    env["PATH"] = f"{gh_path.parent}:{env.get('PATH', '')}"
    write_transport = GhWriteTransport(
        command=str(gh_path),
        cwd=str(repo),
        per_call_timeout_seconds=per_call_timeout_seconds,
        env=env,
    )
    read_transport = GhApiTransport(
        command=str(gh_path),
        cwd=str(repo),
        per_call_timeout_seconds=per_call_timeout_seconds,
        env=env,
    )
    gateway = GitHubWriteGateway(
        policy=GitHubWritePolicy(repository_cwd=str(repo)),
        write_transport=write_transport,
        read_transport=read_transport,
        input_reader=InputArtifactReader(art),
        write_evidence=WriteEvidenceStore(art),
    )
    return WriteGatewayBundle(gateway=gateway, controller=controller, repo=repo, art=art)


def reconcile_request_review_applied(bundle: WriteGatewayBundle) -> None:
    marker = build_opaque_trigger_marker(run_id=H.RUN_ID, cycle_number=1)
    effect = H.trigger_effect(marker)
    needle = html_comment_marker(marker)
    bundle.controller.state.issue_comments.append(
        {
            "id": "IC_applied",
            "databaseId": 1001,
            "body": f"@codex review\n{needle}",
            "createdAt": "2026-07-21T12:01:00Z",
            "author": {"login": "orchestrator"},
        }
    )
    bundle.controller.state.save(bundle.controller.state_path)
    proof = bundle.gateway.reconcile_request_review(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC)
    )
    assert proof.proof is WriteProofKind.APPLIED
    reloaded = bundle.controller.reload()
    assert reloaded.mutation_counts.get("create_issue_comment", 0) == 0


def reconcile_request_review_proven_not_applied(bundle: WriteGatewayBundle) -> None:
    marker = build_opaque_trigger_marker(run_id=H.RUN_ID, cycle_number=2)
    effect = H.trigger_effect(marker)
    proof = bundle.gateway.reconcile_request_review(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC)
    )
    assert proof.proof is WriteProofKind.PROVEN_NOT_APPLIED
    reloaded = bundle.controller.reload()
    assert reloaded.mutation_counts.get("create_issue_comment", 0) == 0
    assert marker not in reloaded.applied_markers


def reconcile_request_review_unresolved(bundle: WriteGatewayBundle) -> None:
    marker = build_opaque_trigger_marker(run_id=H.RUN_ID, cycle_number=3)
    effect = H.trigger_effect(marker)
    needle = html_comment_marker(marker)
    bundle.controller.state.issue_comments.extend(
        [
            {
                "id": "IC_dup_a",
                "databaseId": 2001,
                "body": needle,
                "createdAt": "2026-07-21T12:00:00Z",
                "author": {"login": "a"},
            },
            {
                "id": "IC_dup_b",
                "databaseId": 2002,
                "body": needle,
                "createdAt": "2026-07-21T12:01:00Z",
                "author": {"login": "b"},
            },
        ]
    )
    bundle.controller.state.save(bundle.controller.state_path)
    proof = bundle.gateway.reconcile_request_review(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC)
    )
    assert proof.proof is WriteProofKind.UNRESOLVED


def reconcile_resolve_thread_applied(bundle: WriteGatewayBundle) -> None:
    effect = H.resolve_thread_effect()
    thread_id = effect.thread_id
    bundle.controller.state.fixture["default_thread_id"] = thread_id
    bundle.controller.state.resolved_threads.append(thread_id)
    bundle.controller.state.save(bundle.controller.state_path)
    proof = bundle.gateway.reconcile_resolve_thread(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC)
    )
    assert proof.proof is WriteProofKind.APPLIED


def reconcile_resolve_thread_proven_not_applied(bundle: WriteGatewayBundle) -> None:
    effect = H.resolve_thread_effect()
    bundle.controller.state.fixture["default_thread_id"] = effect.thread_id
    bundle.controller.state.resolved_threads.clear()
    bundle.controller.state.save(bundle.controller.state_path)
    proof = bundle.gateway.reconcile_resolve_thread(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC)
    )
    assert proof.proof is WriteProofKind.PROVEN_NOT_APPLIED


def reconcile_post_thread_reply_applied(bundle: WriteGatewayBundle) -> None:
    reply_text = "Thanks for the review."
    reply_ref = H.write_reply(bundle.art, H.RUN_ID, reply_text)
    effect = H.reply_effect(reply_ref)
    marker = derive_content_bound_marker(
        operation="post_thread_reply",
        target_kind="thread_reply",
        idempotency_key=effect.idempotency_key,
        canonical_content=reply_text,
    )
    body = append_owned_marker(reply_text, marker.marker_text)
    bundle.controller.state.thread_replies[effect.thread_id] = [
        {
            "id": "RC_1",
            "body": body,
            "createdAt": "2026-07-21T12:02:00Z",
            "author": {"login": "orchestrator"},
        }
    ]
    bundle.controller.state.save(bundle.controller.state_path)
    proof = bundle.gateway.reconcile_post_thread_reply(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC)
    )
    assert proof.proof is WriteProofKind.APPLIED


def reconcile_post_thread_reply_proven_not_applied(bundle: WriteGatewayBundle) -> None:
    reply_ref = H.write_reply(bundle.art, H.RUN_ID, "Missing on remote.")
    effect = H.reply_effect(reply_ref)
    bundle.controller.state.thread_replies.clear()
    bundle.controller.state.save(bundle.controller.state_path)
    proof = bundle.gateway.reconcile_post_thread_reply(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC)
    )
    assert proof.proof is WriteProofKind.PROVEN_NOT_APPLIED


def reconcile_create_or_update_pr_applied(bundle: WriteGatewayBundle) -> None:
    title = "Title"
    body = "Body marker"
    pub_ref = H.write_publication(bundle.art, H.RUN_ID, title=title, body=body)
    effect = H.create_pr_effect(pub_ref)
    marker = derive_content_bound_marker(
        operation="create_or_update_pr",
        target_kind="pr_body",
        idempotency_key=effect.idempotency_key,
        canonical_content=canonicalize_publication_text(title=title, body=body),
    )
    bundle.controller.state.fixture["title"] = title
    bundle.controller.state.fixture["body"] = append_owned_marker(body, marker.marker_text)
    bundle.controller.state.fixture["head_sha"] = H.SHA_COMMIT
    bundle.controller.state.save(bundle.controller.state_path)
    proof = bundle.gateway.reconcile_create_or_update_pr(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC)
    )
    assert proof.proof is WriteProofKind.APPLIED


def reconcile_create_or_update_pr_proven_not_applied(bundle: WriteGatewayBundle) -> None:
    pub_ref = H.write_publication(bundle.art, H.RUN_ID, title="Title", body="Absent")
    effect = H.create_pr_effect(pub_ref)
    bundle.controller.state.fixture["pull_requests"] = []
    bundle.controller.state.fixture["head_sha"] = H.SHA_COMMIT
    bundle.controller.state.save(bundle.controller.state_path)
    proof = bundle.gateway.reconcile_create_or_update_pr(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC)
    )
    assert proof.proof is WriteProofKind.PROVEN_NOT_APPLIED


def reconcile_update_pr_text_applied(bundle: WriteGatewayBundle) -> None:
    title = "Updated"
    body = "Updated body"
    pub_ref = H.write_publication(bundle.art, H.RUN_ID, title=title, body=body)
    effect = H.update_pr_text_effect(pub_ref)
    marker = derive_content_bound_marker(
        operation="update_pr_text",
        target_kind="pr_body",
        idempotency_key=effect.idempotency_key,
        canonical_content=canonicalize_publication_text(title=title, body=body),
    )
    bundle.controller.state.fixture["title"] = title
    bundle.controller.state.fixture["body"] = append_owned_marker(body, marker.marker_text)
    bundle.controller.state.fixture["head_sha"] = H.SHA_COMMIT
    bundle.controller.state.save(bundle.controller.state_path)
    proof = bundle.gateway.reconcile_update_pr_text(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC)
    )
    assert proof.proof is WriteProofKind.APPLIED


def reconcile_update_pr_text_proven_not_applied(bundle: WriteGatewayBundle) -> None:
    title = "Updated"
    body = "Missing"
    pub_ref = H.write_publication(bundle.art, H.RUN_ID, title=title, body=body)
    effect = H.update_pr_text_effect(pub_ref)
    old_marker = derive_content_bound_marker(
        operation="update_pr_text",
        target_kind="pr_body",
        idempotency_key="different-key",
        canonical_content=canonicalize_publication_text(title=title, body="old body"),
    )
    bundle.controller.state.fixture["title"] = title
    bundle.controller.state.fixture["body"] = append_owned_marker(
        "old body", old_marker.marker_text
    )
    bundle.controller.state.fixture["head_sha"] = H.SHA_COMMIT
    bundle.controller.state.save(bundle.controller.state_path)
    proof = bundle.gateway.reconcile_update_pr_text(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC)
    )
    assert proof.proof is WriteProofKind.PROVEN_NOT_APPLIED


RECONCILE_MATRIX: dict[str, dict[str, Callable[[WriteGatewayBundle], None]]] = {
    "request_bot_review": {
        "APPLIED": reconcile_request_review_applied,
        "PROVEN_NOT_APPLIED": reconcile_request_review_proven_not_applied,
        "UNRESOLVED": reconcile_request_review_unresolved,
    },
    "resolve_thread": {
        "APPLIED": reconcile_resolve_thread_applied,
        "PROVEN_NOT_APPLIED": reconcile_resolve_thread_proven_not_applied,
    },
    "post_thread_reply": {
        "APPLIED": reconcile_post_thread_reply_applied,
        "PROVEN_NOT_APPLIED": reconcile_post_thread_reply_proven_not_applied,
    },
    "create_or_update_pr": {
        "APPLIED": reconcile_create_or_update_pr_applied,
        "PROVEN_NOT_APPLIED": reconcile_create_or_update_pr_proven_not_applied,
    },
    "update_pr_text": {
        "APPLIED": reconcile_update_pr_text_applied,
        "PROVEN_NOT_APPLIED": reconcile_update_pr_text_proven_not_applied,
    },
}


def wait_for_path(path: Path, *, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for {path}")


def install_blocking_agent(
    bin_dir: Path,
    *,
    ready_path: Path,
    proceed_path: Path,
    chat_id: str = "019abc00-1111-2222-3333-444444444444",
    ledger_path: Path | None = None,
) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "agent"
    ledger = str(ledger_path) if ledger_path is not None else ""
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, os, sys, time
            ready = {str(ready_path)!r}
            proceed = {str(proceed_path)!r}
            chat_id = {chat_id!r}
            ledger = {ledger!r}
            args = sys.argv[1:]
            def _ledger(kind):
                if not ledger:
                    return
                with open(ledger, "a", encoding="utf-8") as handle:
                    handle.write(kind + "\\n")
            if args and args[0] == "create-chat":
                _ledger("agent:create-chat")
                print(chat_id)
                raise SystemExit(0)
            if args and args[0] == "--version":
                print("agent 1.0.0")
                raise SystemExit(0)
            if args and args[0] == "models":
                print("composer-2.5-fast")
                raise SystemExit(0)
            if args and args[0] == "status":
                print(json.dumps({{"authenticated": True}}))
                raise SystemExit(0)
            if "-p" in args:
                resume_id = args[args.index("--resume") + 1] if "--resume" in args else "<missing>"
                _ledger("agent:prompt:" + resume_id)
                with open(ready, "w", encoding="utf-8") as handle:
                    handle.write(f"ready\\n{{os.getpid()}}\\n{{os.getpgid(0)}}\\n")
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    if os.path.isfile(proceed):
                        break
                    time.sleep(0.02)
                else:
                    print("blocking agent timed out", file=sys.stderr)
                    raise SystemExit(2)
                if "--workspace" in args:
                    workspace = args[args.index("--workspace") + 1]
                    target = os.path.join(workspace, "app.py")
                    with open(target, "a", encoding="utf-8") as handle:
                        handle.write("y = 2\\n")
                print(json.dumps({{"type": "result", "subtype": "success", "result": "done"}}))
                raise SystemExit(0)
            print("agent ok")
            raise SystemExit(0)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def install_blocking_codex(
    bin_dir: Path,
    *,
    ready_path: Path,
    proceed_path: Path,
    delegate_executable: Path | None = None,
    ledger_path: Path | None = None,
) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "codex"
    delegate_literal = "None" if delegate_executable is None else repr(str(delegate_executable))
    ledger_literal = "None" if ledger_path is None else repr(str(ledger_path))
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os, sys, time
            ready = __READY__
            proceed = __PROCEED__
            delegate = __DELEGATE__
            ledger = __LEDGER__
            args = sys.argv[1:]
            def _ledger(kind):
                if ledger is None:
                    return
                with open(ledger, "a", encoding="utf-8") as handle:
                    handle.write(kind + "\\n")
            if "exec" in args or "resume" in args:
                session_id = args[args.index("-") - 1] if "-" in args else "<missing>"
                _ledger("codex:review:" + session_id)
                with open(ready, "w", encoding="utf-8") as handle:
                    handle.write("ready\\n" + str(os.getpid()) + "\\n" + str(os.getpgid(0)) + "\\n")
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    if os.path.isfile(proceed):
                        break
                    time.sleep(0.02)
                else:
                    print("blocking codex timed out", file=sys.stderr)
                    raise SystemExit(2)
                print(
                    '{"has_actionable_findings": false, "findings_count": 0, '
                    '"highest_severity": null, "cursor_fix_prompt": null, '
                    '"tests_status": "passed", "review_markdown": "ok"}'
                )
                raise SystemExit(0)
            if delegate is not None:
                os.execv(delegate, [delegate, *args])
            print("codex ok")
            raise SystemExit(0)
            """
        )
        .replace("__READY__", repr(str(ready_path)))
        .replace("__PROCEED__", repr(str(proceed_path)))
        .replace("__DELEGATE__", delegate_literal)
        .replace("__LEDGER__", ledger_literal),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def sqlite_table_dump(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        chunks: list[str] = []
        for (name,) in rows:
            data = conn.execute(f"SELECT * FROM {name}").fetchall()
            chunks.append(json.dumps(data, default=str))
        return "\n".join(chunks)
    finally:
        conn.close()


CARRIER_CHECKPOINTS = (
    "before_seed",
    "after_seed",
    "before_cursor",
    "mid_cursor",
    "after_cursor_before_staging",
    "after_staging_before_codex",
    "mid_codex",
    "after_finalization_before_v2_result",
    "terminal_reopen",
)


def read_ready_pid(ready_path: Path) -> int:
    """Return the child PID recorded by blocking checkpoint fakes."""

    lines = ready_path.read_text(encoding="utf-8").strip().splitlines()
    if len(lines) >= 2:
        return int(lines[1].strip())
    if lines and lines[0].strip().isdigit():
        return int(lines[0].strip())
    raise AssertionError(f"ready marker missing pid: {ready_path}")


def read_ready_pgid(ready_path: Path) -> int:
    """Return the child PGID recorded by blocking checkpoint fakes."""

    lines = ready_path.read_text(encoding="utf-8").strip().splitlines()
    if len(lines) >= 3:
        return int(lines[2].strip())
    # Legacy two-line markers: fall back to the child's own process group.
    pid = read_ready_pid(ready_path)
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return pid


def install_term_refusing_codex(
    bin_dir: Path,
    *,
    ready_path: Path,
    proceed_path: Path,
) -> Path:
    """Fake codex child that ignores SIGTERM and must be SIGKILLed."""

    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "codex"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os, signal, sys, time
            ready = __READY__
            proceed = __PROCEED__
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            args = sys.argv[1:]
            if "exec" in args or "resume" in args:
                with open(ready, "w", encoding="utf-8") as handle:
                    handle.write(f"ready\\n{{os.getpid()}}\\n")
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    if os.path.isfile(proceed):
                        print('{"title":"t","body":"b","commit_subject":"s","commit_body":"c"}')
                        raise SystemExit(0)
                    time.sleep(0.02)
                print("term-refusing codex timed out", file=sys.stderr)
                raise SystemExit(2)
            print("codex ok")
            raise SystemExit(0)
            """
        )
        .replace("__READY__", repr(str(ready_path)))
        .replace("__PROCEED__", repr(str(proceed_path))),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def install_checkpoint_blocking_agent(
    bin_dir: Path,
    *,
    ready_path: Path,
    proceed_path: Path,
    chat_id: str = "019abc00-1111-2222-3333-444444444444",
    ledger_path: Path | None = None,
) -> Path:
    """Blocking agent that writes a checkpoint readiness marker then waits."""

    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "agent"
    ledger = str(ledger_path) if ledger_path is not None else ""
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, os, sys, time
            ready = {str(ready_path)!r}
            proceed = {str(proceed_path)!r}
            chat_id = {chat_id!r}
            ledger = {ledger!r}
            args = sys.argv[1:]
            def _ledger(kind):
                if not ledger:
                    return
                with open(ledger, "a", encoding="utf-8") as handle:
                    handle.write(kind + "\\n")
            if args and args[0] == "create-chat":
                _ledger("agent:create-chat")
                print(chat_id)
                raise SystemExit(0)
            if args and args[0] == "--version":
                print("agent 1.0.0")
                raise SystemExit(0)
            if args and args[0] == "models":
                print("composer-2.5-fast")
                raise SystemExit(0)
            if args and args[0] == "status":
                print(json.dumps({{"authenticated": True}}))
                raise SystemExit(0)
            if "-p" in args:
                resume_id = args[args.index("--resume") + 1] if "--resume" in args else "<missing>"
                _ledger("agent:prompt:" + resume_id)
                with open(ready, "w", encoding="utf-8") as handle:
                    handle.write(f"ready\\n{{os.getpid()}}\\n{{os.getpgid(0)}}\\n")
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    if os.path.isfile(proceed):
                        break
                    time.sleep(0.02)
                else:
                    print("blocking agent timed out", file=sys.stderr)
                    raise SystemExit(2)
                if "--workspace" in args:
                    workspace = args[args.index("--workspace") + 1]
                    target = os.path.join(workspace, "app.py")
                    with open(target, "a", encoding="utf-8") as handle:
                        handle.write("y = 2\\n")
                print(json.dumps({{"type": "result", "subtype": "success", "result": "done"}}))
                raise SystemExit(0)
            print("agent ok")
            raise SystemExit(0)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def assert_completion_fenced(
    engine: PrReviewEngine,
    lease,
    claim,
    event,
    *,
    owner: str = "owner-a",
) -> None:
    from ai_dev_loop.pr_review_v2.application.contracts import EffectCompletionRequest

    receipt = engine.complete_claim(
        EffectCompletionRequest(
            submission_id="late-complete",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id=owner,
            lease_generation=lease.generation,
            event=event,
        )
    )
    assert receipt.disposition is EventDisposition.STALE


def assert_production_term_refusal_kill(
    *,
    codex_path: Path,
    ready_path: Path,
    artifact_root: Path,
    run_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise ProcessCodexRunner TERM→grace→KILL against a SIGTERM-ignoring child."""

    import os
    import signal
    import subprocess

    from ai_dev_loop.pr_review_v2.runtime_factory import ProcessCodexRunner

    proc = subprocess.Popen(
        [str(codex_path), "exec", "resume", "session", "-"],
        start_new_session=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wait_for_path(ready_path, timeout_seconds=5.0)
    assert proc.poll() is None

    signals: list[int] = []
    real_killpg = os.killpg

    def _track_killpg(pgid: int, sig: int) -> None:
        signals.append(int(sig))
        if sig == signal.SIGTERM:
            assert proc.poll() is None, "child must ignore SIGTERM during grace"
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", _track_killpg)
    runner = ProcessCodexRunner(
        artifact_root=artifact_root,
        run_id=run_id,
        term_grace_seconds=0.15,
    )
    runner._terminate_and_reap(proc)
    assert signal.SIGTERM in signals
    assert signal.SIGKILL in signals
    assert proc.poll() is not None
