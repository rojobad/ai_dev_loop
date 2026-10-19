# Phase 25 — Local Cursor SDK adoption and token usage

## Goals

Replace Cursor CLI execution for new **Cursor-provider** scheduler admissions with
a supervised local Python SDK integration. Preserve Codex CLI implementation,
the durable implementer/reviewer exchange, supported
recovery decisions, Git authority and read-only access to historical records.
Record provider-specific token usage by attempt, iteration, run, phase and sequence
with explicit coverage and idempotent aggregation.

Replanning baseline: `main`, commit `558b799` (full identity in the replanning
findings). Phase 27 dual implementers and authenticated recovery, Phase 26 local
Dashboard, and Phase 28 names/inspection/filter refinements are implemented.
Numbering follows the accepted repository artifacts. Phase 24 is unexecuted and
is **not a prerequisite**. Preserve fixed timeouts and all existing provider
retry policies. Recheck accepted code before dependent work.

The operator approved the SDK design on 2026-10-09 and on 2026-10-16 authorized
updating these plans, committing them, preparing a fresh sequence and starting it
with Codex `gpt-5.6-sol` / `xhigh` as implementer. Reviewer choices below stay
unchanged. This authorizes real implementation/review turns through the installed
scheduler, not live SDK smoke tests, package installation, credentials, timer
changes, deployment or final Git publication.

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
| 25.4 | [Token usage and historical queries](phase-25-4-token-usage-and-historical-queries.md) | Cursor implementer plus Codex implementer/reviewer observations, coverage-aware role/provider aggregates, read-only usage CLI and historical/Dashboard query regressions. |
| 25.5 | [Integration and cutover acceptance](phase-25-5-sdk-integration-and-cutover-acceptance.md) | Both-provider integration gate, SDK-only new Cursor admissions, Cursor CLI retirement, preserved Codex execution and Dashboard, operational docs and manually reviewed source configuration. |

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

The fresh execution manifest is [phase-25-replanned-sequence.yaml](phase-25-replanned-sequence.yaml).
It uses the installed manifest v3, a meaningful short name for the sequence and
each leaf, and independent explicit settings per phase:

| Subphase | Implementer model / effort | Reviewer model / effort |
| --- | --- | --- |
| 25.1 | `gpt-5.6-sol` / `xhigh` | `gpt-6.1-sol` / `high` |
| 25.2 | `gpt-5.6-sol` / `xhigh` | `gpt-6.1-sol` / `xhigh` |
| 25.3 | `gpt-5.6-sol` / `xhigh` | `gpt-6-astra` / `high` |
| 25.4 | `gpt-5.6-sol` / `xhigh` | `gpt-6.1-sol` / `high` |
| 25.5 | `gpt-5.6-sol` / `xhigh` | `gpt-6-astra` / `high` |

The manifest freezes seven completed reviews maximum and 90-minute role
timeouts, following current project/sequence conventions. Only accepted
non-final phases receive local checkpoints; final changes remain staged.

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
- Phase 27 `codex_implementer_attempt_runner.py`, role-specific Codex evidence,
  `implementer_timeout_retry.py`, `implementer_failure_recovery.py`,
  `sequence_implementer_recovery.py`, mixed-provider materialization and held
  abort paths. Preserve implementer/reviewer identity and capacity separation.
- `dashboard/`, `integration_api/dashboard_*`, `dashboard-web/`, Phase 26/28
  rules, accepted findings and tests. Integration API is currently **1.11**;
  SQLite is schema **17**, submitted contexts **1–6**, frozen definitions/entries
  **v1/v2**, manifests **v1/v2/v3**. Inspect current versions before allocating new
  ones; never reuse a Phase 27 version for a different SDK shape.
- Current tests for configuration, schemas/history, attempts, abort, Phase 20
  sequences, Phase 21 inspection and Phase 23 recovery. The POC used Python 3.13;
  SDK operation on the project's minimum Python 3.11 remains a mandatory check.

Official references: [Cursor Python SDK](https://cursor.com/docs/sdk/python),
[Codex JSON output](https://learn.chatgpt.com/docs/non-interactive-mode).
Verify adopted SDK behavior against the pinned package and preserved POC;
documentation alone does not establish production parity.

## Cursor Rules And Skills

Both Codex and Cursor implementers follow `AGENTS.md` and these ten
`.cursor/rules/` files (the directory name does not limit their applicability):

- `ai-dev-loop-governance.mdc`
- `ai-dev-loop-orchestrator-contracts.mdc`
- `ai-dev-loop-state-and-schema-contracts.mdc`
- `ai-dev-loop-loop-and-resume-contracts.mdc`
- `ai-dev-loop-abort-contracts.mdc`
- `ai-dev-loop-codex-review-contracts.mdc`
- `ai-dev-loop-docs-acceptance-contracts.mdc`
- `ai-dev-loop-global-integrations-contracts.mdc`
- `ai-dev-loop-dashboard-contracts.mdc`
- `ai-dev-loop-dashboard-refinements-contracts.mdc`

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

New Cursor SDK project configuration uses version 2 and an explicit SDK binding:

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

Preserve `implementer` selection and its provider-specific merge/override rules
from Phase 27. An omitted implementer still means Cursor; explicit Codex keeps
its independent command/model/effort/timeout and enforced sandbox/approval policy.
Codex-provider admissions with valid v1 config remain supported. They do not
require Cursor CLI, the SDK extra, a Cursor key, catalog, bridge or native store.
V2 can also select Codex; validate its configuration syntax but perform no Cursor
runtime work for that selection. Temporary v2 dispatch guards in 25.1 apply only
when the resolved implementer is Cursor. Never infer a provider from the required
historical `context.cursor` field or config version alone.

Do not shallow-merge legacy Cursor defaults into v2 SDK configuration. Resolve
the config-version/runtime branch before applying compatible defaults; an omitted
SDK selection cannot inherit a CLI alias. Frozen Codex bindings and historical
v1 config serialization retain their existing semantics and hashes.

In 25.2 introduce SDK-capable manifest v4 by extending v3's mandatory short names
and independent implementer/reviewer selection, with explicit complete SDK
model+parameter overrides. Keep new v3 Codex-only preparations supported; v3
Cursor CLI definitions become historical at final cutover. Preserve accepted
v1/v2/v3 replay and original frozen bytes. Allocate new SDK frozen-entry/context
versions above existing versions rather than overwriting Phase 27 contracts.

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
review ceilings; no Phase 24 activity policy. Cursor remains the default
implementer; frozen Codex implementation and mixed-provider sequences remain
supported. Codex stays final reviewer. Preserve separate authenticated Codex
implementer/reviewer sessions, role-bound capacity waits, exact reviewer B and
explicit independent model/effort,
prompt/fix envelopes, staging normalization and authorized non-final checkpoints.
No provider switching, autonomous commits, push, PR or merge enters the runtime.
Preflight is admission, not continuous Git-status policing.

### Historical data and metrics

Historical records remain byte/hash intact and readable through version-aware
models/projections in every affected slice. No legacy run continuation is
promised for Cursor CLI. At final cutover, reject attempts to
start/retry/extend/handoff old **Cursor CLI-backed** execution with an actionable
message, while read/inspection and safe
non-agent cancellation of pending work remain available. Old prepared work
containing Cursor CLI must be cancelled/settled and freshly prepared,
never rewritten in place. Codex-only frozen inputs remain executable and
recoverable; do not reject them merely because they contain v1 config or an
unused legacy Cursor binding. Precheck all effective providers in a sequence at
start; an old mixed definition with any CLI-Cursor leaf cannot partially start
under the final runtime. Retain guards at materialization/recovery/handoff too.

Numeric usage counters are provider-specific data. Cursor cache categories are
additional to its `input_tokens` in the tested SDK; Codex cached input is a subset
of its input. Reasoning is an output subset. Keep native counters and explicit
normalization versions; never add cached/reasoning subsets a second time.
Cancelled/crashed attempts can consume without counters: absent usage is unknown,
not zero. Deduplicate event/result/replay evidence by scheduler/native identity.
Metrics remain observational and cannot authorize retries, influence reviews or
change budgets. No billing API prerequisite, USD estimate or new model call.

Codex usage includes **both roles**, identified by authenticated attempt
component/effect and invocation evidence, never by shared model or session text.
Expose provider+role breakdowns before optional combined totals. Initial and
correction implementer turns are not reviews, and capacity probes are not
inference attempts. Usage counters must not change existing Dashboard activity
definitions, short-name metadata, linked selection, filters, paging or privacy.
This revision preserves the Dashboard; it does not add a new token chart/UI.

## Implementation Plan

Implement 25.1 -> 25.2 -> 25.3 -> 25.4 -> 25.5. Contract IDs in each linked plan
are mandatory and map to production callers/tests. Do not silently implement
later slices to make an intermediate checkpoint appear deployable. Earlier
checkpoint guards remain explicit until their owning later slice replaces them.

Implementation must use a stable installed scheduler runtime that is independent
of the changing source checkout. Do not install editable WIP into the live worker
environment. Each phase can be implemented/reviewed against its predecessor in
the current checkout using the fresh manifest linked above. The operator has
authorized preparation/start after the planning commit; record the resulting
exact sequence/run IDs from scheduler receipts, never guess them. Do not
hot-install source changes during this self-hosted sequence. Its driver and
frozen v1 project configuration stay independent of the final v2 source YAML.

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

The original planning checks are recorded in the original planning findings;
they do not describe this newer baseline. Record replanning checks separately
in `../findings/phase-25-replanning-after-phases-26-28.md`. Format changed
maintained code without unrelated reformatting. Archived verbatim POC scripts
remain excluded from maintained-code lint discovery and hash-checked.

## Risks Or Recovery Notes

SDK 1.0.37 reports busy and billing unavailability as `InternalServerError`;
`supports('observe')` can disagree with actual replay. Classify authenticated
codes/context, not generic HTTP 500 or unreliable capability flags. No automatic
fallback to a different SDK version or provider. Preserve source evidence in the
durable POC directory; its original absolute paths are mapped by that index.

Before deployment, settle old Cursor CLI runs, sequences containing such work and
their prepared definitions and
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
