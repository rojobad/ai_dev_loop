# Phase 27.3 implementation and focused release evidence

Worktree: `/home/rojobad/Projects/ai_dev_loop-phase27-execution`.
Branch: `codex/phase-27-dual-implementer`. Starting HEAD: `128b5cb` (27.2),
preceded by `062f43e` (27.1). Initial Git status was clean. This report records
independent execution in this implementation session, not inherited manual review
or scheduler acceptance. Phase 27.3 changes remain unstaged.

## Contract evidence

| Contract | Production paths | Independently exercised evidence |
| --- | --- | --- |
| C-01 | `config.py`, `cli.py`, `scheduler/application/submission.py` | `test_phase27_1_implementer_inputs.py`: provider/override validation, independent frozen settings, null/bool/conflicts. |
| C-02 | `scheduler/domain/state.py`, submitted-context schemas, protected artifacts | 27.1 binding/historical tests; new genuine pre-27.3 v1 fixture retains canonical entry hashes and materializes v4; new leaves serialize/validate v6. |
| C-03 | `scheduler/application/scheduler_preflight.py` | 27.1 actual fake executable/model/auth probes; new sequence/CLI smoke uses real bounded Git admission and actual preflight with fake CLIs. |
| C-04 | Cursor workflow, reviewer bootstrap, historical serializers | 27.1 Cursor regressions and focused 20.1/20.2/20.4 tests. New v1 prepare preserves the accepted rejection of YAML-selected Codex. |
| C-05 | `scheduler/codex_implementer_attempt_runner.py`, `codex_argv.py`, `codex_evidence.py` | 27.2 adapter/implementation tests, separate implementer/reviewer identity and exact resume; 27.3 frozen-model CLI smoke. |
| C-06 | `codex_workflow_service.py`, staging, review wrapper | 27.2 success/correction tests; both three-phase provider orders reach two authorized checkpoints and final staged output. |
| C-07 | `domain/state.py`, `codex_workflow_service.py`, capacity probe | 27.2 capacity contracts; 27.3 initial/correction waits on first and second sequence ordinals; EXHAUSTED/UNAVAILABLE retain wait, AVAILABLE continues exact role. |
| C-08 | Codex attempt intent/evidence/continuation, tick restart | 27.2 bootstrap/uncertainty tests; sequence restart keeps leaf/ordinal/reservation/partial edits and exact pending prompt/session. Mixed CRLF/Unicode correction bytes are witnessed at fake CLI stdin. |
| C-09 | `abort.py`, unsupported retry guards, sequence abort/reconciliation | 27.2 abort/recovery guards; new sequence wait abort, uncertain process termination/hold convergence and unsafe identity hold across restart until explicit abort. |
| C-10 | `domain/sequence.py`, `sequence_prepare.py`, `sequence_materializer.py`, context v6/entry/definition/manifest v2 schemas | `test_phase27_3_implementer_sequences.py`: v2 independent fields/hashes, YAML same-provider defaults with phase precedence, missing resolved fields, omission/null/conflicts, effort vocabulary, idempotence/source drift, historical v1 fixture. Prepare has no Git/agent effects. |
| C-11 | Existing tick/Codex workflow/handoff, `sequence_abort.py`, `sequence_reconcile.py` | Same new file: Cursor→Codex→Cursor and Codex→Cursor→Codex, distinct sessions per phase, two checkpoints/final staged, waits/restarts/abort. Reconciliation now carries the existing unsafe Codex reservation hold through a sequence rather than releasing it as an ordinary terminal leaf. |
| C-12 | Scheduler contracts/status/history/timeline/CLI; integration models/projections/version/store filters; process output authentication/resolution; implementer capture runner | `test_phase27_3_inspection.py`: provider and pending role/turn/reason, API 1.6/schema validation, summary privacy and byte-identical ledger after reads, correct v6 run classification, bounded authenticated stdout/stderr, hash tampering rejection, historical DTOs/capabilities. Existing integration unit/content/sequence tests and pinned consumer tests also executed. |
| C-13 | `AGENTS.md`, applicable governance/state/orchestrator/loop rules; `docs/referencia/{cli,configuracion,integration-api}.md`, `docs/operacion/{estado-artefactos,seguridad-privacidad}.md`; CLI help | Exact gpt-5.6-sol/xhigh implementer and gpt-6.1-sol/high reviewer standalone/manifest examples, role-aware waiting, process uncertainty, conservative model-agnostic probe, deferred recovery, explicit operator installation, strict MkDocs build. Planning/review skills and project YAML untouched. |

## Fake CLI smoke and isolation

`test_cli_smoke_real_admission_fake_clis_and_frozen_models` drives the public
Typer CLI for standalone submit/start/tick and sequence prepare/start/tick.
The detached process backend is fake and the tick clock is controlled; production admission, preflight,
staging, review, checkpoint and materialization run against temporary Git repos.
Fake `agent`/`codex` executables are first in PATH, models/auth are deterministic,
and HOME/XDG roots and counters are isolated. No systemd unit or real model runs.
Other new sequence tests use the same real Git admission, fake detached backend,
and deterministic tick clock/probe responses. Assertions independently count
Git commits, inspect final index/worktree, validate frozen settings/session
separation and compare exact prompt bytes.

`tests/fixtures/phase27_3_historical/` was captured once using actual accepted
`128b5cb` production preparation code, extracted to `/tmp`, with non-running fake
executables and isolated HOME/XDG roots. Original bytes/absolute historical
metadata were retained. Its provenance and original canonical entry hashes are
recorded; existing historical fixtures were not rewritten.

## Exact validation

All pytest commands use `TMPDIR=/tmp TMP=/tmp TEMP=/tmp` and no real model/backend.

Prerequisite selection (executed before dependent implementation):

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler/test_phase27_1_implementer_inputs.py tests/unit/scheduler/test_phase27_2_codex_implementer.py tests/unit/scheduler/test_phase27_2_codex_implementer_adapter.py tests/unit/scheduler/test_phase27_2_implementer_capacity.py
```

Result: **74 passed in 122.04s**. This independently verifies the accepted
prerequisites; the previously reported independent manual review is not counted.

Mandatory new contracts:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/integration/test_phase27_3_implementer_sequences.py tests/integration/test_phase27_3_inspection.py
```

Result: **38 passed in 139.34s**. This run includes public standalone/sequence
CLI smoke and real bounded Git admission. It preceded the final v1 compatibility
correction; the affected prepare/frozen/version tests and the additional explicit
Cursor representation test passed in the final selection below.

Affected regressions:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler/test_phase20_1_sequence_prepare.py tests/unit/scheduler/test_phase20_1_sequence_corrections.py tests/unit/scheduler/test_phase20_2_sequence_start.py tests/unit/scheduler/test_phase20_4_sequence_lifecycle.py tests/unit/scheduler/test_phase20_4_sequence_abort.py tests/unit/scheduler/test_phase20_4_sequence_corrections.py tests/unit/scheduler/test_phase19_codex_capacity.py tests/integration/test_phase20_1_sequence_prepare.py tests/integration/test_phase20_2_sequence_start.py tests/integration/test_phase20_3_sequence_handoff.py tests/integration/test_phase20_4_sequence_end_to_end.py tests/unit/integration_api tests/integration/test_phase21_1_integration_contract.py tests/integration/test_phase21_3_sequence_inspection.py tests/integration/test_phase21_5_process_output.py tests/integration/test_phase21_6_capacity.py
```

Result: **280 passed, 3 skipped, 1 warning in 228.40s**. The warning is an existing
synthetic test constructing a dict instead of typed `SequenceRunBinding`; the
three fixture skips are reported as skips, not successful coverage. This broad
focused run preceded the final sequence reservation-hold correction; affected
lifecycle/abort/correction checks were rerun separately:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler/test_phase20_4_sequence_lifecycle.py tests/unit/scheduler/test_phase20_4_sequence_abort.py tests/unit/scheduler/test_phase20_4_sequence_corrections.py tests/integration/test_phase21_6_integration_acceptance.py -k 'sequence or c05'
```

Result: **23 passed, 2 skipped, 7 deselected, 1 warning in 10.28s** (same synthetic
warning). This includes pinned historical API consumers, not a Phase 26 dashboard.

During development, test setup assumptions (missing log means no launch, an
ingested attempt is looked up by its exact ID, empty historical list, Rich help
line wrapping and bootstrap-only empty DB) were corrected. One missing hold-helper
import was corrected. The new hold test passed afterward. A prerequisite rerun
identified the v1 YAML guard regression; production rejection was restored and
its failed/affected selection was rerun, as recorded below. Earlier failing
runs are not claimed as successful final checks.

Final v1 compatibility/prepare follow-up:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler/test_phase27_1_implementer_inputs.py tests/unit/scheduler/test_phase20_1_sequence_prepare.py tests/unit/scheduler/test_phase20_1_sequence_corrections.py tests/integration/test_phase27_3_implementer_sequences.py -k 'v1 or prepare or manifest or frozen'
```

Result: **40 passed, 53 deselected in 35.35s**. Before correcting the guard,
rerunning the 74-test prerequisite selection gave **73 passed, 1 failed in
141.08s**; its sole failed test was
`test_sequence_v1_rejects_codex_and_keeps_omitted_cursor`. The final selection
above reruns it and all affected preparation paths. V1 also retains explicit
Cursor YAML in its canonical effective artifact, while omitted YAML stays omitted.
The genuine accepted fixture retains its original entry hashes. No existing test
expectations were weakened to remove that prerequisite failure.

Required static/docs commands:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
git diff --check
```

Results: **all exit 0**. Ruff format reports **346 files already formatted**;
Ruff lint reports **All checks passed**; mypy reports **no issues in 181 source
files**. Strict MkDocs builds successfully (the Material package prints its
upstream informational banner); `git diff --check` is clean.

## Remaining gates and limits

- Mandatory C-10 through C-13 implementation and automated coverage: complete.
  No unresolved prerequisite or safety decision remains.
- Separate pending gate: repository-wide pytest, package pipeline/build and
  explicit operator installation/update validation. No package installation,
  timer/linger change or live-model acceptance was performed here. The installed
  scheduler does not hot-load worktree changes.
- Codex implementer generic failure/timeout manual retry, automatic timeout
  recovery, successor recovery parity and model/provider switching remain deferred.
  Automatic capacity continuation is implemented. Shared capacity probing remains
  model-agnostic and conservatively retains relevant windows.
- No blocked live sequence/run was controlled; no live ledger, protected artifact
  or scheduler-owned session was changed. No stage, commit, branch switch, push,
  package install, skill change, project YAML change or Phase 24/25/26 work.
- Manual staging and independent review remain the user's next steps. Git commits
  and this report do not update scheduler acceptance.
