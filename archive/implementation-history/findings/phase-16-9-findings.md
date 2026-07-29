# Phase 16.9 Findings — PR-review v2 cutover and legacy test cleanup

Date: 2026-07-28

## Summary

Phase 16.9 made the SQLite v2 engine the only supported public `ai_dev_loop
pr-review` workflow. Legacy v1 modules, `RunState.github_pr_review`, the
temporary `pr-review-v2` CLI namespace, and v1-only subcommands were removed.
No migration or read adapter was added. Phase 16.8 deferred resilience items
were not implemented.

## Public CLI cutover

| Surface | After Phase 16.9 |
|---|---|
| Public namespace | `ai_dev_loop pr-review` only |
| Commands | `create`, `prepare`, `start`, `status`, `history`, `resume`, `abort` |
| Retired | `pr-review-v2`, `continue`, `recover`, `set-cursor-model` |
| Config key (internal) | `pr_review_v2` unchanged |
| XDG dirname (internal) | `pr-review-v2/` unchanged |

`start` remains the sole effects gate. `create` / `prepare` stay read-only.

## Deleted production modules

- `src/ai_dev_loop/commands/pr_review.py`
- `src/ai_dev_loop/commands/pr_review_independent.py`
- `src/ai_dev_loop/commands/pr_review_recover.py`
- `src/ai_dev_loop/pr_review_worker.py`
- `src/ai_dev_loop/legacy_pr_review_local_adapter.py`
- `src/ai_dev_loop/external_adjudication.py`
- `src/ai_dev_loop/github_pr_review_result.py`
- `src/ai_dev_loop/runners/codex_github.py`
- `src/ai_dev_loop/schemas/github-pr-review-result-v1.json`
- `src/ai_dev_loop/schemas/github-publication-text-v1.json`

Extracted: `src/ai_dev_loop/commands/github_doctor.py` (read-only `gh`/SSH checks).

## State/schema changes

- Removed `RunState.github_pr_review` and related typed models from `state.py`.
- Removed Phase 15 PR-review-only `RunStatus` values from schema and transitions.
- Retained local-loop `recovery` checkpoints in schema for A/B `recover` (including
  historical reason codes readable but not wired to public `pr-review recover`).

## Test reduction evidence

| Metric | Before | After | Delta |
|---|---:|---:|---:|
| Collected tests | 1917 | 1678 | −239 |

Deleted legacy-only integration tests (Phase 15 PR-review flows and recoveries):

- `tests/integration/test_phase15_pr_review.py`
- `tests/integration/test_phase15_5_independent_pr_review.py`
- `tests/integration/test_phase15_7_adjudication_recovery.py`
- `tests/integration/test_phase15_8_reviewing_recovery.py`
- `tests/integration/test_phase15_9_worker_continuity.py`
- `tests/integration/test_phase15_10_legacy_cycle_freeze.py`
- `tests/integration/test_phase15_11_nested_legacy_cycle_freeze.py`
- `tests/integration/test_phase15_12_external_feedback_recovery.py`
- `tests/integration/test_phase15_13_publication_pre_commit_recovery.py`
- `tests/integration/test_phase15_15_publication_patch_fingerprint.py`
- `tests/integration/test_phase15_16_external_feedback_clean_baseline.py`
- `tests/integration/test_phase15_17_durable_external_adjudication_flow.py`

Deleted legacy-only unit tests:

- `tests/unit/test_codex_github_schema_rejection.py`
- `tests/unit/test_github_pr_review_result.py`
- `tests/unit/test_legacy_external_cycle_freeze.py`
- `tests/unit/test_phase15_12_external_feedback_iteration.py`
- `tests/unit/test_phase15_13_ssh_agent_interrupt.py`
- `tests/unit/test_phase15_14_effective_ssh_agent_preflight.py`
- `tests/unit/test_phase15_16_external_feedback_preflight.py`
- `tests/unit/test_phase15_17_durable_external_adjudication.py`
- `tests/unit/test_pr_review_worker_liveness.py`

Added: `tests/unit/test_pr_review_cli.py` (public v2 command set, absent
`pr-review-v2`, prepare read-only, start as effects gate).

Retained v2 contract coverage: reducer/SQLite/control/supervisor/integration
Phase 16.1 A/B barrier, Phase 16.8 Gate A happy-path helpers, privacy-safe
status/history, write reconciliation, exact session/chat continuity.

## Contract-to-test matrix (retained)

| Contract | Primary tests |
|---|---|
| Public v2 CLI | `tests/unit/test_pr_review_cli.py` |
| Explicit `start` gate | `test_pr_review_cli.py`, Phase 16.7/16.8 control tests |
| SQLite authority | `tests/unit/pr_review_v2/test_durable_*`, integration Phase 16.4+ |
| Redacted status/history | `tests/unit/pr_review_v2/test_durable_status.py` |
| Phase 16.1 A/B barrier | `tests/integration/test_phase16_1_ab_regression_barrier.py` |
| No `--last` / exact Codex session | v2 codex runner + integration boundaries |
| Non-force Git | v2 git transport + publish tests |
| Write reconciliation | Phase 16.6+ unit/integration tests |

## Validation commands

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q tests/unit/test_pr_review_cli.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q tests/integration/test_phase16_1_ab_regression_barrier.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest --collect-only -q   # 1678
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q                   # 1678 passed
uv run ruff format --check .
uv run ruff check .
uv run mypy src                                                     # success
uv build                                                            # success
uv run mkdocs build --strict                                        # success
```

## Documentation

Updated README and MkDocs pages for public `pr-review` v2 semantics, compatibility
boundary, and removal of v1 recovery/continue docs. Historical phase traceability
retains Phase 15 narrative with Phase 16.9 supersession note.

## Compatibility boundary (intentional)

- Historical v1 runs with `github_pr_review` in `state.json` are not readable.
- No `pr-review recover` public command.
- Internal schema filenames and XDG paths may still contain `pr-review-v2` prefix.

## Residual risks

- Phase 16.8 deferred crash/recovery matrix remains open (`PHASE_16_8_DEFERRED_ISSUES.md`).
- Live acceptance evidence from Gate B is unchanged; automated Gate A coverage does
  not replace it.
- Root `pr-review-v2-restructure-context.md` remains reference material, not product docs.

## OpenQuestions

None.
