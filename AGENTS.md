# ai_dev_loop Agent Guidance

## Current workflow and source of truth

- New work uses `scheduler submit` -> `scheduler start` -> `scheduler tick`,
  or `scheduler sequence prepare` -> `scheduler sequence start` -> ticks.
  Read `docs/referencia/cli.md` for the supported command surface.
- Verify behavior against current code, schemas, CLI help, and tests. Keep
  rules and `/docs` aligned with that evidence. Archived plans and findings
  explain history; they do not reinstate retired commands or override the
  user's approved scope. Report unresolved safety decisions before dependent
  implementation; correcting an obsolete instruction is not such a decision.
- The central `engine.sqlite3` ledger and protected `artifacts/` are the
  scheduler's authority. Legacy `runs/.../state.json` is not live scheduler
  state. Do not restore top-level `prepare`, `start`, `resume`, `recover`,
  `launch`, or `pr-review` to satisfy historical instructions.

## Reviewer identity and frozen configuration

- Controller A is optional provenance. New submissions do not require a
  desktop session, a fork, or a message to another chat.
- `scheduler submit` requires explicit `--codex-review-model` and
  `--codex-review-reasoning-effort`. Sequence manifests supply both per phase.
  Freeze these inputs; do not inherit them from YAML defaults, controller
  session metadata, or Codex CLI configuration.
- Reviewer B does not exist at submission. The scheduler creates it once at
  the first review with `codex exec`, captures its exact identity, and uses
  `codex exec resume` for subsequent reviews and eligible retries. Never pass
  a pre-existing B through `--codex-session-id`, use `--last`, or guess an ID.
- The scheduler enforces `workspace-write` for the Codex subprocess. This
  capability does not authorize reviewer edits: the current wrapper and
  configured review skill require staged-only review without intentional
  repository modifications and permit relevant automated tests.
- Cursor remains the default implementation agent and Codex the final reviewer.
  `scheduler submit` can freeze an implementer provider of `cursor` or `codex`,
  with Codex command, model, reasoning effort, and timeout independent from the
  reviewer. Historical contexts that omit implementer metadata stay Cursor.
  Codex runs, standalone or manifest v2 sequence leaves, dispatch implementation and corrections in
  one authenticated implementer session, normalize staging, and then bootstrap
  an independent reviewer B. Structured/provider quota evidence or an exhausted
  post-failure observation from the shared capacity probe waits and resumes that
  exact pending role and session; it never
  starts review early. Manifest v2 freezes independent implementer settings per
  phase and supports mixed providers; historical v1 stays Cursor-only. Provider
  switching during a run and a scheduler-managed Cursor master/executor stage
  are not implemented configuration options. Confirmed Codex timeouts and
  authenticated generic failures use the shared bounded recovery policy.
  Unsafe implementer identity or incomplete evidence blocks with the repository
  reservation retained. Explicit `scheduler abort` cancels these held Codex
  blocks and releases resources only after attempt termination is resolved.

## Product intent and Git authority

`ai_dev_loop` is an orchestrator for a durable conversation between a Codex
reviewer and a programming agent. Its purpose is to preserve their hand-offs,
decisions, reviewed artifacts, and explicitly approved Git actions -- not to
become a general-purpose controller of every worktree change.

- Use preflight as an admission check before an agent run begins. It may verify
  repository identity, frozen inputs, and a safe starting worktree, including
  rejecting an unexpectedly dirty baseline when the active workflow requires
  it.
- After admission, the reviewer and implementer own the technical decision
  loop. Do not add continuous baseline checks, exhaustive Git-status emulation,
  or speculative drift gates merely to constrain normal work performed during
  that exchange.
- Stage or commit only an explicit, durable decision approved by the agents and
  allowed by the active phase. Never infer approval, create an autonomous
  commit, or use destructive Git operations to force a desired state.
- Preserve the hard boundaries that make orchestration trustworthy: repository
  reservation, immutable input and artifact integrity, exact agent identity,
  explicit review decisions, and no destructive or external Git actions unless
  the approved plan expressly authorizes them.
- The supported runtime normalizes staging after the selected implementer. An explicitly started
  sequence also authorizes the existing local checkpoint commit for each
  accepted non-final phase. Standalone runs and final sequence phases leave
  changes staged; acceptance does not authorize push, PR creation, or merge.

## Recovery and operator actions

- Use the durable state and safe next action: `scheduler implementer-retry` for
  eligible provider-specific timeouts and authenticated generic failures,
  `scheduler cursor-retry` for the compatible Cursor-only surface (including an
  eligible terminated timeout and `--force` failure recovery), `scheduler review
  retry` for eligible review failures/capacity waits, and `scheduler extend` for
  an explicitly increased review ceiling after `max_iterations_reached`.
  `implementer-retry --force` on a current Codex or Cursor sequence leaf reuses
  the same sequence and ordinal; a stale leaf with no existing relation is rejected.
  The one exception is an authenticated Cursor SDK terminal status whose public
  message is exactly `Agent turn stopped after repeated resume attempts made no progress`:
  the scheduler reserves one same-agent continuation for that logical turn and
  dispatches it on a later tick at least 60 seconds later. The fresh read and a
  finished replay use the owned reconciliation worker. The successor cannot
  dispatch until recovered output is installed. A lost read retires against the
  ready reconciliation and reschedules only when that attempt published no
  result. The send rechecks the original capture digest. Abort of the current
  successor or its sequence cancels a replay still waiting to install output and
  keeps the ready recovery hashes. Abort of the old source cancels only a continuation
  that has not started or been handed off. A second such failure
  stays manual. Other generic failures, timeouts, and capacity waits do not.
- Same-reviewer recovery preserves the authenticated reviewer binding. An
  eligible blocked run may have a successor; the blocked source is immutable.
  `scheduler extend` is a separate explicit same-run transition and does not
  rewrite the submitted configuration.
- Retired-state deletion is only the explicit `scheduler cutover cleanup`
  operator flow. Do not run cleanup, live agents, timer changes, or integration
  installation as a side effect of documentation or automated tests.

## Review standard

During phase/subphase implementation and review, run the tests for the approved
contracts and the regressions affected by the changed production paths. The
repository-wide test suite belongs to the pipeline/integration gate, not every
implementation turn or review round. After a correction, rerun the failed and
affected tests; broaden the selection only to resolve a concrete remaining risk.
Keep mandatory contract coverage intact. Report focused results and the separate
pending pipeline gate honestly; neither implies repository-wide validation.

Reviewers accept or reject staged changes based on the approved phase contract,
normal orchestrator flow, artifact and state integrity, prohibited side effects,
and user-visible safety. Do not block acceptance on exhaustive handling of rare
Git edge cases unless the plan explicitly promises that behavior or the case
breaks the supported normal workflow.

## Scheduler timer operations

For real workstation setup, enablement, recovery, or removal of the packaged
user-systemd scheduler timer in WSL, follow
`docs/operacion/timer-systemd-wsl.md`. Treat timer and WSL lingering changes as
explicit operator actions; do not perform them during implementation or tests
unless the active phase and user explicitly authorize real workstation work.

## Dashboard governance

For Dashboard planning, implementation and review, read
[Dashboard UX governance](docs/referencia/dashboard-ux.md) and
[Dashboard data contracts](docs/referencia/dashboard.md), then apply
`.cursor/rules/ai-dev-loop-dashboard-contracts.mdc` and
`.cursor/rules/ai-dev-loop-dashboard-refinements-contracts.mdc`.
The DUX contracts record the operator's current decisions: graphs first,
explanations behind the italic circled information icon, details on demand,
short names and secondary IDs, stable scroll/layout, automatic valid scope,
and shared selected-IANA date/duration formatting. Historical phase plans do
not restore superseded presentation. Apply relevant contracts to the approved
change; this guidance does not authorize unrelated implementation or deployment.

## Chosen short names for named preparation

Phase 28 introduces scheduler-owned presentation names. Once the installed CLI
supports named preparation, every new standalone submission must explicitly
supply --short-name, and every new sequence must include short_name on the
sequence and each planned phase. Use the installed CLI's supported named format: v3 for Codex-only and
v4 for SDK/mixed sequences. Choose meaningful names of at most 35
Unicode code points after trimming; do not derive new names by truncating IDs.
IDs retain identity. Historical v1/v2 stay readable and replayable; execution
eligibility follows the current provider contract, not the alias.
Never rewrite frozen inputs or install changing code into a live self-hosted
sequence to add labels. Only the explicit metadata command renames existing
records; a Dashboard read never does. Do not send unsupported flags to older
installed drivers. See docs/referencia/dashboard-ux.md and docs/referencia/cli.md.
