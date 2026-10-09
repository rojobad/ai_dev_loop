# Phase 25.1 — SDK foundation and explicit configuration

## Goals

Introduce the pinned optional Python SDK dependency, a public-API adapter and
explicit local configuration/credential/store contracts. Close the Python 3.11
prerequisite and version-specific regressions before scheduler integration.
Existing CLI-backed execution remains the intermediate runtime.

## Non-Goals

SDK scheduler dispatch, billing, custom callbacks, cloud runtime, changing fixed
timeouts or installing the SDK into the live workstation.

## Scope

Packaging extra, SDK integration port/infrastructure, version-2 config parser,
safe credential/store path helpers, isolated bridge lifecycle, offline tests,
affected reference docs and project rules. New SDK input is explicitly guarded
at submit/sequence-prepare until 25.2 supplies its durable bindings.

## Out of Scope

Root `ai_dev_loop.yaml`, state-machine/recovery/schema changes owned by 25.2/25.3,
metric projections, historical execution, Phase 24, real credentials/model calls,
timer/installation actions, planning/review skills and unrelated Git mutations.

## Required Context

Read [the Phase 25 overview](phase-25-cursor-sdk-and-token-usage.md), its approved
configuration/storage contracts and [POC index](../findings/phase-25-sdk-poc/README.md).
The current config is v1/CLI; package Python minimum is 3.11. The POC establishes
SDK 1.0.37 on Python 3.13 with a bundled runtime and explicit project settings,
not operation on Python 3.11. Inspect `pyproject.toml`, `config.py`, `paths.py`,
`scheduler/infrastructure/paths.py`, `application/submission.py`,
`sequence_prepare.py`, `runners/probes.py`, and current configuration tests
under `src/ai_dev_loop/` and `tests/` before editing.

## Cursor Rules And Skills

Follow `AGENTS.md` and all eight `.cursor/rules/*.mdc` listed in the overview:
governance, orchestrator, state/schema, loop/resume, abort, Codex review,
docs/acceptance and global integrations. Read
`.agents/skills/create-cursor-plan/SKILL.md`; preserve that skill and
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`. No `.cursor/skills/`
exists. Correct directly affected rules/docs without advertising later dispatch.

## Architecture Guardrails

- Put SDK imports, dataclass conversion, exceptions and HTTP/bridge behavior in
  an integration adapter, behind a typed port. Domain/config and historical
  readers must import without the SDK installed. No SDK-private PID/error APIs.
- Pin `cursor-sdk==1.0.37` in optional extra `cursor-sdk`; do not upgrade Python,
  alter system packages or require external Node. Package selection is an
  infrastructure dependency, not provider switching configuration.
- Follow the overview's explicit v2 shape. Strict booleans for `fast`, strings
  for `context`/`reasoning_effort`; allow other catalog parameters as nonempty
  strings or strict booleans. Canonical parameters are sorted by key; convert
  booleans explicitly to SDK wire strings. Reject unknown section keys, null
  required members, numbers/boolean coercion, duplicate settings and unsafe refs.
  Runtime is exactly `sdk-local`; sdk_version must match the pinned version.
- Model/parameters are a complete selection. Retain v1 parsers for the
  intermediate CLI runtime and historical reads, without translating aliases
  into SDK defaults. SDK v2 rejects command/output_format/force/trust_workspace.
- Settings layers are an explicit nonempty list selected from supported public
  SDK layers; the approved project example is `[project]`. Sandbox values are
  explicit `disabled` or `enabled`, with disabled for this project; don't change
  permissions silently. Standard builtins and configured MCP remain available.
- Credentials are references to private XDG files. Reject symlink/traversal,
  missing/non-file/insecure files with safe diagnostics. The worker reads the
  value only for authentication; the adapter never logs or persists it. Handle
  trailing newline without accepting empty/multiline key payloads.
- Give each explicit client its workspace, state root and JSONL store root;
  launch/close its bridge inside the owning worker process group. No daemon or
  global shared client. Disable transport retries and exclude auth secrets from
  subprocess environments. Apply the POC's tested public replay configuration.

## Implementation Plan

**F-01 — Package and usable Python minimum.** Add the optional SDK extra and lazy
adapter imports. Install/build an isolated test distribution with the extra on
Python 3.11 and invoke bridge health/version without a key or model inference.
Assert the tested bundled-runtime behavior with external Node absent from PATH.
Historical CLI help/queries must import in a second environment without the extra.
If Python 3.11/wheel support fails, report the prerequisite and stop dependent SDK
work; raising the project's minimum needs a new explicit decision.

**F-02 — Explicit validated configuration.** Add config v2 SDK types and complete
model selection. Freeze-ready canonical values must preserve `fast: false` and
catalog parameter identity. V1 remains parseable with its original semantics.
Reject incomplete or CLI-only SDK fields. Existing submit/sequence preparation
must reject v2 at the temporary boundary before freezing a false CLI binding or
performing any agent/network call. 25.2 replaces this guard with real freeze.
Do not remove active CLI execution at this checkpoint.

**F-03 — Credential/store adapter boundaries.** Implement safe reference resolution
under the overview's XDG roots. Derive the store key from conversation-owner
identity; validate it as an opaque safe key rather than using an agent ID as a
path. Preserve private ownership and refuse repository overlap. Expose public
create/resume/send/stream/get-run/list-runs/cancel operations through the port
for later wiring; no implementation prompt is sent by config validation.
Typed adapter outcomes carry safe error codes, optional retry_after and required
identity/context. Values/tokens/environments never enter DTOs or diagnostics.

**F-04 — Pinned-version failures and replay.** Add offline adapter contracts for
auth failures, rate limits, busy/feature_unavailable carried by generic server
errors, unknown identities and timeouts. Preserve supported metadata without
interpreting all 500s as capacity or repeatable work. Test public observe directly
when the pinned `supports('observe')` incorrectly says false; confirm explicit
key with no env fallback key permits tested local read/replay. Capture exactly
one terminal text/usage without adding event/result/replay counters here.

Boundary: local configuration -> validated explicit selection -> eligible future
freeze, without SDK network, key reads or live store mutation at submit. Adapter
calls are worker-owned and close their bridge on normal/failed return; crash
supervision and durable create/send intent belong to 25.2. Do not claim safe
scheduler restart or inference exactly-once from this adapter-only slice.

## Testing Criteria

Add `tests/unit/test_phase25_1_sdk_config.py`,
`tests/unit/test_phase25_1_sdk_adapter.py` and
`tests/integration/test_phase25_1_sdk_packaging.py`. Use fake SDK public objects,
mock HTTP transport, fake bridge subprocesses and temporary HOME/XDG; adapt
recorded POC facts into independent expected assertions. No real keys/models.

| Contract | Acceptance scenario and production path |
| --- | --- |
| F-01 | Actual built package/extra on Python 3.11 runs health/version; base package without SDK imports help and historical readers; no global package changes. |
| F-02 | Actual config loader handles v1/v2, explicit false, null/absent/type/unknown-key cases; CLI submit and sequence prepare reject v2 at the documented temporary boundary with zero agent calls. |
| F-03 | Adapter receives exact key/options, bridge/tool environments and captured logs omit key sentinels; unsafe refs/modes/overlap fail; separate client/store roots remain distinct. |
| F-04 | Simulated 401/429/timeout/busy/feature errors have exact safe classifications and zero mutating retries; public replay works despite unreliable supports flag; no private attributes used. |

Tests must prove no key value is copied into artifacts or public errors and the
existing CLI/config flow still works. The no-SDK import test must run in a fresh
environment, not rely on monkeypatching an already imported SDK module.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/test_phase25_1_sdk_config.py tests/unit/test_phase25_1_sdk_adapter.py tests/integration/test_phase25_1_sdk_packaging.py tests/unit/test_config.py tests/unit/test_cursor_runner.py
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Record F-01–F-04, actual package/Python/bridge versions and exact isolated install/
health commands/results in `archive/implementation-history/findings/phase-25-1-sdk-foundation-findings.md`.
Python 3.11 coverage cannot be replaced by the Python 3.13 POC. Repository-wide
pytest belongs to 25.5; focused contract coverage is mandatory now.

## Risks Or Recovery Notes

The SDK starts native auxiliary processes and exceptions can contain secrets.
Test sanitized errors and public lifecycle closure rather than treating the
library as an in-process function. This checkpoint is not ready for deployment;
preserve the temporary v2 boundary and don't install an editable runtime.

## OpenQuestions

None. Report failed prerequisites before dependent implementation.
