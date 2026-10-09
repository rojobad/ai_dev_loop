# Phase 25 — Local Cursor SDK adoption and token usage

## Goals

Replace Cursor CLI execution for new scheduler admissions with a supervised local
Python SDK integration. Preserve the durable Cursor/Codex exchange, supported
recovery decisions, Git authority and read-only access to historical records.
Record provider-specific token usage by attempt, iteration, run, phase and sequence
with explicit coverage and idempotent aggregation.

Planning baseline: `main`, commit
`58944326a06cf319df521d595050fec091d870da`. Phase 23 implementation is present;
Phase 24 consists of unexecuted plans and is **not a prerequisite**. Keep the
existing fixed timeout budgets and retry policy. Recheck code and accepted
predecessors in the actual execution checkout before dependent work.

The operator approved the design below on 2026-10-09, requested these plans and
authorized committing/pushing the planning artifacts to the existing `main` branch.
That authorization does not start implementation, call models, install a package,
configure credentials, change the timer or deploy the final runtime.

## Non-Goals

Executing historical Cursor CLI sessions, inferring missing historical tokens,
USD billing, activity-aware timeouts, cloud agents, provider switching, concurrent
implementation agents, custom orchestrator tools or changing reviewer B.

## Scope

| Subphase | Plan | Accepted delivery |
| --- | --- | --- |
| 25.1 | [SDK foundation and configuration](phase-25-1-sdk-foundation-and-configuration.md) | Pinned package, explicit configuration/credential/store adapter and offline contracts; existing CLI execution remains the intermediate runtime. |
| 25.2 | [Identity, worker and evidence](phase-25-2-sdk-identity-worker-and-evidence.md) | Versioned SDK identities and supervised worker capture; fake-backed normal implementation/review/correction flow. SDK recovery remains explicitly guarded until 25.3. |
| 25.3 | [Continuity and recovery](phase-25-3-sdk-continuity-and-recovery.md) | Exact-agent retry/recovery, full process-tree cleanup, ambiguous-dispatch safety, sequence lineage and eventual reconciliation. |
| 25.4 | [Token usage and historical queries](phase-25-4-token-usage-and-historical-queries.md) | Cursor and Codex observations, coverage-aware aggregates, read-only usage CLI and historical query regression coverage. |
| 25.5 | [Integration and cutover acceptance](phase-25-5-sdk-integration-and-cutover-acceptance.md) | Complete integration gate, SDK-only new admissions, retirement of CLI execution, operational documentation and manually reviewed project configuration. |

Each subphase owns its mandatory tests and affected documentation. Historical
readers must remain functional **in every schema-changing slice**; 25.4 broadens
query coverage, rather than postponing that requirement. 25.2 must close process
ownership/termination for its enabled normal flow; 25.3 adds recoverability.

Concise implementation prompts: [25.1](prompt_phase-25-1-sdk-foundation-and-configuration.txt),
[25.2](prompt_phase-25-2-sdk-identity-worker-and-evidence.txt),
[25.3](prompt_phase-25-3-sdk-continuity-and-recovery.txt),
[25.4](prompt_phase-25-4-token-usage-and-historical-queries.txt),
[25.5](prompt_phase-25-5-sdk-integration-and-cutover-acceptance.txt).
These five prompts are deliberately included in the planning commit despite the
general ignore convention, so the published plans have portable handoff inputs.

## Out of Scope

Do not execute Phase 24, edit its plans/manifest, alter live ledger/artifacts or
native SDK stores, install integrations/timers, change lingering or WSL services,
run cleanup, modify planning/review skills, restore retired commands or perform
external/destructive Git actions. Preserve unrelated incident documentation.
Root `ai_dev_loop.yaml` changes belong only to the manually reviewed 25.5 cutover
configuration; the current planning delivery leaves it unchanged.

## Required Context

- `AGENTS.md`; current CLI/configuration, state/privacy and timer documentation.
- [Durable POC index](../findings/phase-25-sdk-poc/README.md), the preserved
  `SDK_ADOPTION_REPORT.md`, verification summary and synthetic evidence.
  Repeated offline checks: **27 passed** (22 recorded-evidence assertions,
  five simulated transport contracts). No production integration is implied.
- `runners/cursor.py`, `cursor_failure.py`, `cursor_output.py`, `process.py`;
  scheduler `cursor_attempt_runner.py`, `codex_attempt_runner.py`, application
  submission/sequence preparation/materialization, preflight, attempt services,
  systemd backend, Cursor workflow/evidence, abort/restart and Phase 23 recovery.
  Paths here are relative to `src/ai_dev_loop/`.
- Domain state/events/sequence/recovery models, `schemas/`, validated SQLite
  snapshot readers, protected artifacts and `integration_api/` readers.
- Current tests for configuration, schemas/history, attempts, abort, Phase 20
  sequences, Phase 21 inspection and Phase 23 recovery. The POC used Python 3.13;
  SDK operation on the project's minimum Python 3.11 remains a mandatory check.

Official references: [Cursor Python SDK](https://cursor.com/docs/sdk/python),
[Codex JSON output](https://learn.chatgpt.com/docs/non-interactive-mode).
Verify adopted SDK behavior against the pinned package and preserved POC;
documentation alone does not establish production parity.

## Cursor Rules And Skills

Every subphase follows `AGENTS.md` and these `.cursor/rules/` files:

- `ai-dev-loop-governance.mdc`
- `ai-dev-loop-orchestrator-contracts.mdc`
- `ai-dev-loop-state-and-schema-contracts.mdc`
- `ai-dev-loop-loop-and-resume-contracts.mdc`
- `ai-dev-loop-abort-contracts.mdc`
- `ai-dev-loop-codex-review-contracts.mdc`
- `ai-dev-loop-docs-acceptance-contracts.mdc`
- `ai-dev-loop-global-integrations-contracts.mdc`

Read `.agents/skills/create-cursor-plan/SKILL.md`. The separate read-only reviewer
uses `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`; implementers
must not modify either skill. There is no `.cursor/skills/` directory. Existing
rules are sufficient; update directly contradicted CLI-specific statements only
within the slice that changes their implementation. References to forbidden
"tokens" meaning authentication secrets do not forbid numeric usage counters.
For documentation and acceptance evidence, also read
`.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`, applying its current
source/privacy rules within this Phase 25 scope rather than restoring historical
Phase 8 commands. Keep that skill unchanged as well.

## Architecture Guardrails

### Approved configuration and storage

Use `cursor-sdk==1.0.37` initially, with public APIs and a private client/bridge
owned by each attempt worker. Python remains >=3.11; the package bundles its
runtime in the tested Linux/WSL environment. Do not require external Node or
install/change system Python. Add the SDK as an optional installation extra
`cursor-sdk`, avoiding an SDK import requirement for historical inspection.

New project configuration uses version 2 and an explicit SDK binding:

```yaml
version: 2
cursor:
  runtime: sdk-local
  sdk_version: 1.0.37
  model: grok-4.7
  parameters:
    context: 256k
    reasoning_effort: high
    fast: false
  credential_ref: cursor-default
  setting_sources: [project]
  sandbox: disabled
```

The SDK model/parameter choice is the POC-tested **new selection**, not a claim
of exact equivalence to CLI alias `grok-4.7-high`. Other projects supply their own
explicit model/parameters. Validate parameter names/values at bounded admission
through the catalog; do not query a model or credential during submit/prepare.
Model overrides replace the model+parameter selection as one complete object;
sequence phase overrides win over global preparation overrides, then YAML.
An omitted override inherits the already resolved explicit selection; explicit
null, partial objects and unknown configuration keys fail. No SDK model defaults
or automatic translation of old CLI aliases are permitted.

Freeze runtime/version, model and parameters, settings layers, sandbox,
credential reference, tool policy and workspace/store binding in versioned
contracts. Use the standard builtin tool policy, including read/edit/shell and
configured MCP access; do not introduce a restrictive custom allowlist as part
of adoption. Reapply options at create/resume/send boundaries as required by the
public API. `setting_sources: [project]` is the project choice approved here;
if the actual target needs user layers, make that target's explicit configuration
complete before admission. No silent ambient-settings expansion. SDK default
tool discovery may be used with a frozen policy marker and pinned SDK version;
do not persist fabricated lists of unavailable tools.

Credential files live under
`$XDG_CONFIG_HOME/ai_dev_loop/credentials/<credential_ref>` (default config home
`~/.config`), with private parent directories and file mode `0600`. References
are safe simple names, never values or arbitrary traversal paths. Submit/prepare
freeze the reference only. Workers validate/read it, pass `api_key` explicitly
and omit that key from bridge/tool environments, artifacts, errors and summaries.
Do not create a credential-writing command or install a real credential in tests.
Replaced credential contents may rotate authentication without changing frozen
model/workspace authority. Pin/test the documented POC workaround: explicit key,
no key in the environment, default `allow_api_key_env_fallback=True` for supported
local replay; do not patch SDK private internals.

The approved private credential file is user-owned external configuration, a
narrow exception to older rule wording forbidding all retained authentication
files. It does not authorize key copies in ledger/artifacts, project files,
native conversation state, subprocess environments or public output. Update
that affected wording in 25.1 while preserving the secret audit boundaries.

Native mutable conversation state lives at
`$XDG_STATE_HOME/ai_dev_loop/cursor-sdk/<conversation_key>/`, outside repositories
and immutable `artifacts/`. Use a stable safe key derived from the original
conversation-owner run identity. Successors inherit the **same key, exact agent
ID and store owner**, rather than opening a new store per successor. Retain it
until separately authorized deletion; introduce no retention cleanup command.
Authenticate paths/ownership and use `0700` directories/`0600` sensitive files.
The native store enables conversation continuity; ledger/artifacts remain the
scheduler authority. Historical queries require neither store, SDK, bridge,
credential nor Cursor CLI.

### Identity, execution and decision authority

Keep Cursor SDK agent/run identities separate from the Codex UUID type. Accept
the POC-observed exact `agent-UUID` and bare UUID agent IDs, and `run-UUID` native
run IDs, through bounded Cursor-specific validators; reject traversal/control
characters, arbitrary coercion, missing/empty IDs and unsupported formats safely.
Never strip prefixes, invent replacement identities, search for the last agent
or loosen reviewer B validation. One agent per conversation; one native run per
authorized prompt dispatch. A native status never substitutes for reviewed
acceptance, prompt integrity, fences or cancellation authority.

Persist intent before external create/send and bind returned identities as soon
as available. Disable transport retries (`max_retries=0`). A lost response can
mean accepted work: hold ownership, reconcile exact evidence or block safely;
do not resend merely because an exception occurred. Whole-tree termination and
ledger fencing are required before retry or release. The launcher PID alone is
insufficient; use the owned attempt unit/process mechanism, not SDK private PIDs.
SDK `SendOptions.local.force` must not inherit CLI `cursor.force` or automatically
authorize expiring an active turn. Use the proved get-run/cancel/resume recovery.

Keep current fixed timeout limits, three eligible automatic timeout retries and
review ceilings; no Phase 24 activity policy. Cursor remains implementer and
Codex final reviewer. Preserve exact reviewer B and explicit review model/effort,
prompt/fix envelopes, staging normalization and authorized non-final checkpoints.
No provider switching, autonomous commits, push, PR or merge enters the runtime.
Preflight is admission, not continuous Git-status policing.

### Historical data and metrics

Historical records remain byte/hash intact and readable through version-aware
models/projections in every affected slice. No legacy run continuation is
promised. At final cutover, reject attempts to start/retry/extend/handoff old
CLI-backed execution with an actionable message, while read/inspection and safe
non-agent cancellation of pending work remain available. Old prepared work must
be cancelled/settled and freshly submitted/prepared, never rewritten in place.

Numeric usage counters are provider-specific data. Cursor cache categories are
additional to its `input_tokens` in the tested SDK; Codex cached input is a subset
of its input. Reasoning is an output subset. Keep native counters and explicit
normalization versions; never add cached/reasoning subsets a second time.
Cancelled/crashed attempts can consume without counters: absent usage is unknown,
not zero. Deduplicate event/result/replay evidence by scheduler/native identity.
Metrics remain observational and cannot authorize retries, influence reviews or
change budgets. No billing API prerequisite, USD estimate or new model call.

## Implementation Plan

Implement 25.1 -> 25.2 -> 25.3 -> 25.4 -> 25.5. Contract IDs in each linked plan
are mandatory and map to production callers/tests. Do not silently implement
later slices to make an intermediate checkpoint appear deployable. Earlier
checkpoint guards remain explicit until their owning later slice replaces them.

Implementation must use a stable installed scheduler runtime that is independent
of the changing source checkout. Do not install editable WIP into the live worker
environment. Each phase can be implemented/reviewed against its predecessor in
an isolated checkout; these plans contain no automatically started sequence or
chosen reviewer model. Submit/prepare require explicit reviewer choices later.

## Testing Criteria

Use fake SDK ports/bridge executables and fake Codex with temporary native-WSL
HOME/XDG/repos. Tests call real CLI/application workflows, not only adapter
methods. Deterministic barriers cover promised crash/abort windows. Production
must supply its own locks/fences/reconciliation; fixtures must not supply the
missing behavior. Regressions must detect the POC failures: launcher-only kill,
orphan tools/stale running status, ambiguous send, replay double count and lost
historical snapshot validation.

The POC's 27 checks establish prerequisites and observed limitations, not a gate
for production. Test every changed schema/reader with genuine pre-25 fixtures,
old hashes and byte comparisons. Preserve the historical ability to query without
the SDK extra. Mandatory Python 3.11 isolated package/bridge check involves no
model calls. Live smoke remains a separately authorized operator action against
an isolated synthetic repository; never run it as an automated test.

## Validation

Each subphase lists focused pytest selections and quality/docs checks. Record
contract ID, production caller, independent acceptance test and exact commands/
results in its findings, distinguishing failed/unexecuted checks. Missing
mandatory behavior or coverage is incomplete scope, not residual risk.
After correction rerun failed/affected checks; broaden only for concrete risk.
The repository-wide suite belongs to the separate 25.5 integration gate.

Planning-time `ruff check .` passed. `ruff format --check .` reported 23 existing
unchanged source/test files as unformatted. Record that baseline separately;
format changed maintained code, but do not expand a subphase into unrelated
baseline reformatting merely to make a global command green. Archived verbatim
POC scripts are excluded from maintained-code lint discovery and hash-checked.

## Risks Or Recovery Notes

SDK 1.0.37 reports busy and billing unavailability as `InternalServerError`;
`supports('observe')` can disagree with actual replay. Classify authenticated
codes/context, not generic HTTP 500 or unreliable capability flags. No automatic
fallback to a different SDK version or provider. Preserve source evidence in the
durable POC directory; its original absolute paths are mapped by that index.

Before deployment, settle all old runs, sequences, prepared definitions and
pending recovery/handoff intents through their existing supported actions.
Confirm no outstanding owned processes or uncertain reservations. Preserve their
records. Install only the fully accepted final package with the SDK extra after
explicit operator authorization; packaging source changes are not installation.
Any rollback during SDK execution requires quiescence and a package that can read
the new records; blindly reinstalling the pre-25 reader is not a safe rollback.

## OpenQuestions

None for the approved design. A failed SDK/Python/platform prerequisite or a
target requiring a settings layer outside its explicit configuration must be
reported with evidence before dependent execution; do not invent a fallback.
