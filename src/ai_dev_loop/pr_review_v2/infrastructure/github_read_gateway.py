"""Read-only GitHub observation gateway for PR review v2."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, runtime_checkable

from ai_dev_loop.pr_review_v2.application.github_read import (
    GatewayBlockKind,
    GatewayTransient,
    GatewayTransientKind,
    GhTransportResult,
    GitHubReadPolicy,
    ObservationEvidenceKind,
    ObservationSnapshot,
    ObservedIssueComment,
    ObservedNoFindingsReaction,
    ObservedReaction,
    ObservedReviewThread,
    ObservedTriggerComment,
    block_for_kind,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    ArtifactRef,
    PullRequestBinding,
    TransientErrorKind,
    coerce_utc_instant,
)
from ai_dev_loop.pr_review_v2.domain.effects import ObserveBotReviewEffect
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import (
    ArtifactStoreError,
    ReviewArtifactStore,
    sha256_text,
)
from ai_dev_loop.redaction import redact_text

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_REVIEWED_COMMIT_LINE_RE = re.compile(
    r"(?m)^\s*(?:\*\*)?Reviewed commit:(?:\*\*)?\s*`?([0-9a-fA-F]+)`?\s*$"
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SESSION_LIKE_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


@runtime_checkable
class GitHubReadTransport(Protocol):
    """Narrow typed read ports; no arbitrary GraphQL/REST query surface."""

    def fetch_pull_request_identity(
        self, *, owner: str, name: str, number: int, timeout_seconds: float | None = None
    ) -> GhTransportResult: ...

    def fetch_issue_comments_page(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        cursor: str | None,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult: ...

    def fetch_review_threads_page(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        cursor: str | None,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult: ...

    def fetch_issue_comment_reactions_page(
        self,
        *,
        owner: str,
        name: str,
        comment_id: str,
        page: int,
        per_page: int = 100,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult: ...


@dataclass(frozen=True)
class _EphemeralComment:
    comment_id: str
    author_login: str
    created_at: datetime
    body: str
    body_sha256: str


@dataclass(frozen=True)
class _EphemeralThread:
    thread_id: str
    is_resolved: bool
    author_login: str
    created_at: datetime
    commit_sha: str
    root_comment_id: str
    root_body: str
    root_body_sha256: str
    path: str | None
    line: int | None
    review_id: str | None


class GitHubReadGateway:
    """Observe and validate remote PR bot feedback without writes or SQLite."""

    def __init__(
        self,
        *,
        policy: GitHubReadPolicy,
        transport: GitHubReadTransport,
        artifacts: ReviewArtifactStore,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._policy = policy
        self._transport = transport
        self._artifacts = artifacts
        self._monotonic = monotonic or time.monotonic

    def observe_with_artifact(
        self,
        effect: ObserveBotReviewEffect,
        *,
        observation_time: datetime,
    ) -> tuple[ObservationSnapshot, ArtifactRef]:
        if effect.trigger_marker is None or not str(effect.trigger_marker).strip():
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail="observe effect is missing trigger_marker",
                )
            )
        deadline = self._monotonic() + self._policy.overall_timeout_seconds
        owner, name = _split_nwo(effect.binding.repository.name_with_owner)
        identity = self._fetch_identity(
            owner=owner,
            name=name,
            number=effect.binding.pr_number,
            deadline=deadline,
        )
        self._validate_identity(effect.binding, identity)
        comments = self._fetch_issue_comments(
            owner=owner,
            name=name,
            number=effect.binding.pr_number,
            deadline=deadline,
        )
        trigger = self._select_trigger(comments, marker=effect.trigger_marker)
        threads = self._fetch_review_threads(
            owner=owner,
            name=name,
            number=effect.binding.pr_number,
            deadline=deadline,
        )
        eligible = self._select_eligible_threads(
            threads,
            bound_head_sha=effect.binding.head_sha,
            trigger_created_at=trigger.created_at,
        )
        comment_no_findings = self._select_comment_no_findings(
            comments,
            bound_head_sha=effect.binding.head_sha,
            trigger_created_at=trigger.created_at,
        )
        reactions = self._fetch_trigger_reactions(
            owner=owner,
            name=name,
            comment_id=trigger.comment_id,
            deadline=deadline,
        )
        reaction_no_findings = self._select_no_findings_from_reactions(
            reactions,
            trigger_comment_id=trigger.comment_id,
            trigger_created_at=trigger.created_at,
        )
        if comment_no_findings is not None and reaction_no_findings is not None:
            raise GhTransportError(block=block_for_kind(GatewayBlockKind.CONTRADICTORY_EVIDENCE))
        if eligible and (comment_no_findings is not None or reaction_no_findings is not None):
            raise GhTransportError(block=block_for_kind(GatewayBlockKind.CONTRADICTORY_EVIDENCE))
        total_sanitized = 0
        if eligible:
            evidence_kind = ObservationEvidenceKind.ELIGIBLE_THREADS
            observed_threads: list[ObservedReviewThread] = []
            for thread in eligible:
                observed = self._to_observed_thread(thread)
                total_sanitized += len(observed.sanitized_root_body)
                if total_sanitized > self._policy.max_total_sanitized_chars:
                    raise GhTransportError(
                        block=block_for_kind(
                            GatewayBlockKind.MALFORMED_EVIDENCE,
                            detail="total sanitized review text exceeded configured bounds",
                        )
                    )
                observed_threads.append(observed)
            observed_no_findings = None
            observed_no_findings_reaction = None
        elif comment_no_findings is not None:
            evidence_kind = ObservationEvidenceKind.VERIFIED_NO_FINDINGS
            observed_threads = []
            observed_no_findings = comment_no_findings
            observed_no_findings_reaction = None
        elif reaction_no_findings is not None:
            evidence_kind = ObservationEvidenceKind.VERIFIED_NO_FINDINGS
            observed_threads = []
            observed_no_findings = None
            observed_no_findings_reaction = reaction_no_findings
        else:
            evidence_kind = ObservationEvidenceKind.BOT_STILL_WAITING
            observed_threads = []
            observed_no_findings = None
            observed_no_findings_reaction = None
        snapshot = ObservationSnapshot(
            binding=effect.binding,
            cycle_number=effect.cycle_number,
            poll_sequence=effect.poll_sequence,
            trigger_marker=effect.trigger_marker,
            observed_at=observation_time,
            evidence_kind=evidence_kind,
            trigger=ObservedTriggerComment(
                comment_id=trigger.comment_id,
                author_login=trigger.author_login,
                created_at=trigger.created_at,
                body_sha256=trigger.body_sha256,
            ),
            eligible_threads=tuple(observed_threads),
            no_findings_comment=observed_no_findings,
            no_findings_reaction=observed_no_findings_reaction,
            reactions=tuple(reactions),
        )
        try:
            ref = self._artifacts.persist_observation_for_run(
                run_id=effect.run_id,
                snapshot=snapshot,
            )
        except ArtifactStoreError as exc:
            raise GhTransportError(block=exc.block) from exc
        return snapshot, ref

    def _ensure_time(self, deadline: float) -> None:
        if self._monotonic() >= deadline:
            raise GhTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TIMEOUT,
                    safe_summary="observation overall deadline exceeded",
                    transient_kind=TransientErrorKind.TIMEOUT,
                )
            )

    def _call_timeout_seconds(self, deadline: float) -> float:
        """Cap each remote call at min(per_call_timeout, remaining overall deadline)."""

        self._ensure_time(deadline)
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            self._ensure_time(deadline)
        return min(float(self._policy.per_call_timeout_seconds), remaining)

    def _fetch_identity(
        self, *, owner: str, name: str, number: int, deadline: float
    ) -> dict[str, Any]:
        timeout = self._call_timeout_seconds(deadline)
        result = self._transport.fetch_pull_request_identity(
            owner=owner, name=name, number=number, timeout_seconds=timeout
        )
        data = _require_dict(result.body_json, "identity response")
        try:
            repo = data["data"]["repository"]
            if repo is None:
                raise GhTransportError(block=block_for_kind(GatewayBlockKind.NOT_FOUND))
            pr = repo["pullRequest"]
            if pr is None:
                raise GhTransportError(block=block_for_kind(GatewayBlockKind.NOT_FOUND))
            return {
                "name_with_owner": str(repo["nameWithOwner"]),
                "number": int(pr["number"]),
                "state": str(pr["state"]),
                "is_cross_repository": bool(pr["isCrossRepository"]),
                "head_branch": str(pr["headRefName"]),
                "base_branch": str(pr["baseRefName"]),
                "head_sha": str(pr["headRefOid"]).lower(),
            }
        except GhTransportError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail="pull request identity response was incomplete",
                )
            ) from exc

    def _validate_identity(self, binding: PullRequestBinding, identity: dict[str, Any]) -> None:
        if identity["name_with_owner"] != binding.repository.name_with_owner:
            raise GhTransportError(block=block_for_kind(GatewayBlockKind.REPOSITORY_DRIFT))
        if identity["number"] != binding.pr_number:
            raise GhTransportError(block=block_for_kind(GatewayBlockKind.REPOSITORY_DRIFT))
        if identity["is_cross_repository"]:
            raise GhTransportError(block=block_for_kind(GatewayBlockKind.UNSUPPORTED_FORK))
        if str(identity["state"]).upper() != "OPEN":
            raise GhTransportError(block=block_for_kind(GatewayBlockKind.CLOSED_PR))
        if (
            identity["head_branch"] != binding.head_branch
            or identity["base_branch"] != binding.base_branch
        ):
            raise GhTransportError(block=block_for_kind(GatewayBlockKind.BRANCH_DRIFT))
        head_sha = identity["head_sha"]
        if not _FULL_SHA.match(head_sha):
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail="pull request head SHA was not a full 40-character digest",
                )
            )
        if head_sha != binding.head_sha:
            raise GhTransportError(block=block_for_kind(GatewayBlockKind.HEAD_DRIFT))

    def _fetch_issue_comments(
        self, *, owner: str, name: str, number: int, deadline: float
    ) -> list[_EphemeralComment]:
        nodes = self._paginate_graphql_nodes(
            kind="issue_comments",
            owner=owner,
            name=name,
            number=number,
            connection_path=("repository", "pullRequest", "comments"),
            deadline=deadline,
        )
        comments: list[_EphemeralComment] = []
        seen_ids: set[str] = set()
        for node in nodes:
            if not isinstance(node, dict):
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="issue comment node was malformed",
                    )
                )
            comment_id = str(node.get("databaseId") or node.get("id") or "").strip()
            if not comment_id:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="issue comment missing id",
                    )
                )
            if comment_id in seen_ids:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="duplicate issue comment id",
                    )
                )
            seen_ids.add(comment_id)
            author = node.get("author") if isinstance(node.get("author"), dict) else {}
            login = str((author or {}).get("login") or "").strip()
            created_raw = node.get("createdAt")
            body = str(node.get("body") or "")
            if not login or not created_raw:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="issue comment missing author or timestamp",
                    )
                )
            try:
                created_at = coerce_utc_instant(str(created_raw))
            except (TypeError, ValueError) as exc:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="issue comment timestamp was invalid",
                    )
                ) from exc
            comments.append(
                _EphemeralComment(
                    comment_id=comment_id,
                    author_login=login,
                    created_at=created_at,
                    body=body,
                    body_sha256=sha256_text(body),
                )
            )
        return comments

    def _fetch_review_threads(
        self, *, owner: str, name: str, number: int, deadline: float
    ) -> list[_EphemeralThread]:
        nodes = self._paginate_graphql_nodes(
            kind="review_threads",
            owner=owner,
            name=name,
            number=number,
            connection_path=("repository", "pullRequest", "reviewThreads"),
            deadline=deadline,
        )
        threads: list[_EphemeralThread] = []
        seen_ids: set[str] = set()
        for node in nodes:
            if not isinstance(node, dict):
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="review thread node was malformed",
                    )
                )
            thread_id = str(node.get("id") or "").strip()
            if not thread_id:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="review thread missing id",
                    )
                )
            if thread_id in seen_ids:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="duplicate review thread id",
                    )
                )
            seen_ids.add(thread_id)
            comments = (node.get("comments") or {}).get("nodes") or []
            if not comments:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="review thread missing root comment",
                    )
                )
            root = comments[0]
            if not isinstance(root, dict):
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="review thread root comment was malformed",
                    )
                )
            author = root.get("author") if isinstance(root.get("author"), dict) else {}
            login = str((author or {}).get("login") or "").strip()
            created_raw = root.get("createdAt")
            root_id = str(root.get("id") or "").strip()
            body = str(root.get("body") or "")
            commit = root.get("commit") if isinstance(root.get("commit"), dict) else {}
            commit_sha = str((commit or {}).get("oid") or "").lower()
            review = (
                root.get("pullRequestReview")
                if isinstance(root.get("pullRequestReview"), dict)
                else {}
            )
            if not login or not created_raw or not root_id:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="review thread root provenance was incomplete",
                    )
                )
            if not commit_sha or not _FULL_SHA.match(commit_sha):
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="review thread commit SHA was incomplete",
                    )
                )
            try:
                created_at = coerce_utc_instant(str(created_raw))
            except (TypeError, ValueError) as exc:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="review thread timestamp was invalid",
                    )
                ) from exc
            line = root.get("line")
            threads.append(
                _EphemeralThread(
                    thread_id=thread_id,
                    is_resolved=bool(node.get("isResolved")),
                    author_login=login,
                    created_at=created_at,
                    commit_sha=commit_sha,
                    root_comment_id=root_id,
                    root_body=body,
                    root_body_sha256=sha256_text(body),
                    path=str(root["path"]) if root.get("path") is not None else None,
                    line=int(line) if isinstance(line, int) else None,
                    review_id=str(review["id"]) if review and review.get("id") else None,
                )
            )
        return threads

    def _paginate_graphql_nodes(
        self,
        *,
        kind: Literal["issue_comments", "review_threads"],
        owner: str,
        name: str,
        number: int,
        connection_path: tuple[str, ...],
        deadline: float,
    ) -> list[Any]:
        nodes: list[Any] = []
        cursor: str | None = None
        pages = 0
        previous_cursor: str | None = None
        while True:
            timeout = self._call_timeout_seconds(deadline)
            pages += 1
            if pages > self._policy.max_pages:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="pagination exceeded configured page limit",
                    )
                )
            if kind == "issue_comments":
                result = self._transport.fetch_issue_comments_page(
                    owner=owner,
                    name=name,
                    number=number,
                    cursor=cursor,
                    timeout_seconds=timeout,
                )
            else:
                result = self._transport.fetch_review_threads_page(
                    owner=owner,
                    name=name,
                    number=number,
                    cursor=cursor,
                    timeout_seconds=timeout,
                )
            data = _require_dict(result.body_json, f"{kind} response")
            try:
                cursor_obj: Any = data["data"]
                for key in connection_path:
                    cursor_obj = cursor_obj[key]
            except (KeyError, TypeError) as exc:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail=f"{kind} connection shape was incomplete",
                    )
                ) from exc
            page_nodes, has_next, end_cursor = _parse_graphql_connection(cursor_obj, kind=kind)
            nodes.extend(page_nodes)
            if len(nodes) > self._policy.max_items:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="pagination exceeded configured item limit",
                    )
                )
            if not has_next:
                break
            if end_cursor is None:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="pagination cursor was missing for hasNextPage",
                    )
                )
            if end_cursor == previous_cursor:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="pagination cursor did not advance",
                    )
                )
            previous_cursor = end_cursor
            cursor = end_cursor
        return nodes

    def _select_trigger(
        self, comments: list[_EphemeralComment], *, marker: str
    ) -> _EphemeralComment:
        if not marker.strip() or "-->" in marker or "<!--" in marker:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail="trigger marker was invalid",
                )
            )
        needle = f"<!-- {marker} -->"
        matches = [item for item in comments if needle in item.body]
        if not matches:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail="exact trigger comment was not found",
                )
            )
        if len(matches) > 1:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.CONTRADICTORY_EVIDENCE,
                    detail="duplicate trigger comments were found",
                )
            )
        return matches[0]

    def _select_eligible_threads(
        self,
        threads: list[_EphemeralThread],
        *,
        bound_head_sha: str,
        trigger_created_at: datetime,
    ) -> list[_EphemeralThread]:
        allowed = {login.lower() for login in self._policy.reviewer_logins}
        eligible: list[_EphemeralThread] = []
        for thread in threads:
            if thread.is_resolved:
                continue
            if thread.author_login.lower() not in allowed:
                continue
            if thread.commit_sha != bound_head_sha:
                continue
            if thread.created_at <= trigger_created_at:
                continue
            eligible.append(thread)
        return eligible

    def _select_comment_no_findings(
        self,
        comments: list[_EphemeralComment],
        *,
        bound_head_sha: str,
        trigger_created_at: datetime,
    ) -> ObservedIssueComment | None:
        if not self._policy.comment_no_findings_enabled:
            return None
        allowed = {login.lower() for login in self._policy.reviewer_logins}
        prefix_len = self._policy.reviewed_commit_prefix_length
        expected_prefix = bound_head_sha[:prefix_len].lower()
        matches: list[ObservedIssueComment] = []
        for comment in comments:
            if comment.author_login.lower() not in allowed:
                continue
            if comment.created_at <= trigger_created_at:
                continue
            matched_rule: str | None = None
            for index, prefix in enumerate(self._policy.accepted_no_findings_prefixes):
                if comment.body.startswith(prefix):
                    matched_rule = f"accepted_comment_prefix:{index}"
                    break
            if matched_rule is None:
                continue
            commit_match = _REVIEWED_COMMIT_LINE_RE.search(comment.body)
            if commit_match is None:
                continue
            reviewed = commit_match.group(1).lower()
            if len(reviewed) < prefix_len:
                continue
            if reviewed[:prefix_len] != expected_prefix:
                continue
            matches.append(
                ObservedIssueComment(
                    comment_id=comment.comment_id,
                    author_login=comment.author_login,
                    created_at=comment.created_at,
                    body_sha256=comment.body_sha256,
                    matched_no_findings_rule_id=matched_rule,
                    reviewed_commit_prefix=expected_prefix,
                    sanitized_body=None,
                )
            )
        if len(matches) > 1:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.CONTRADICTORY_EVIDENCE,
                    detail="multiple conflicting no-findings markers were found",
                )
            )
        return matches[0] if matches else None

    def _select_no_findings_from_reactions(
        self,
        reactions: list[ObservedReaction],
        *,
        trigger_comment_id: str,
        trigger_created_at: datetime,
    ) -> ObservedNoFindingsReaction | None:
        if not self._policy.accept_bot_thumbs_up:
            return None
        allowed = {login.lower() for login in self._policy.reviewer_logins}
        matches: list[ObservedReaction] = []
        for reaction in reactions:
            if reaction.user_login.lower() not in allowed:
                continue
            if reaction.content != "+1":
                continue
            # Eligible allowlisted +1 on the selected trigger with missing/unusable
            # timestamp is typed fail-closed evidence, not normal bot polling.
            if reaction.created_at is None:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="no-findings reaction timestamp was missing",
                    )
                )
            if reaction.created_at <= trigger_created_at:
                continue
            matches.append(reaction)
        if len(matches) > 1:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.CONTRADICTORY_EVIDENCE,
                    detail="multiple conflicting no-findings reactions were found",
                )
            )
        if not matches:
            return None
        chosen = matches[0]
        created_at = chosen.created_at
        assert created_at is not None
        return ObservedNoFindingsReaction(
            trigger_comment_id=trigger_comment_id,
            reaction_id=chosen.reaction_id,
            user_login=chosen.user_login,
            content="+1",
            created_at=created_at,
        )

    def _fetch_trigger_reactions(
        self, *, owner: str, name: str, comment_id: str, deadline: float
    ) -> list[ObservedReaction]:
        if not comment_id.isdigit():
            return []
        reactions: list[ObservedReaction] = []
        seen: set[str] = set()
        page = 1
        pages = 0
        per_page = 100
        while True:
            timeout = self._call_timeout_seconds(deadline)
            pages += 1
            if pages > self._policy.max_pages:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="pagination exceeded configured page limit",
                    )
                )
            result = self._transport.fetch_issue_comment_reactions_page(
                owner=owner,
                name=name,
                comment_id=comment_id,
                page=page,
                per_page=per_page,
                timeout_seconds=timeout,
            )
            items = _parse_json_items(result.body_json)
            if len(items) > per_page:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="reaction page exceeded declared per_page size",
                    )
                )
            for item in items:
                if not isinstance(item, dict):
                    raise GhTransportError(
                        block=block_for_kind(
                            GatewayBlockKind.MALFORMED_EVIDENCE,
                            detail="reaction node was malformed",
                        )
                    )
                reaction_id = str(item.get("id") or "").strip()
                if not reaction_id:
                    raise GhTransportError(
                        block=block_for_kind(
                            GatewayBlockKind.MALFORMED_EVIDENCE,
                            detail="reaction missing id",
                        )
                    )
                if reaction_id in seen:
                    raise GhTransportError(
                        block=block_for_kind(
                            GatewayBlockKind.MALFORMED_EVIDENCE,
                            detail="duplicate reaction id",
                        )
                    )
                seen.add(reaction_id)
                user = item.get("user") if isinstance(item.get("user"), dict) else {}
                login = str((user or {}).get("login") or "").strip()
                content = str(item.get("content") or "").strip()
                if not login or not content:
                    raise GhTransportError(
                        block=block_for_kind(
                            GatewayBlockKind.MALFORMED_EVIDENCE,
                            detail="reaction provenance was incomplete",
                        )
                    )
                created_at = None
                if item.get("created_at"):
                    try:
                        created_at = coerce_utc_instant(str(item["created_at"]))
                    except (TypeError, ValueError) as exc:
                        raise GhTransportError(
                            block=block_for_kind(
                                GatewayBlockKind.MALFORMED_EVIDENCE,
                                detail="reaction timestamp was invalid",
                            )
                        ) from exc
                reactions.append(
                    ObservedReaction(
                        reaction_id=reaction_id,
                        user_login=login,
                        content=content,
                        created_at=created_at,
                    )
                )
                if len(reactions) > self._policy.max_items:
                    raise GhTransportError(
                        block=block_for_kind(
                            GatewayBlockKind.MALFORMED_EVIDENCE,
                            detail="pagination exceeded configured item limit",
                        )
                    )
            if len(items) < per_page:
                break
            page += 1
        return reactions

    def _to_observed_thread(self, thread: _EphemeralThread) -> ObservedReviewThread:
        sanitized = sanitize_review_text(
            thread.root_body,
            max_chars=self._policy.max_sanitized_body_chars,
        )
        return ObservedReviewThread(
            thread_id=thread.thread_id,
            is_resolved=thread.is_resolved,
            author_login=thread.author_login,
            created_at=thread.created_at,
            commit_sha=thread.commit_sha,
            root_comment_id=thread.root_comment_id,
            root_body_sha256=thread.root_body_sha256,
            path=thread.path,
            line=thread.line,
            review_id=thread.review_id,
            sanitized_root_body=sanitized,
        )


def sanitize_review_text(body: str, *, max_chars: int) -> str:
    """Sanitize review text for protected artifacts; fail closed on incompleteness."""

    if _CONTROL_CHAR_RE.search(body):
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail="review text contained disallowed control characters",
            )
        )
    without_markers = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    redacted = redact_text(without_markers)
    redacted = _SESSION_LIKE_RE.sub("<redacted-session>", redacted)
    cleaned = redacted.strip()
    if not cleaned:
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail="sanitized review text was empty",
            )
        )
    if len(cleaned) > max_chars:
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail="sanitized review text exceeded configured bounds",
            )
        )
    return cleaned


def next_poll_not_before(observation_time: datetime, *, poll_interval_seconds: int) -> datetime:
    if observation_time.tzinfo is None:
        observation_time = observation_time.replace(tzinfo=UTC)
    return observation_time + timedelta(seconds=poll_interval_seconds)


def _split_nwo(name_with_owner: str) -> tuple[str, str]:
    owner, name = name_with_owner.split("/", 1)
    return owner, name


def _require_dict(value: object | None, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail=f"{label} was not a JSON object",
            )
        )
    return value


def _parse_graphql_connection(
    connection: object, *, kind: str
) -> tuple[list[Any], bool, str | None]:
    """Strictly parse a GraphQL connection; never coerce null/truthy shapes."""

    if not isinstance(connection, dict):
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail=f"{kind} connection was not an object",
            )
        )
    if "nodes" not in connection:
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail=f"{kind} nodes field was missing",
            )
        )
    page_nodes = connection["nodes"]
    if page_nodes is None:
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail=f"{kind} nodes were null",
            )
        )
    if not isinstance(page_nodes, list):
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail=f"{kind} nodes were not a list",
            )
        )
    if "pageInfo" not in connection:
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail=f"{kind} pageInfo field was missing",
            )
        )
    page_info = connection["pageInfo"]
    if not isinstance(page_info, dict):
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail=f"{kind} pageInfo was not an object",
            )
        )
    if "hasNextPage" not in page_info:
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail=f"{kind} hasNextPage field was missing",
            )
        )
    has_next = page_info["hasNextPage"]
    if not isinstance(has_next, bool):
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail=f"{kind} hasNextPage was not a boolean",
            )
        )
    end_cursor: str | None = None
    if has_next:
        if "endCursor" not in page_info:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail=f"{kind} endCursor was missing for hasNextPage",
                )
            )
        raw_cursor = page_info["endCursor"]
        if not isinstance(raw_cursor, str) or not raw_cursor:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail=f"{kind} endCursor was not a non-empty string",
                )
            )
        end_cursor = raw_cursor
    elif "endCursor" in page_info and page_info["endCursor"] is not None:
        raw_cursor = page_info["endCursor"]
        if not isinstance(raw_cursor, str):
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail=f"{kind} endCursor was not a string",
                )
            )
        end_cursor = raw_cursor or None
    return page_nodes, has_next, end_cursor


def _parse_json_items(value: object | None) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    raise GhTransportError(
        block=block_for_kind(
            GatewayBlockKind.MALFORMED_EVIDENCE,
            detail="paginated JSON items were malformed",
        )
    )
