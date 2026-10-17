# Phase 25 replanning after implemented Phases 26–28

Operator request: review the unexecuted SDK plans against the newer code,
update/expand as needed, commit, prepare a fresh sequence with Codex GPT-5.6
XHIGH implementation and unchanged reviewers, then start it. Recorded on
2026-10-16, America/La_Paz.

Source baseline: clean `main`,
`558b7998118051cdee2a665a6ec28507e754e604`. No Phase 25 implementation is claimed
by this planning evidence. Phase numbers here follow repository source:
**26 Dashboard, 27 dual implementers/recovery, 28 Dashboard refinements/names**.
Phase 24 remains excluded.

## Evidence and changes

| Accepted behavior inspected | Consequence for the updated plans |
| --- | --- |
| Config `ImplementerSection`, provider-specific precedence/serialization; context v5/v6 and separate Codex worker/evidence; preflight's Codex branch | SDK configuration/admission guards affect Cursor only. Codex keeps independent model/effort/timeout/session and requires no Cursor executable, SDK extra, bridge, catalog, credential or store. Preserve valid v1 Codex config and add provider-aware v2 config without merging CLI defaults into SDK fields. |
| `implementer_timeout_retry.py`, `implementer_failure_recovery.py`, `sequence_implementer_recovery.py`, accepted 27.3/27.7 evidence | Preserve Codex timeout/generic-failure recovery and pending-role capacity. Add SDK handling to Cursor delegation from `implementer-retry` alongside compatible `cursor-retry`, with separate lineage and reservation/abort contracts. |
| Manifest v3, context versions 1–6, frozen definitions/entries v1/v2; schema-17 presentation tables and copy-once names | Execution manifest uses installed v3 and explicit bounded names. Planned SDK manifest v4 extends named v3; new SDK context/entry versions cannot reuse Phase 27 numbers. Keep historical hashes/replay and supported Codex-only definitions intact. |
| Codex implementer JSONL and reviewer JSONL are separate authenticated captures | Usage must cover both roles, initial/correction/review stages and both recovery ancestries. Role comes from attempt/effect/invocation evidence. SDK native-result replay in a reconciliation attempt cannot create duplicate consumption. |
| Dashboard validated SQLite reader, effect classification, shared application queries, API 1.11, strict browser consumers and Phase 28 refinements | New mandatory U-05 tests preserve Dashboard counts/roles, historical reads, names, exact-ID links, project/time-zone filters, bounded paging and privacy. No new token chart/UI is added. Retain packaged frontend build provenance/installed acceptance. |
| Installed scheduler is outside source, currently supports named v3 preparation | Keep its frozen v1 config and runtime independent of source changes, including final v2 YAML. Cursor CLI retirement must never disable Codex-only old/new execution or recovery. A mixed historical definition containing CLI-Cursor work must reject before starting, rather than executing its early Codex leaves first. |

Updated the overview, five execution plans and five tracked prompts. Added
`archive/implementation-history/plans/phase-25-replanned-sequence.yaml`.
The five subphases remain independently reviewable; contract coverage grows from
20 to **21** IDs (F-01–F-04, W-01–W-04, R-01–R-04, U-01–U-05, G-01–G-04).
The POC archive and all original planning/acceptance findings remain unchanged.

## Frozen execution choices

Every phase: Codex implementer `gpt-5.6-sol`, `xhigh`, 90 minutes, seven completed
reviews maximum. Reviewers remain the primary recommendation previously given:

| Phase | Reviewer | Effort |
| --- | --- | --- |
| 25.1 | gpt-6.1-sol | high |
| 25.2 | gpt-6.1-sol | xhigh |
| 25.3 | gpt-6-astra | high |
| 25.4 | gpt-6.1-sol | high |
| 25.5 | gpt-6-astra | high |

The installed Codex CLI **0.161.0** public catalog probes classified all four
distinct model/effort pairs as `compatible`, with `debug_models` evidence. No
inference was invoked by those probes. Official Codex model documentation also
still lists GPT-5.6 Sol during the rollout; local availability was checked rather
than substituting the requested model.

## Executed validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase27_1_implementer_inputs.py tests/unit/dashboard/test_metrics.py tests/unit/scheduler/test_phase28_short_names.py
```

Result: **59 passed in 12.08s**, exit 0. These verify accepted provider/config,
role/activity and presentation prerequisites, not the unimplemented SDK feature.

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mkdocs build --strict --site-dir /tmp/ai-dev-loop-phase25-replanning-site
git diff --check
```

- Ruff lint: **passed**, exit 0.
- Ruff format: **failed**, exit 1, five pre-existing unchanged files:
  `src/ai_dev_loop/cli.py`, `src/ai_dev_loop/dashboard/domain/filters.py`,
  `src/ai_dev_loop/scheduler/infrastructure/sqlite_store.py`,
  `tests/integration/test_phase28_short_names.py`,
  `tests/unit/scheduler/test_phase20_1_sequence_prepare.py`.
  384 files already formatted. No unrelated reformatting was performed.
- Strict MkDocs: **passed**, exit 0, 0.94s. Existing informational notices about
  Material/MkDocs and unlisted incident pages remain. Archive links were checked
  independently because the plans are outside the documentation nav.
- Whitespace check: **passed**, exit 0.
- Static artifact check: **passed**. Manifest v3 validates with the current
  domain parser; all five implementer/reviewer pairs and short names match;
  six plans retain all twelve required sections; five prompts match their
  plans; all 21 IDs have acceptance rows; 38 distinct existing regression paths
  resolve; all 25 relative Markdown links resolve. Each of the **71** preserved
  POC files still matches its recorded SHA-256 and byte length. The installed
  parser is checked again before preparation; preparation remains the authority
  for the frozen effective configuration.

Read-only installed checks: scheduler package resolves under the `uv tool`
environment, not this source tree; installed manifest parser supports v3 and
short names. Scheduler timer reports installed/owned/matching, enabled and active.
The scheduler list had no active run in this checkout. No timer, service,
credential, integration or package configuration was changed.

## Scope and remaining work

This commit changes planning artifacts only. New SDK contract tests, isolated
Python 3.11 SDK health, normalization evidence, frontend/installed-package gates
and repository-wide pytest belong to execution of the linked plans. They are
not reported as passed here. The original temporary POC virtualenv no longer
exists; its 27 tests were not rerun in this replanning turn. Preserved hashes
remain valid, and the original evidence is explicitly historical.

After this planning commit, prepare/start the new sequence using the stable
installed CLI and capture its exact receipts. Its authorization includes local
accepted non-final checkpoints. Final changes remain staged; installation,
manual control-plane acceptance, real SDK smoke, push, PR and merge are separate
operator work. Do not hot-install the changing source into the active driver or
rewrite any previously prepared sequence.
