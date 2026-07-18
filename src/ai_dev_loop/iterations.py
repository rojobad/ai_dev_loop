"""Iteration metadata helpers for multi-iteration runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.state import RunState, atomic_write_text, sha256_file, sha256_text

CORRECTION_ENVELOPE_HEADER = """This is an ai_dev_loop correction turn.

You may stage or unstage files when required by the confirmed fixes, including updating
.gitignore and removing ignored files from the index.

Do not commit, amend, reset, checkout/switch branches, stash, clean, merge, rebase, tag,
push, or otherwise change Git history or repository identity.

The orchestrator will run git add -A after this turn and Codex will review the complete
resulting staged snapshot. Merely unstaging a tracked non-ignored modification will not
exclude it from the final snapshot.

Codex findings:
"""

USAGE_LIMIT_CONTINUATION_HEADER = """This is an ai_dev_loop Cursor recovery turn in the existing chat.

The previous Cursor turn stopped because its configured model reached a usage limit.
Continue this same task using the currently selected model.

Inspect the current repository state before editing. Existing tracked unstaged changes
and untracked non-ignored files are partial work from the interrupted turn. Preserve
and complete valid work; do not discard it or start the implementation over.

Do not commit, amend, reset, checkout/switch branches, stash, clean, merge, rebase,
tag, push, or change repository identity. The orchestrator will normalize staging and
Codex will review the complete staged result after this turn succeeds.

Previous orchestrator prompt follows verbatim:
"""


def iteration_label(number: int) -> str:
    return f"{number:02d}"


def find_iteration(state: RunState, number: int) -> dict[str, Any] | None:
    for entry in state.iterations:
        if entry.get("number") == number:
            return entry
    return None


def upsert_iteration(state: RunState, entry: dict[str, Any]) -> None:
    number = entry.get("number")
    if not isinstance(number, int):
        raise ValidationError("iteration entry requires integer number")
    for index, existing in enumerate(state.iterations):
        if existing.get("number") == number:
            merged = dict(existing)
            for key, value in entry.items():
                if key in {"cursor", "git", "codex", "review"} and key in merged:
                    section = dict(merged[key]) if isinstance(merged[key], dict) else {}
                    if isinstance(value, dict):
                        section.update(value)
                        merged[key] = section
                    else:
                        merged[key] = value
                else:
                    merged[key] = value
            state.iterations[index] = merged
            return
    state.iterations.append(entry)


def max_iteration_number(state: RunState) -> int:
    numbers = [
        entry["number"] for entry in state.iterations if isinstance(entry.get("number"), int)
    ]
    return max(numbers) if numbers else 0


def iteration_kind(number: int) -> str:
    if number == 1:
        return "initial_implementation"
    return "cursor_correction"


def fix_prompt_path(review_number: int) -> str:
    return f"prompts/fixes/{iteration_label(review_number)}.txt"


def correction_execution_envelope_path(review_number: int) -> str:
    return f"prompts/fixes/{iteration_label(review_number)}.execution-envelope.txt"


def usage_limit_continuation_path(iteration_number: int) -> str:
    return (
        f"prompts/cursor-recovery/{iteration_label(iteration_number)}.usage-limit-continuation.txt"
    )


def cursor_prompt_path(state: RunState, iteration_number: int) -> str:
    recovery = state.recovery
    if (
        recovery is not None
        and recovery.recovered_checkpoint == "cursor"
        and recovery.source_iteration == iteration_number
        and recovery.continuation_envelope_path
    ):
        return recovery.continuation_envelope_path
    github = state.github_pr_review
    if (
        github is not None
        and github.lifecycle == "fixing_external_feedback"
        and iteration_number == 1
        and github.external_fix_prompt_path
    ):
        return github.external_fix_prompt_path
    if iteration_number == 1:
        return state.prompt.snapshot_path
    return fix_prompt_path(iteration_number - 1)


def build_correction_execution_envelope(fix_prompt: str) -> str:
    """Wrap the exact Codex fix prompt in a deterministic operational envelope.

    The fix prompt body is embedded verbatim after the header. Callers must not
    summarize, translate, reorder, rewrite findings, or alter newlines.
    """

    return CORRECTION_ENVELOPE_HEADER + "\n" + fix_prompt


def build_usage_limit_continuation_envelope(source_prompt: str) -> str:
    """Wrap the exact previous Cursor prompt for usage-limit recovery.

    The source prompt body is embedded verbatim after the fixed header. For
    corrections this must be the exact Codex findings file, not a nested
    correction execution envelope.
    """

    return USAGE_LIMIT_CONTINUATION_HEADER + "\n" + source_prompt


def write_usage_limit_continuation_envelope(
    run_directory: Path,
    *,
    iteration_number: int,
    source_prompt: str,
) -> tuple[str, str]:
    """Persist the continuation envelope and return ``(relative_path, sha256)``."""

    envelope = build_usage_limit_continuation_envelope(source_prompt)
    if source_prompt not in envelope:
        raise ValidationError(
            "usage-limit continuation envelope must include the exact source prompt"
        )
    rel_path = usage_limit_continuation_path(iteration_number)
    atomic_write_text(run_directory / rel_path, envelope, sensitive=True)
    return rel_path, sha256_text(envelope)


def source_prompt_for_usage_limit_recovery(
    state: RunState,
    run_directory: Path,
    iteration_number: int,
) -> tuple[str, str]:
    """Return ``(relative_path, exact_text)`` for the prompt that failed mid-turn.

    Iteration 1 uses the initial snapshot. Corrections use the exact findings
    file (not a previous execution envelope).
    """

    if iteration_number == 1:
        rel_path = state.prompt.snapshot_path
        prompt_file = run_directory / rel_path
        if not prompt_file.is_file():
            raise ValidationError(f"source prompt missing for usage-limit recovery: {rel_path}")
        text = _read_text_exact(prompt_file)
        if not text.strip():
            raise ValidationError(f"source prompt is empty for usage-limit recovery: {rel_path}")
        return rel_path, text

    review_number = iteration_number - 1
    rel_path = fix_prompt_path(review_number)
    text = read_exact_fix_prompt(run_directory, review_number)
    return rel_path, text


def _read_text_exact(path: Path) -> str:
    """Read UTF-8 text without universal-newline translation."""

    return path.read_bytes().decode("utf-8")


def read_exact_fix_prompt(run_directory: Path, review_number: int) -> str:
    rel_path = fix_prompt_path(review_number)
    prompt_file = run_directory / rel_path
    if not prompt_file.is_file():
        raise ValidationError(
            f"cursor prompt missing for iteration {iteration_label(review_number + 1)}: {rel_path}"
        )
    text = _read_text_exact(prompt_file)
    if not text.strip():
        raise ValidationError(
            f"cursor prompt is empty for iteration {iteration_label(review_number + 1)}: {rel_path}"
        )
    return text


def _read_verified_continuation_envelope(
    envelope_file: Path,
    *,
    expected_sha256: str,
    rel_path: str,
) -> str:
    if not expected_sha256.strip():
        raise ValidationError("cursor recovery continuation envelope hash is missing")
    actual_hash = sha256_file(envelope_file)
    if actual_hash != expected_sha256:
        raise ValidationError(f"cursor recovery continuation envelope hash mismatch: {rel_path}")
    text = _read_text_exact(envelope_file)
    if not text.strip():
        raise ValidationError(f"cursor recovery continuation envelope is empty: {rel_path}")
    return text


def read_cursor_prompt(state: RunState, run_directory: Path, iteration_number: int) -> str:
    """Return the prompt text that should be sent to Cursor for this iteration.

    Iteration 1 uses the prepared initial prompt snapshot.
    Correction iterations send a deterministic envelope that embeds the exact
    stored Codex fix prompt byte-for-byte, and persist that envelope for audit.
    Usage-limit recovery successors reuse the stored continuation envelope for
    the incomplete recovered iteration.
    """

    recovery = state.recovery
    if (
        recovery is not None
        and recovery.recovered_checkpoint == "cursor"
        and recovery.source_iteration == iteration_number
        and recovery.continuation_envelope_path
    ):
        envelope_file = run_directory / recovery.continuation_envelope_path
        if not envelope_file.is_file():
            raise ValidationError(
                "cursor recovery continuation envelope missing: "
                f"{recovery.continuation_envelope_path}"
            )
        if not recovery.continuation_envelope_sha256:
            raise ValidationError(
                "cursor recovery continuation envelope hash is missing from lineage"
            )
        return _read_verified_continuation_envelope(
            envelope_file,
            expected_sha256=recovery.continuation_envelope_sha256,
            rel_path=recovery.continuation_envelope_path,
        )

    if iteration_number == 1:
        rel_path = cursor_prompt_path(state, iteration_number)
        prompt_file = run_directory / rel_path
        if not prompt_file.is_file():
            raise ValidationError(
                f"cursor prompt missing for iteration {iteration_label(iteration_number)}: {rel_path}"
            )
        text = _read_text_exact(prompt_file)
        if not text.strip():
            raise ValidationError(
                f"cursor prompt is empty for iteration {iteration_label(iteration_number)}: {rel_path}"
            )
        return text

    review_number = iteration_number - 1
    exact = read_exact_fix_prompt(run_directory, review_number)
    envelope = build_correction_execution_envelope(exact)
    if exact not in envelope:
        raise ValidationError("correction envelope must include the exact Codex fix prompt")
    envelope_rel = correction_execution_envelope_path(review_number)
    atomic_write_text(run_directory / envelope_rel, envelope, sensitive=True)
    return envelope


def iteration_staged_patch_rel_path(state: RunState, iteration_number: int) -> str:
    entry = find_iteration(state, iteration_number)
    if entry is None:
        raise ValidationError(
            f"iteration metadata missing for staged patch lookup: {iteration_label(iteration_number)}"
        )
    git_section = entry.get("git")
    if not isinstance(git_section, dict):
        raise ValidationError(
            f"iteration git metadata missing for staged patch lookup: {iteration_label(iteration_number)}"
        )
    patch_rel = git_section.get("staged_diff_path")
    if not isinstance(patch_rel, str):
        raise ValidationError(
            f"staged diff path missing for iteration {iteration_label(iteration_number)}"
        )
    return patch_rel


def previous_iteration_git_patch_path(state: RunState, iteration_number: int) -> str | None:
    if iteration_number <= 1:
        return None
    previous = find_iteration(state, iteration_number - 1)
    if previous is None:
        return None
    git_section = previous.get("git")
    if not isinstance(git_section, dict):
        return None
    staged_diff = git_section.get("staged_diff_path")
    return staged_diff if isinstance(staged_diff, str) else None
