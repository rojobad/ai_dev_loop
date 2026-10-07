# Phase 24 — Cursor activity-aware timeouts

## Goals

Replace premature fixed-duration interruption of active Cursor turns with an
explicit activity policy: a soft threshold of 120 minutes, an inactivity window
of 20 minutes and an unconditional hard threshold of 360 minutes. A hard timeout
requires manual continuation; it must never authorize an automatic retry.

The operator approved three implementation runs on 2026-10-07. This overview and
the three linked plans define that scope; they describe future behavior, not a
feature already installed. Planning baseline: `main` at
`6792e0f` (Phase 23 integration already present). Verify prerequisites in the
actual execution checkout before dependent work.

## Non-Goals

Guaranteeing useful progress from activity, recovering the systemd control bus,
estimating token usage, monitoring CPU/Git changes, or controlling existing runs.

## Scope

| Run | Plan | Observable delivery |
| --- | --- | --- |
| 24.1 | [Configuration and frozen contracts](phase-24-1-timeout-policy-contracts.md) | Explicit policy inputs, versioned freeze/compatibility, and a safe execution boundary until 24.2. |
| 24.2 | [Runner, systemd and retry policy](phase-24-2-activity-aware-execution.md) | Recent activity survives the soft threshold; inactivity and hard timeouts have authenticated distinct outcomes; hard timeout waits for manual same-run continuation. |
| 24.3 | [Integration and operational acceptance](phase-24-3-timeout-integration-acceptance.md) | Full initial/correction/sequence/recovery flows, genuine historical compatibility, operational docs and final integration-gate evidence. |

Every slice owns its mandatory tests and directly affected documentation. 24.3
does not postpone safety or retry semantics required for 24.2 acceptance.

## Out of Scope

No production implementation during planning, live agent calls, timer/linger/bus
changes, installation, retroactive run/config/artifact edits, external Git
actions or destructive cleanup. Preserve the pending Phase 8.9 incident entry
and its existing diagnosis. Do not change planning/review skills.

## Required Context

Read `AGENTS.md`, current CLI/configuration/state/privacy/operation docs, the
three subplans, current process/attempt/retry/recovery code and relevant tests.

The Phase 8.10 case `ai-dev-loop-hub-52125e775ded` motivated this scope. Protected
attempt metadata shows two 7,200-second timed-out executions. Comparing the
last activity event with metadata-file write time gives approximately 0.3 seconds
and 120 seconds of silence, respectively. These are retrospective observations,
not exact monotonic receipt-time measurements; both are well inside 20 minutes.
Do not copy transcripts, prompts or agent session IDs into the repository.
The pending incident `docs/operacion/incidentes/2026-10-06-systemd-user-control-channel.md`
concerns a completed agent and unavailable systemd observation in Phase 8.9;
this timeout policy does not solve that incident.

## Cursor Rules And Skills

All subplans require `AGENTS.md` and applicable `.cursor/rules/` files:
`ai-dev-loop-governance.mdc`, `ai-dev-loop-orchestrator-contracts.mdc`,
`ai-dev-loop-state-and-schema-contracts.mdc`,
`ai-dev-loop-loop-and-resume-contracts.mdc`,
`ai-dev-loop-abort-contracts.mdc`, `ai-dev-loop-codex-review-contracts.mdc`,
`ai-dev-loop-docs-acceptance-contracts.mdc` and
`ai-dev-loop-global-integrations-contracts.mdc`.
There is no `.cursor/skills/`. Read the planning contract in
`.agents/skills/create-cursor-plan/SKILL.md`; the scheduler's separate read-only
reviewer follows `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`.
Keep both skills unchanged. Update directly affected rules only when their
statements would otherwise contradict the implemented slice.

## Architecture Guardrails

- Ledger and protected immutable artifacts remain authority. Submit/prepare
  freezes policy; retries and successors preserve it without consulting new YAML
  defaults. Never rewrite historical bytes/hashes to manufacture compatibility.
- Activity is observed in the running Cursor stream, not by rereading the entire
  transcript, querying Git, tracing arbitrary subprocesses or polling the model.
  Process helpers transport observations; scheduler services authorize retries.
- Use monotonic receipt time for live decisions; timestamps emitted by Cursor are
  untrusted diagnostics. No transcript content enters status/history summaries.
- Preserve repository reservations during manual waits or uncertain termination.
  Existing admission, staging, review, checkpoint, fencing and abort boundaries
  apply. No continuous baseline policing or speculative Git drift gates.
- Cursor remains implementer; reviewer B is created at the first review and
  resumed by exact identity. Frozen review configuration is `gpt-6.1-sol / high`.

## Implementation Plan

### Approved policy shared by all subphases

The optional configuration block is `workflow.cursor_activity_timeout`:

```yaml
workflow:
  cursor_timeout_minutes: 90
  cursor_activity_timeout:
    soft_timeout_minutes: 120
    inactivity_window_minutes: 20
    hard_timeout_minutes: 360
```

Omission or explicit null selects the existing fixed policy. An object must
provide all three strict positive integers; reject booleans, numeric strings,
floats, missing/null members, unknown keys, `hard <= soft`, or `window > hard`.
No package-wide default change is authorized. The old field remains the bounded
create-chat/preflight budget and fixed-turn fallback; it is not a competing
turn deadline when the activity policy is selected. Adaptive turns require
`stream-json`; incompatible output formats must fail before agent launch.

New CLI flags on submit and sequence prepare are
`--cursor-soft-timeout-minutes`, `--cursor-inactivity-window-minutes` and
`--cursor-hard-timeout-minutes`, supplied as a complete triplet. A layer replaces
the entire policy, rather than mixing partial values. Standalone CLI triplet
wins over repository configuration. Sequence phase manifest policy wins over
global CLI defaults, which win over repository configuration, matching current
sequence precedence. The manifest uses `workflow.cursor_activity_timeout` in
each phase. At a given override layer,
an explicit legacy `cursor_timeout_minutes` selects fixed turns and overrides an
inherited activity policy; same-layer legacy override plus activity object/flags
is rejected. The base YAML may contain both blocks, as above, to keep create-chat
bounded. Preserve the ordinary precedence of unrelated fields.

For a running turn, initialize last activity at successful child launch. Let
`elapsed` and `silent` be monotonic duration and silence in seconds:

```text
if elapsed >= hard: terminate with hard_timeout
elif elapsed >= soft and silent >= window: terminate with inactivity_timeout
else: continue
```

The window begins at last activity, not at the soft threshold. No activity-based
cut occurs before soft. At the hard boundary activity cannot extend execution.
Thresholds are per process attempt, not aggregate turn elapsed time or token
budget. Normal successful exit/completion follows existing ingestion; use
deterministic tests to define an exit coincident with a deadline without turning
an already-completed process into a manufactured timeout.

Qualifying activity is a valid supported event for the bound Cursor chat:
non-empty `thinking/delta`, `thinking/completed`, assistant output, and
`tool_call/started` or `tool_call/completed`. Do not count startup/user echoes,
blank/invalid records, stderr noise, unknown events or connection/retry messages.
The parser retains only the current partial record and scalar receipt state;
reuse observed event forms, preserve raw captured artifacts, and never interpret
tool payloads as instructions. A pending tool alone is not a heartbeat: a
legitimate tool silent longer than the configured window after soft can be cut.
Document that tradeoff; do not add indefinite tool exemptions.

Inactivity timeout retains the existing three automatic retries per logical
turn, 30 minutes apart. Hard timeout adds none and does not consume that counter:
wait with the same run, reservation, chat, prompt/fix envelope, reviewer and review
budget until explicit `scheduler cursor-retry RUN_ID` or abort. Manual retry
authorizes one new attempt with the same frozen thresholds; its hard limit again
requires manual continuation. Historical fixed timeouts keep historical retry
semantics. An activity-policy timeout lacking an authenticated reason is not
eligible for an automatic retry. Do not reinterpret uncertain systemd termination
as a proved hard timeout.

The three-run execution manifest deliberately uses existing schema v1 and
explicit legacy budgets (seven reviews, Cursor 90 minutes, Codex 90 minutes).
It can be prepared by today's runtime and does not require its own unimplemented
feature. All entries freeze at preparation; changing code or YAML later does not
retroactively turn these execution runs into adaptive runs. Cursor model remains
the repository's `grok-4.7-high`, explicitly recorded in each manifest entry.

## Testing Criteria

Require unit/schema, subprocess and production-service integration coverage,
with fake `agent`/`codex`, injected clocks, bounded accelerated subprocess
deadlines and temporary native-WSL homes/XDG/repos. The two motivating regressions
must survive soft while emitting reasoning/tool activity, and silence must cause
cut only when both soft and window are satisfied. Every contract in a subplan
must map to actual production callers and independently asserted outcomes.

Use genuine pre-24 fixtures captured from existing writers at the baseline, with
commit provenance. Do not edit old fixtures to add new policy/reason fields.
Test pending tool silence, connection noise, continuous streams, termination of
descendants, no duplicated output, replay/abort ownership, manual hard-timeout
waits and eventual successful continuation.

## Validation

24.1 and 24.2 run mandatory focused contracts and affected regressions only.
After corrections, rerun failed and affected selections; broaden only for a
concrete remaining risk. 24.3 owns one repository-wide pipeline/integration gate
after its focused contracts pass, not a full-suite rerun on each review round:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
.venv/bin/python -m build
git diff --check
```

Record exact commands/results and executed, failed and unexecuted checks in a
new Phase 24 findings artifact. Missing mandatory behavior/coverage is incomplete
scope. A focused pass is not repository-wide validation. No real workstation
installation or model-backed smoke is required or authorized by these plans.

## Risks Or Recovery Notes

Activity cannot prove useful progress. Silence can represent legitimate long
tests or a stalled provider; the 20-minute cutoff is an explicit chosen tradeoff.
Inactivity retries can still accumulate cost; only the hard-limit automatic
retry prohibition changes here. Do not add a new aggregate cost budget.

Sequence preparation freezes inputs without launching agents or reserving the
repository. Starting it is a separate operator action and authorizes existing
local checkpoints for 24.1 and 24.2 only. 24.3 leaves final changes staged.
The planning-session commit includes the already-pending incident documentation
under the operator's explicit authorization; it does not accept implementation.
Any future `ai_dev_loop.yaml` adoption is control-plane work and requires a final
manual acceptance review; automated-loop acceptance alone is insufficient.

## OpenQuestions

None for timeout behavior or compatibility. Repository activation is optional
operator rollout; implementation does not change `ai_dev_loop.yaml` unless the
operator explicitly adds that scope before the final plan is frozen.
