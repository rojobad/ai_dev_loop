# Phase 17.5 — Codex review and bounded scheduler loop

## Goal

Complete the local A/B scheduler workflow: a staged run launches an exact-session
Codex review attempt, ingests only the structured result, then either reaches a
terminal outcome or schedules the exact Cursor correction turn. Every agent
remains externally owned by systemd; every scheduler invocation remains a short
tick.

## Non-Goals

- Do not add GitHub/PR-review behavior, commits, pushes, or external writes.
- Do not implement cutover deletion, systemd enablement, or public abort beyond
  the already-existing low-level attempt cancellation capability.
- Do not use Markdown or a new Codex session to decide workflow outcomes.

## Scope

- Add durable Codex review launch/observe/ingestion effects to the scheduler.
- Preserve exact reviewer session/runtime/model/reasoning and schema-valid review
  decisions.
- Add bounded finding -> Cursor correction -> staging -> review iterations and
  terminal result states to the scheduler reducer.

## Out of Scope

- Legacy command deletion, XDG cleanup, human/manual service activation,
  automatic unknown-failure recovery, and PR-review v2 migration.

## Required Context

Read the master and Phases 17.1–17.4, all Cursor rules,
`runners/codex.py`, `review_result.py`, `response_schema.py`,
`workflow_engine.py`, `iterations.py`, `local_review_loop.py`,
`tests/integration/test_start_codex_review.py`, and bounded-loop/resume tests.

## Cursor Rules And Skills

All `.cursor/rules/*.mdc` apply. In particular, the configured target review
skill, exact session ID, command ordering, structured result schema, review
privacy, and immutable Codex-authored fix prompt are mandatory. Do not invoke
the staged-review skill during implementation.

## Architecture Guardrails

- Codex is a durable attempt, not a blocking tick call. It uses the exact frozen
  session/runtime and no `--last`; stdin/wrapper command ordering stays identical
  to current tested behavior.
- Only a validated `codex-review-result-v1` result drives findings, tests status,
  residual risk, fix prompt, and terminalization. Markdown is audit content only.
- The exact stored fix prompt is the only source of correction content. The
  scheduler may create a deterministic envelope but cannot rewrite findings.
- Preserve review budgets: review 01 follows initial Cursor; review N findings
  schedule correction N+1 only below max; at limit final state is
  `max_iterations_reached` with no extra Cursor attempt.
- Every review/turn/staging boundary is independently fenced and restartable;
  late attempt outcomes cannot overwrite a newer state.

## Implementation Plan

1. Extract/build a scheduler Codex effect adapter around existing safe command,
   artifact, schema parsing, and error-redaction primitives. Route execution via
   the Phase 17.3 systemd backend and result ingestion in a later tick.
2. Add state/events/effects for review launch, review-result ingestion,
   `waiting_for_cursor_fix`, correction turn scheduling, no-findings completion,
   residual-risk completion, and max-iterations terminalization.
3. Reuse the Phase 17.4 Cursor/staging effects for corrections with the exact
   same persisted chat ID. Reapply correction preconditions and staged patch
   equality before every correction.
4. Make status/controller read projection report review-active, waiting fix,
   active correction, terminal outcome, and safe next action without sensitive
   content.
5. Update active docs only for behavior now proved by fake-backed tests; keep
   cutover and service-install claims for later phases.

## Testing Criteria

- Fake-Codex integration verifies exact session/model/reasoning argv, no
  `--last`, wrapper content, stdout/stderr privacy, schema success/failure,
  timeout, and restart before/after result ingestion.
- End-to-end fake scheduler traces: no findings; findings then one correction;
  repeated findings to max; residual tests status; missing/empty fix prompt;
  staged drift before correction; usage limit during correction; late stale
  Cursor/Codex completion fence.
- Retain independent checks that all ticks exit while a fake agent remains active.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/test_codex_runner.py tests/unit/test_response_schema.py tests/unit/test_iterations_and_resume.py tests/integration/test_start_codex_review.py tests/integration/test_phase5_loop_resume.py tests/integration/test_phase17_5_*.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
git diff --check
```

## Risks Or Recovery Notes

The most serious regression is accidentally treating a review report or a partial
agent exit as a decision. Reject invalid output durably and preserve artifacts.
Never create a second reviewer session or Cursor chat to make a recovery easier.

## OpenQuestions

None. The master fixes the A/B-only workflow, exact agent identity, review limit,
and no automatic model fallback policy.
