# Phase 25.7 A — SDK SQLite implementation finding

Date: 2026-10-20
Scope: delivery A only (`C-01`, `C-02`, `C-03`)

## Outcome: completed candidate for independent review

Delivery A now uses the pinned public `cursor-sdk==1.0.37` native SQLite store
without modifying the vendor bridge. Before the original bridge entry point
runs, an application-owned packaged initializer calls public
`SqliteLocalAgentStore.open({workspaceRef, stateRoot})` with the authenticated
private owner root and installs that store through public `configureCursorSdk`.
The Python side starts the original unmodified bridge with public
`CursorClient.launch_bridge(command=...)`.

This route supersedes the source finding's assertion that the pinned SDK has no
public confinement route. The default Python bridge route still selects its
default store, and remains covered as an upstream control; the production route
does not use it.

Admission was verified before editing:

- HEAD: `3d1a7a3074e0edfe8ef2f0aca16d4d12d77c3250`.
- Source patch SHA-256:
  `d83d5362b45f58fba638c522484e5022baa7e92a92323f02322b02d64183b0a3`.
- The source patch passed `git apply --check` and was applied mechanically,
  without `--index`, reproducing the complete existing 41-file candidate before
  the native initializer work.

The candidate preserves SDK 1.0.37, exact agent/run identities, lifecycle,
historical JSONL forms, and the inherited F-02/F-03/F-04 corrections. It adds no
vendor patch, custom storage engine, global `HOME` workaround, SDK upgrade,
fallback, replacement conversation, retention behavior, or delivery B/C work.

## Contract status

### C-01 — native SQLite persistence and owner confinement: satisfied

- Fresh SDK standalone submissions freeze submitted context v9 with SDK binding
  v2, `store_backend="sqlite"`, and `native_store_layout_version=1`. Fresh
  manifest-v4 Cursor sequence entries freeze definition/entry v4 and materialize
  submitted context v10 with the same binding.
- The owner key selects the private application root under
  `$XDG_STATE_HOME/ai_dev_loop/cursor-sdk/<validated-key>`. The existing path
  guards preserve containment, ownership, permissions, non-symlink requirements,
  and repository/global-store separation.
- The installed initializer opens the native store at that exact root before the
  original bridge module starts. The bridge is launched with the pinned bundled
  Node executable, `--import <installed initializer>`, and the original bundled
  `cursor-sdk-bridge.js` entry point.
- The positive production-launch regression seeds owner A, owner B, and the
  upstream default store for one workspace; then launches A, B, and A after
  shutdown/reopen. Each launch lists/gets only its expected owner agent, rejects
  both forbidden agents with `AgentNotFoundError`, and leaves the default store
  byte-identical.
- Normal `CursorClient` close terminates each owned bridge; the initializer
  disposes its process-owned store on normal event-loop drain. Abrupt termination
  retains the existing owned-process/OS cleanup behavior.

### C-02 — frozen propagation and exact identity: satisfied

- SDK storage identity is carried by context v9/v10, evidence v3, workflow
  checkpoint/runtime identity v2, and sequence definition/entry v4. Canonical
  writers and validators reject backend/layout changes.
- `CursorSdkAdapter.open` resolves the installed initializer plus the pinned
  bundled runtime only for SQLite bindings, and passes the resulting command to
  public `CursorClient.launch_bridge`. Historical JSONL bindings retain their
  original launch shape with no custom command.
- The same frozen binding flows through initial creation, correction/resume,
  restart reconciliation, recovery, sequence successor materialization, and
  abort-related SDK reconciliation. Existing agent and run IDs are resumed; no
  replacement conversation is created.
- Production workflow fakes assert the four-part launch command while preserving
  the existing one-session and exact-identity assertions. Two unrelated owners
  of the same workspace and fresh-vs-historical sequence behavior are covered.

### C-03 — historical and absent native state behavior: satisfied

- Submitted contexts v7/v8 and sequence definition/entry v3 remain readable with
  their historical SDK binding v1/JSONL interpretation. Fresh preparation opts
  into SQLite without mutating historical definitions or fixture bytes.
- A bound SQLite conversation requires existing valid native state. Absent,
  zero-byte, truncated, corrupt, or schema-empty state fails before bridge/model
  work and never creates replacement state.
- If public resume cannot find the exact bound agent, the outcome is
  `cursor_sdk_native_unavailable`; the reservation is retained and no prompt is
  sent. Corrupt native state also prevents bridge launch/agent operations and is
  left unchanged.

## F-01 — public production initializer implemented

`src/ai_dev_loop/integrations/cursor_sdk/native_store_initializer.mjs` is the
application-owned preload. It derives the public JavaScript SDK modules relative
to the original installed bridge entry point, imports public
`configureCursorSdk` and `SqliteLocalAgentStore`, opens the requested store, and
configures it before bridge startup.

`resolve_cursor_sdk_native_bridge_command` verifies the installed distribution
is exactly 1.0.37 and requires the packaged initializer, bundled executable Node
runtime, and original bridge entry point. Missing initializer/runtime failures
are classified as `INVALID_LOCAL_PATH` at `open`; unit tests prove no launch,
agent creation, or send occurs. Native-store open failure is separately proved
with the real bridge and corrupt state.

The isolated-wheel test builds the project wheel, installs it only into a
temporary `--target`, runs outside the checkout, proves both the Python package
and initializer resolve from that installation, and performs keyless public
list/get through the production launch command. This is focused packaging proof,
not a workstation installation.

## F-02 — native-state guard preserved

`ensure_cursor_sdk_client_paths` does not accept a zero-byte SQLite file merely
because `PRAGMA schema_version` succeeds. Its read-only validation rejects a
short header, failed `quick_check`, no pages, or no persisted table. Tests cover
absent, zero-byte, truncated, and corrupt indices without creating replacement
state.

The inherited exact-bound-agent correction is preserved: public
`AgentNotFoundError` during resume maps to `cursor_sdk_native_unavailable`, keeps
the reservation, and sends nothing. Fake native-state fixtures use a real SQLite
database/table rather than an empty marker.

## F-03 — pytest-process isolation preserved

The SQLite integration module does not import `cursor_sdk` during collection or
inside the pytest process. It locates the distribution without importing the
package and executes public bridge calls in isolated subprocesses. Submission
tests retain an operation-scoped `builtins.__import__` guard, so a real
submission-time SDK import fails regardless of test import order.

## F-04 — historical provenance preserved

`tests/fixtures/phase25_7_historical/` remains the privacy-safe capture produced
by pre-change writers at source commit
`3d1a7a3074e0edfe8ef2f0aca16d4d12d77c3250`: contexts v7/v8,
definition/entries v3, the canonical first frozen entry and hash, provenance,
writer names, and canonical SHA-256 values. Compatibility tests verify exact
round trips and hashes. Fresh standalone submission and sequence preparation
snapshot the fixtures and verify no historical byte changes.

## Red/green evidence

The upstream control reproduces the old route: despite an advertised requested
root, the unmodified default Python launch lists/gets only its default-store
agent and rejects both explicit-owner agents. The production-command test uses
the same seeded topology and is green for A/B/A owner isolation, reopen, and
default exclusion.

The first three-test production-route run reached the expected owner each time
but failed an overstrict assertion that every owner-store byte remain unchanged
across SQLite open/close; native WAL/SHM lifecycle legitimately changes those
bytes. The contract assertion was corrected to require exact default-store byte
stability plus owner isolation/reopen. Evidence was `1 failed, 2 passed in
5.87s`, then `3 passed in 4.35s`.

The first combined regression after adding runtime resolution reported `56
failed, 235 passed in 395.71s`. Every failure shared one test-fixture cause: the
central fake `cursor-sdk` distribution supplied Python metadata but omitted the
bundled Node and original bridge resources now required by production preflight.
The fixture now supplies synthetic installed resources and asserts the exact
four-part launch command; representative initial/correction, busy, and sequence
tests passed before the full rerun.

The first mypy run found one `importlib.metadata.PackagePath`/`SimplePath`
conversion error in the resolver. Converting resource results through `str`
before `Path` construction fixed it. No production contract was weakened for
either correction.

Final pinned-SDK bridge/resource gate after all edits:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/phase257-uv-cache \
  uv run --extra cursor-sdk python -m pytest -q --tb=short \
  tests/integration/test_phase25_7_sdk_sqlite_store.py
5 passed in 50.60s
```

That file covers direct native persistence, the upstream default-route control,
positive production A/B/A isolation and default exclusion, corrupt-open
fail-before-agent-operations behavior, and the installed-wheel resource/launch
proof. Missing initializer and missing runtime pre-send unit cases separately
exercise the real resource resolver and reported `2 passed in 0.29s`.

Complete affected focused gate after the production and shared-fixture
corrections (the later test-only missing-resource strengthening was rerun
separately above):

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/phase257-uv-cache \
  uv run --extra cursor-sdk python -m pytest -q --tb=short \
  tests/unit/test_phase25_1_sdk_adapter.py \
  tests/unit/test_phase25_1_sdk_config.py tests/unit/test_paths.py \
  tests/unit/scheduler/test_phase25_2_sdk_contracts.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/unit/scheduler/test_phase25_7_historical_compatibility.py \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_3_sdk_recovery.py \
  tests/integration/test_phase25_5_sdk_acceptance.py \
  tests/integration/test_phase25_6_sdk_busy.py \
  tests/integration/test_phase25_7_sdk_sqlite_store.py \
  tests/unit/scheduler/test_phase27_1_implementer_inputs.py \
  tests/integration/test_phase27_3_implementer_sequences.py
292 passed in 435.87s (0:07:15)
```

Final static and documentation checks:

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/phase257-uv-cache \
  uv run python -m ruff check .
All checks passed!

TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/phase257-uv-cache \
  uv run python -m ruff format --check .
415 files already formatted

TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/phase257-uv-cache \
  uv run python -m mypy src
Success: no issues found in 216 source files

TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/phase257-uv-cache \
  uv run mkdocs build --strict
Documentation built successfully in 0.61 seconds
```

## Changed production callers and files

The production adapter command path is consumed through
`cursor_attempt_runner._sdk_adapter` by create, resume/correction, inventory and
reconciliation operations. Binding validation/propagation changes also cover
`AttemptService`, `CursorWorkflowService`, `cursor_sdk_reconciliation`,
`cursor_evidence`, `review_recovery`, submission, sequence preparation and
materialization, state/sequence models, SQLite canonical persistence, and their
schemas. The adapter port adds the frozen backend/layout fields; path handling
provides the owner-native validation.

Production/schema files changed by the complete candidate are:

- `src/ai_dev_loop/paths.py`;
- `src/ai_dev_loop/integrations/cursor_sdk/{adapter.py,port.py,native_store_initializer.mjs}`;
- `src/ai_dev_loop/scheduler/application/{attempt_service.py,cursor_evidence.py,cursor_sdk_reconciliation.py,cursor_workflow_service.py,review_recovery.py,sequence_materializer.py,sequence_prepare.py,submission.py`;
- `src/ai_dev_loop/scheduler/{cursor_attempt_runner.py,domain/sequence.py,domain/state.py,infrastructure/sqlite_store.py}`;
- submitted-context v9/v10, sequence definition/entry v4, and updated prepared,
  active, and workflow-checkpoint schemas under `src/ai_dev_loop/schemas/`.

Affected tests cover Phase 25.1/25.2/25.3/25.5/25.6/25.7 and Phase 27
standalone/sequence paths. Operator documentation was updated in
`docs/referencia/{cli.md,configuracion.md}` and
`docs/operacion/{cutover-cursor-sdk.md,estado-artefactos.md,prepare-start-resume-abort.md,seguridad-privacidad.md}`.

## Remaining gates and boundaries

- Repository-wide pytest and the outer full packaging/integration pipeline were
  not run; they remain the separate pipeline gate. The focused temporary wheel
  build/install proof above was run.
- Real-model smoke, live acceptance observation, operator/workstation
  installation, live ledger/artifact mutation, scheduler/controller actions,
  services/timers, retention cleanup, and deliveries B/C were not executed.
- No staging, commit, push, dependency installation into the project/workstation,
  service operation, or Git-history mutation was performed.
- No new OpenQuestion was found. The operator-approved public native-store
  initializer resolves the obsolete F-01 prerequisite without relaxing owner
  isolation or expanding scope.
