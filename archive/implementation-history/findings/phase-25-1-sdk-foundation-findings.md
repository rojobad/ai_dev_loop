# Phase 25.1 SDK foundation findings

Status: **READY_FOR_REVIEW**. This label reports implementation evidence only.
It does not authorize acceptance, a checkpoint commit, installation, provider
activity or any external Git action.

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-25-sdk-replanned`, starting HEAD
`1d724d1f48653e6dd730d103e73d45577e3fb3e5`. Baseline `558b799` was verified as
an ancestor (`git merge-base --is-ancestor`, exit 0), the initial worktree was
clean, and the replanned Phase 25 overview has no OpenQuestions. Phase 24 was not
used. The implementation started from the accepted Phase 25 predecessors and
Phases 26-28 already present at HEAD.

The package now exposes optional extra `cursor-sdk` pinned exactly to 1.0.37,
with its lockfile entry. Config v1 retains Cursor CLI semantics; config v2 is an
explicit, strict SDK selection with version-specific defaults and canonical
parameters. A typed, lazy-imported adapter owns public SDK bridge operations,
safe outcomes, credential resolution and private per-conversation state/store
roots. Cursor-selected v2 is rejected at the Phase 25.1 submit/sequence boundary;
Codex-selected v2 continues through normal admission without a Cursor binary,
package, key or environment. No scheduler SDK dispatch was added.

All automated tests used temporary repositories and isolated HOME/XDG/cache/state
roots, fake SDK/provider objects, fake executables or a keyless/fake-key local
bridge health check. No real model, credential, live scheduler ledger, timer,
integration, workstation install, cleanup flow or external/destructive Git
operation was invoked.

## Contract evidence

| ID | Production caller/path | Independent automated evidence | Result |
| --- | --- | --- | --- |
| F-01 | `pyproject.toml` and `uv.lock` publish `cursor-sdk==1.0.37` only through extra `cursor-sdk`; `ai_dev_loop.integrations.cursor_sdk` imports the SDK lazily inside `CursorSdkAdapter.open`. The base package, CLI and historical state reader do not import it. | `test_python311_built_extra_bridge_and_base_historical_imports` builds the actual wheel, installs the extra into a fresh Python 3.11 venv, runs keyless bridge health/version with external Node absent, exercises the production adapter lifecycle with the real bridge and a fake non-authenticated key, then installs the base wheel into a second venv and runs the console help plus a genuine Phase 22 historical scheduler fixture while asserting `cursor_sdk` is absent. The same isolated extra runs the production replay correction with pinned public SDK objects and an offline `httpx.MockTransport`. | Final focused selection: **119 passed in 39.77s**. Isolated runtime was Python **3.11.15**; SDK **1.0.37**, bridge **1.0.0**, protocol **sdk.v1**, ping `pong`; ten expected capabilities were asserted and `shutil.which("node")` was `None`. |
| F-02 | `load_project_config`/`resolve_effective_config` choose defaults by source version, reject duplicate YAML keys and mixed global/repository versions, preserve explicit provider replacement, and reject v1 Cursor overrides for v2. `validate_config` reports the source version. `SubmissionService.submit` and `SequencePrepareService.prepare` apply `_reject_cursor_sdk_dispatch` only when the resolved implementer is Cursor, before command freeze/publication; Codex bindings remain independent from reviewer B. | `test_phase25_1_sdk_config.py` covers strict/null/absent/numeric/boolean/unknown/duplicate/unsafe cases, canonical `fast: false`, version layers, config validation, standalone submit and manifest-v3 prepare. It proves Cursor v2 creates no run/sequence/effective-config artifact, while Codex v2 succeeds with no SDK import/key/Cursor runtime and freezes its exact model/effort/timeout separately from reviewer settings. The included Phase 27 and v1 config selections independently cover existing Codex/v1 and provider precedence behavior. | All included F-02 and regression tests passed within **119/119**. No config validation path imported, probed, launched or authenticated Cursor. |
| F-03 | `build_cursor_sdk_client_config`, `read_cursor_credential` and `ensure_cursor_sdk_client_paths` feed `CursorSdkAdapter`; the port exposes open/close/create/resume/send/live-or-replay stream/get/list/cancel with SDK-independent DTOs. XDG refs, owner-derived SHA-256 keys, workspace confinement, private bridge/store roots and safe error codes are the production boundaries. No scheduler calls this adapter until Phase 25.2 by design. | `test_phase25_1_sdk_adapter.py` uses fake public SDK classes and tests exact options/key placement, environment scrubbing/restoration, 0700/0600 ownership, missing/empty/multiline/symlink/mode/overlap failures, distinct owner roots, single mutating calls and normal/exceptional close. `test_codex_selected_v2_does_not_copy_available_cursor_key_to_scheduler_state` scans every scheduler-state file for a sentinel and finds none. The package test opens/closes the real pinned bridge and separately exercises real public SDK objects over an offline transport. | Adapter unit file: **19 passed** in its final isolated rerun; all contracts also passed in the **119-test** final selection. Secret sentinels were absent from bridge environments, captured logs, public outcomes, ledger and protected artifacts. |
| F-04 | `CursorSdkAdapter._classify` maps public pinned exceptions and narrowly interprets generic server metadata; `stream(..., mode="replay")` invokes public `observe` directly and then public `wait` for the authoritative terminal `RunResult`; live streaming likewise combines persisted run identity with that distinct terminal result. `cancel` verifies the public `GetRun` handle identities before its public cancel request. `max_retries=0` leaves retry authority outside the adapter. | Parametrized offline tests cover authentication, 429 metadata, timeout, unknown identity, generic feature-unavailable, generic server error and send-only busy classification with one create/send call. Replay proves `supports("observe")` is never consulted, explicit key options are used, and one exact terminal status/text/usage DTO contains no event/result/replay counters. Fake and real-SDK `MockTransport` cases cover stale `GetRun`, terminal `ObserveRun`/`WaitLiveRun`, credential-bearing 401 metadata, legitimate 429 metadata, and cancel identity mismatches. Source inspection rejects the POC's private SDK attributes. | All F-04 cases passed in the **19-test** adapter suite and **119-test** final focused selection. No credential survives failure metadata; mismatched identities issue zero cancellation requests and an exact identity issues one. No 500 was treated generically as capacity and no ambiguous adapter failure retried work. |

## Correction findings

- Review F-01 — **FIXED**. Production path:
  `CursorSdkAdapter.stream(mode="replay")` -> `_get_public_run` -> public
  `Run.observe` -> public `Run.wait` -> `_terminal_result`. The initial public
  `GetRun` handle is no longer treated as terminal after replay because pinned
  SDK 1.0.37 does not mutate it while `ObserveRun` is consumed. Independent
  `test_public_replay_ignores_unreliable_supports_and_returns_one_terminal_usage`
  starts from a running/stale snapshot, supplies completion separately, asserts
  exact final status, text and all six usage fields, and proves `supports` is
  never called. The isolated package test repeats the contract with actual
  pinned `CursorClient`/`Run` objects and `httpx.MockTransport`; it additionally
  proves actual `Run.supports("observe")` is false and the observed handle stays
  stale before the adapter obtains the terminal public wait result.
- Review F-02 — **FIXED**. Production path: `CursorSdkAdapter.open` retains the
  active credential in memory before bridge launch, and `_classify` passes
  request/retry metadata through credential-aware `_safe_metadata`. A field
  containing the known credential is discarded; exception messages never enter
  public failure DTOs. Unit tests cover create and bridge-launch exceptions.
  Actual SDK HTTP 401 and 429 objects from offline `MockTransport` prove a
  synthetic active key is absent from the outcome while legitimate
  `request-safe-429` and retry-after `17` remain intact.
- Review F-03 — **FIXED**. Production path: `CursorSdkAdapter.cancel` resolves
  the public handle, compares both returned agent ID and run ID to the requested
  identities, and only then invokes public `Run.cancel`. Parametrized unit tests
  and actual pinned `Run`/offline transport cases assert zero `CancelRun`
  requests for either mismatch and exactly one for an exact match. The safe
  mismatch result is `INVALID_RESPONSE` and retains only the requested
  identities.

Correction-specific commands completed without a failed test:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q \
  tests/unit/test_phase25_1_sdk_adapter.py
```

Result: **19 passed in 1.73s, exit 0**.

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q -x \
  tests/integration/test_phase25_1_sdk_packaging.py
```

Result: **1 passed in 42.56s, exit 0**. The later full focused selection rebuilt
the wheel again from the final corrected sources and passed.

## Isolated Python 3.11/package prerequisite

The prerequisite was checked before dependent adapter work. The first exploratory
`uv` install attempt used the environment's default cache and failed because that
cache was read-only. The successful retry isolated `HOME`, every XDG root,
`UV_CACHE_DIR` and all temporary variables under
`/tmp/ai-dev-loop-phase25-prereq.bn4r20`, created a Python 3.11.15 venv and
installed `cursor-sdk==1.0.37`. A first deliberately minimal PATH probe containing
only `sh` failed because the bundled launcher also calls `dirname`; adding only
`/usr/bin/dirname` (still no external `node`) produced:

```text
sdk_version=1.0.37
ping=pong
bridgeVersion=1.0.0
protocolVersion=sdk.v1
external_node=None
```

The final reproducible integration test performs these exact subprocess shapes,
with every `<pytest-tmp>` path owned by pytest:

```bash
.venv/bin/python -m build --wheel --outdir <pytest-tmp>/dist
uv venv --python "$(command -v python3.11)" <pytest-tmp>/extra-venv
uv pip install --python <pytest-tmp>/extra-venv/bin/python \
  '<pytest-tmp>/dist/ai_dev_loop-0.1.0-py3-none-any.whl[cursor-sdk]'
env HOME=<isolated> XDG_CONFIG_HOME=<isolated> XDG_STATE_HOME=<isolated> \
  XDG_CACHE_HOME=<isolated> TMPDIR=<isolated> PATH=<sh-and-dirname-only> \
  <pytest-tmp>/extra-venv/bin/python -c '<keyless health and adapter lifecycle script>'
uv venv --python "$(command -v python3.11)" <pytest-tmp>/base-venv
uv pip install --python <pytest-tmp>/base-venv/bin/python \
  <pytest-tmp>/dist/ai_dev_loop-0.1.0-py3-none-any.whl
<pytest-tmp>/base-venv/bin/ai_dev_loop --help
<pytest-tmp>/base-venv/bin/python -c '<no-SDK historical reader script>' \
  tests/fixtures/phase22_historical/manual_wait_state_pre_phase22_v1.json
```

Result: **1 passed** as part of the final focused run. The test asserts Python
major/minor 3.11 rather than accepting the earlier Python 3.13 POC. No model API
or credential authentication is called; `offline-health-only` is never sent.

The lockfile was updated without syncing or installing the checkout:

```bash
HOME=<isolated> XDG_CONFIG_HOME=<isolated> XDG_STATE_HOME=<isolated> \
  XDG_CACHE_HOME=<isolated> UV_CACHE_DIR=<isolated> TMPDIR=<isolated> uv lock
```

Result: **62 packages resolved**; `cursor-sdk 1.0.37`, `anyio 4.15.1`,
`h11 0.16.0`, `httpcore 1.0.9` and `httpx 0.28.1` were added to the lock.
`uv lock --check` subsequently resolved the same 62 packages in 2 ms, exit 0.

## Exact final validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q \
  tests/unit/test_phase25_1_sdk_config.py \
  tests/unit/test_phase25_1_sdk_adapter.py \
  tests/integration/test_phase25_1_sdk_packaging.py \
  tests/unit/test_config.py \
  tests/unit/test_cursor_runner.py \
  tests/unit/scheduler/test_phase27_1_implementer_inputs.py \
  tests/integration/test_phase28_short_names.py
```

Result: **119 passed in 39.77s, exit 0**.

```bash
.venv/bin/python -m ruff format --check .
```

Result: **exit 1**. Ruff reports exactly five pre-existing, unchanged files that
would be reformatted: `src/ai_dev_loop/cli.py`,
`src/ai_dev_loop/dashboard/domain/filters.py`,
`src/ai_dev_loop/scheduler/infrastructure/sqlite_store.py`,
`tests/integration/test_phase28_short_names.py`, and
`tests/unit/scheduler/test_phase20_1_sequence_prepare.py`. It reports **390 files
already formatted**, including every Phase 25.1 file. Those unrelated accepted
files were not reformatted.

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Results: Ruff `All checks passed!`; MyPy `Success: no issues found in 207 source
files`; strict MkDocs **exit 0, built in 0.90s** with only the Material/MkDocs 2
informational notice and the existing unnav-listed incident-page notices; diff
check **exit 0**.

The first packaging-test run failed **1 test** because the test invoked the
nonexistent module entry point `python -m ai_dev_loop`; the package intentionally
exports the `ai_dev_loop` console script. The corrected test invokes the installed
script and passes. Intermediate targeted Ruff runs found local import ordering,
line layout and nested-context style issues; those were corrected before the
final repository check. One targeted formatter invocation also included the
Markdown findings path and exited 2 because Ruff Markdown formatting requires
preview mode; the corrected Python-only invocation passed. A fake live terminal
initially mirrored the replay
object too closely; inspection of the isolated pinned public signatures showed
that `Run.wait()` returns a distinct `RunResult`, and the production conversion
plus fake contract were corrected before final validation.

The final explicit staging attempt used `git add --` with only the Phase 25.1
files listed in this finding. Git rejected it before changing the index:
`fatal: Unable to create '.git/index.lock': Read-only file system`. The
implementer environment intentionally has read-only `.git` access, so the safe
next action is the scheduler-owned staging normalization after this turn. No
permission change, alternate index, commit or bypass was attempted.

## Unexecuted work and safe next actions

- Repository-wide pytest is **not run**; the plan explicitly reserves that gate
  for Phase 25.5. The focused result does not imply repository-wide validation.
- No real Cursor/Codex model, authentication, usage/billing, live restart,
  process-group crash supervision, timer or installed-scheduler behavior was
  tested. Those claims are not made.
- No mandatory F-01-F-04 implementation item is known unfinished. The only
  validation exception is the documented pre-existing repository format debt;
  all changed files pass the formatter.
- Phase 25.2 may safely replace only the Cursor-selected v2 guard with durable
  SDK bindings/dispatch and process supervision. It must preserve the exact
  validated selection, separate implementer/reviewer identities, native store
  authority, current timeout/recovery/Git policy and named metadata. Phase 25.3+
  owns durable recovery/usage; this slice does not imply exactly-once inference.
- The three requested skills are unchanged. No `.cursor/skills/` directory was
  created. Final implementation changes are left intact for the scheduler's
  staging normalization and separate reviewer; no commit, push, PR, merge or
  accepted intermediate checkpoint was made.
