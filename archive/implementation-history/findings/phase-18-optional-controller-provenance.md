# Phase 18 Findings: Operator-Owned Runs, Iteration Timeline, and Precise Timer Cadence

## Scope delivered

- Optional controller session A provenance for scheduler submit (validated UUID when
  supplied; `null` when omitted).
- `scheduler start <run-id>` without controller flag; queued safe-next-action updated.
- Controller status by exact `--run-id` plus `--repo-path` without A; legacy A-based
  discovery preserved; validation error when both selectors are absent.
- Submission idempotency excludes optional A provenance from frozen-work identity.
- Historical A-bearing idempotency replay via compatibility lookup (current + legacy keys
  and normalized identity scan) without row migration.
- `review_iterations_completed` and `max_review_iterations` on status/list/controller
  summaries, including blocked runs whose snapshot lacks a Codex checkpoint (ledger-backed).
- New `scheduler timeline <run-id>` bounded read-only projection from central attempt
  rows with SQL-derived `phase_attempt` ordinals (no unbounded Python fetch).
- Packaged timer adds `AccuracySec=1s` alongside existing `OnBootSec=30` and
  `OnUnitActiveSec=30`; section-aware exact directive validation.
- Package-owned handoff/controller skill assets and CLI/docs updated for no-A workflow.

## Non-actions (as required)

- No live runs, user-global installs, real timers, target repositories, or real
  Cursor/Codex/model activity.
- No database migration, historical row rewrite, or installed systemd unit mutation.
- No changes to B bootstrap/binding/resume, tick capacity, abort, recovery, or global
  integration mechanics.

## Validation results

| Gate | Result |
|------|--------|
| `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q` | **618 passed**, 3 warnings (pre-existing pydantic serializer warnings in codex runner tests) |
| `uv run ruff format --check .` | **pass** |
| `uv run ruff check .` | **pass** |
| `uv run mypy src` | **pass** (97 source files) |
| `uv build` | **pass** (`ai_dev_loop-0.1.0` sdist + wheel) |
| `uv run mkdocs build --strict` | **pass** |
| `git diff --check` | **pass** |

Focused Phase 18 regressions:

- `tests/integration/test_phase18_optional_controller_provenance.py` (11 tests)
- `tests/unit/scheduler/test_phase18_timeline_and_provenance.py` (4 tests)
- `tests/unit/scheduler/test_phase18_corrections.py` (7 tests)
- `tests/unit/test_phase18_doc_examples.py` (9 tests)
- Updated scheduler start/submit/controller/timer tests across Phase 17 suites

## Codex correction turn (Phase 18 review)

| Finding | Fix |
|---------|-----|
| Historical A-bearing idempotency replay | `find_existing_submission()` tries current + legacy keys; identity scan matches stored keys derived from persisted context so A-free retries reuse active/terminal historical runs. |
| `review_iterations_completed` on blocked runs | `REVIEW_COMPLETION_EVENT_KINDS` ledger count feeds `review_budget_from_state()` when `codex` checkpoint is absent; status/list/controller projections aligned. |
| Timer directive validation | Section-aware `[Timer]` parsing requires exactly one `OnBootSec=30`, `OnUnitActiveSec=30`, and `AccuracySec=1s`; rejects commented/malformed/duplicate values. |
| Timeline bounding | `ROW_NUMBER()` in SQL for `phase_attempt`; query uses `limit + 1` overflow probe only; hard max 200 enforced. |
| Documentation | Removed obsolete `scheduler start --controller-session-id` examples and mandatory-A claims; doc example guards added. |

## Codex correction turn 2 (timeline ordering + timer parsing)

| Finding | Fix |
|---------|-----|
| Timeline ordering mismatch | `list_attempt_timeline_rows()` uses the same chronological key for retry ordinals and page ordering: `COALESCE(launch_requested_at, created_at)`, `created_at`, `attempt_id`. `created_at` is used internally only; public projection unchanged. |
| Timer whitespace duplicates | `_parse_directive_assignment()` splits on the first `=`, trims key/value, and detects conflicting duplicates such as `AccuracySec = 60s` alongside the packaged value. |

## Schema and read compatibility

- `ControllerBinding.controller_session_id`, `RunAuthorizedEvent.controller_session_id`,
  and `authorized_controller_session_id` accept `null` in typed models.
- Historical non-null A-bearing snapshots/events remain valid; redacted prefixes unchanged.
- JSON schemas `scheduler-submitted-run-context-v{1,2,3}.json` allow null controller ID.
- `submission_identity_payload()` strips controller provenance from idempotency material;
  `legacy_submission_identity_payload()` preserves historical key material.

## Timeline privacy and bounding

- Default limit 50, hard max 200, `truncated` flag, deterministic `oldest`/`newest` order
  using `COALESCE(launch_requested_at, created_at)` with `created_at` and `attempt_id`
  tie-breakers (creation time not exposed publicly).
- Public entries: iteration, phase (`cursor`/`codex`), `phase_attempt`, safe status,
  `launch_requested_at`, `completed_at`, `observed_duration_seconds` (null unless both
  timestamps exist).
- No attempt IDs, dispatch IDs, unit/backend identities, artifacts, prompts, reviews, or
  full session IDs in JSON/text output.

## Timer template evidence

- `src/ai_dev_loop/scheduler/assets/ai-dev-loop-scheduler-tick.timer` contains
  `OnBootSec=30`, `OnUnitActiveSec=30`, `AccuracySec=1s`.
- `validate_packaged_assets()` parses the `[Timer]` section, trims whitespace around
  `=` assignments, and requires each directive exactly once with exact values; retains
  Phase 17.10 `/usr/bin/env` + constrained `PATH` service checks.

## Package skill asset impact

- `ai-dev-loop-handoff`: submit no longer requires `--controller-session-id`; optional
  provenance documented.
- `ai-dev-loop-controller`: start/status examples use run-id first; legacy A discovery
  documented.

## Residual risks / tradeoffs

- Removing the start-time controller mismatch guard is intentional: any local process with
  scheduler state access and an exact run ID could already invoke other local scheduler
  commands. This is not a multi-user authorization model.
- Historical idempotency replay scans same-worktree runs when keys differ; cost is bounded
  by worktree reservation (one active run per repo) and is only used after key misses.
- Timeline durations are observed durable intervals (launch request to completion), not
  child CPU/runtime; active attempts never show a changing now-minus-start duration.
- `AccuracySec=1s` narrows systemd coalescing but cannot guarantee exact 30-second ticks
  under suspend, load, or a still-running oneshot service.
- Existing installed timer units keep prior content until the operator deliberately runs
  `scheduler timer install` after package upgrade.

## OpenQuestions

None.
