# Phase 26 Codex replacement planning and launch preparation

Date: 2026-10-15. Accepted source baseline:
`466b8e09724430cebddbdeca6f1961b2d6243531`, merged Phase 27.1–27.7.

## Current authorization and source facts

The operator requested replacing the old sequence, selecting Codex GPT 5.6 xhigh
as implementer, preserving reviewers, refreshing plans against current code,
committing all pending work, creating a Phase 26 branch and launching there.
No push, PR, provider switch inside a run, or Hub worktree change is included.

Native sequence status returned old `ai-dev-loop-seq-c062d0252688` already
`aborted`, `started_at=null`, zero materialized runs and seven cancelled entries.
No redundant cancellation or unrelated-run abort was required. No old run can
be resumed by editing the source manifest.

The stable installed driver resolves outside the editable checkout and reports
API 1.8 / SQLite schema 16. Current source supports manifest v2, provider-aware
frozen bindings, independent implementer/reviewer sessions, role-aware capacity
and retry metadata, component codex_implementer, authenticated process output and
implementer_retry sequence lineage. Phase 24/25 remain unexecuted planning.
The Phase 27.7 findings' historical full-suite/build limits are retained; this
refresh does not claim they were rerun or resolved by documentation.

## Concrete changes

- All seven entries now use native manifest v2 with implementer provider/command
  codex, canonical model gpt-5.6-sol, xhigh and 90-minute timeout. Phase cursor
  overrides and cursor_timeout_minutes are removed to avoid provider conflicts.
- Reviewers remain gpt-6.1-sol xhigh in 26.1/26.2/26.7, high in 26.3–26.6,
  90-minute timeout, seven iterations. Root ai_dev_loop.yaml stays unchanged.
- Source inventory and all plans/prompts now use accepted Phase 27 code, API 1.8,
  SQLite 16 and planned additive dashboard minor 1.9. The original source
  observation is preserved separately with its historical hashes.
- Started/process-time metrics include Codex implementer bootstrap/resume effects;
  reviewer rounds include review effects only. Map/timeline/inspection preserve
  provider, semantic role, retry/wait metadata and authoritative replacement IDs.
- Codex implementer output uses the existing role-authenticated read service;
  reviewer-content never targets an implementer attempt. Initial prompt is run
  context; no exact private per-attempt implementer prompt is invented.
- The planning validator now verifies these provider/version choices. The old
  planning report is explicitly historical. Dashboard product scope remains the
  same seven subphases and final manual walkthrough.

## Executed validation

| Command or production check | Result |
| --- | --- |
| Installed `probe_codex_model_compatibility('codex', required_model='gpt-5.6-sol', required_reasoning_effort='xhigh')` | Compatible from `debug_models`: model and xhigh reasoning present. This is a metadata probe, not a model implementation turn. |
| `.venv/bin/python archive/implementation-history/plans/validate_phase_26_planning.py` | Exit 0. Native manifest v2 accepts seven phases; explicit implementer and unchanged reviewers, 24 contracts, all plan/prompt paths, nine rule paths, 15 current source hashes and seven frozen visual hashes pass. |
| `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run --no-sync python -m pytest -q tests/unit/scheduler/test_phase27_1_implementer_inputs.py tests/integration/test_phase27_3_inspection.py` | Exit 0: 43 passed in 24.79s. Fake-backed production input/binding and role-aware read-only inspection; no live model turns. |
| `.venv/bin/python -m ruff check archive/implementation-history/plans/validate_phase_26_planning.py` | Exit 0. Formatting check reports unchanged; lint passes. |
| `uv run --no-sync mkdocs build --strict --site-dir /tmp/ai-dev-loop-phase26-codex-refresh-site` | Exit 0, build 0.84s. Existing incident pages outside nav remain informational and are preserved. |
| `git diff --check` and planning validator new-file whitespace checks | Passed. |

## Commit and execution boundary

Pending incident documents are preserved in their own commit under the operator's
explicit request to leave all work committed. Refreshed Phase 26 governance/
plans/prompts/manifest/evidence are committed separately before branch creation.
The replacement must be prepared after creating codex/phase-26-local-dashboard
from that clean committed baseline, then started with the native command.
Its exact assigned sequence ID/state and commits are reported to the operator
from native results; they are not invented in this pre-launch evidence file.

No Dashboard runtime implementation, frontend build, installed wheel acceptance
or final human acceptance is claimed here. Those belong to the launched plans.
Repository-wide pytest belongs once to 26.7, not this planning refresh.
