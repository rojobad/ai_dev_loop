# Phase 21 — Complete Local Integration for Remote Supervision

Status: proposed execution plans, not implemented features or authorization to
launch agents. Planning baseline: `d1d8960`, inspected on 2026-09-18.

## Agreed outcome

Deliver the entire local supervision surface needed by the future Web UI:
runs, sequences, multiple runs per phase, actual timelines, frozen plans and
initial prompts, reviewer prompts/results/findings/correction prompts,
per-attempt stdout/stderr during and after execution, final reports, and Codex
capacity. Subphases are review units; partial supervision is not the requested
first usable release. Work is limited to this repository.

The source discussion was the user's `ai_dev_tool_web_ui.md`. Its executable
local scope and subsequent user decisions are captured here and in the six
self-contained plans; no implementation depends on that external file or its
unresolvable chat citations. These artifacts specify concrete proposed wire
choices for review; they do not claim those choices already existed upstream.

## Closed decisions and scope

- ai_dev_loop remains local execution/state/artifact authority. The future
  Bridge calls typed local CLI operations, never SQLite or private paths.
- Compatibility uses only major: same major accepted, different major means
  UPDATE_REQUIRED and no further resource queries. Minor additions are backward
  compatible; no per-minor adapters. Capabilities only enable additive functions.
- Single operator, OS-user local boundary. No new local auth/roles, account
  registry, telemetry storage, HTTP daemon, remote shell or remote infrastructure.
- Full supervision is required. Remote mutations (start/abort/retry/relaunch)
  remain outside Phase 21, as in the source document's later control stage.
- Historical content that was never recorded is explicitly unavailable, not
  recreated. Existing summary privacy remains; explicit content inspection is
  sensitive and on demand. Current safe path/hash boundaries are preserved.
- Use existing resource IDs, validated scheduler logic, and actual Phase 20.9
  lineage; no replacement scheduler or speculative Git controls.

## Subphases and dependencies

Execute in numeric order. Each plan has its own short Cursor prompt and mandatory
contract/test mapping. A later plan requires accepted earlier implementation;
the existence of these files is not evidence a prerequisite was implemented.
Do not launch or submit anything as part of authoring these plans.

The six prompt_*.txt files are created alongside their plans. The existing
.gitignore intentionally excludes these execution prompts because ai_dev_loop
snapshots them outside the repository. Preserve that convention; this planning
change does not force-add prompts or change Git tracking policy.

| Phase | Delivery | API | Plan | Cursor prompt |
|---|---|---|---|---|
| 21.1 | Contract, schemas, info and errors | 1.0 | [phase-21-1-local-integration-contract.md](phase-21-1-local-integration-contract.md) | [prompt](prompt_phase-21-1-local-integration-contract.txt) |
| 21.2 | Runs, attempts, history and frozen inputs | 1.1 | [phase-21-2-run-inspection.md](phase-21-2-run-inspection.md) | [prompt](prompt_phase-21-2-run-inspection.txt) |
| 21.3 | Sequences, lineage, phase inputs and report | 1.2 | [phase-21-3-sequence-inspection.md](phase-21-3-sequence-inspection.md) | [prompt](prompt_phase-21-3-sequence-inspection.txt) |
| 21.4 | Exact reviewer evidence and inspection | 1.3 | [phase-21-4-review-evidence.md](phase-21-4-review-evidence.md) | [prompt](prompt_phase-21-4-review-evidence.txt) |
| 21.5 | Live child stdout/stderr and bounded reads | 1.4 | [phase-21-5-process-output.md](phase-21-5-process-output.md) | [prompt](prompt_phase-21-5-process-output.txt) |
| 21.6 | Capacity and complete fake-client acceptance | 1.5 | [phase-21-6-capacity-and-integration-acceptance.md](phase-21-6-capacity-and-integration-acceptance.md) | [prompt](prompt_phase-21-6-capacity-and-integration-acceptance.txt) |

The source document numbered reviews/output/sequences as 21.3/21.4/21.5.
This set intentionally numbers sequences/reviews/output as 21.3/21.4/21.5 so
the existing multi-run resource model is established before its detailed
evidence. No feature was removed. Versions 1.0–1.5 identify additive delivered
slices, not compatibility branches; final capability inventory is all true.

## Changes established by code inspection

- Current repository already implements Phase 20.9, including same-reviewer
  successor runs and accepted/current lineage. This is baseline, not a future
  recovery feature requiring a new schema in Phase 21.
- Current timeline SQL selects attempt IDs, while public summaries often omit
  or shorten IDs. Integration needs full navigation keys without changing old
  scheduler CLI output.
- Existing artifact read helpers can create directories or change permissions.
  A strictly read-only integration path must avoid those ensure_* entry points.
- Exact final Codex stdin is not saved today. Its capture must be per attempt,
  before launch, and include the operational retry prefix. Iteration-only
  correction artifacts may be overwritten; use each validated result's field.
- Current unlimited Cursor subprocess capture writes files on completion;
  bounded Codex capture can buffer writes. Live inspection requires a narrow
  opt-in incremental/flush change, not merely a file reader.
- Capacity observation currently stores only status/reason. Diagnostics must
  be added to the existing validated producer, not inferred from run state.

## Public interface choices

The six plans are authoritative for exact fields and behavior. Key shared
choices are JSON-only integration commands, typed success/error envelopes,
camelCase DTOs, full existing resource IDs, offset/limit pagination, base64 byte
chunks (decode after joining or incrementally), explicit unavailable reasons,
and observedAt on every response. Base64 preserves stored bytes and makes byte
offset semantics unambiguous without UTF-8 boundary repair in the server.

Final command inventory (leaf commands accept `--output json`):

```text
integration info
integration runs list
integration run inspect RUN_ID
integration run attempts RUN_ID
integration run timeline RUN_ID
integration run history RUN_ID
integration run plan RUN_ID
integration run initial-prompt RUN_ID
integration sequences list
integration sequence inspect SEQUENCE_ID
integration sequence phase-runs SEQUENCE_ID --ordinal N
integration sequence phase-plan SEQUENCE_ID --ordinal N
integration sequence phase-prompt SEQUENCE_ID --ordinal N
integration sequence report SEQUENCE_ID
integration run reviews RUN_ID
integration run review RUN_ID --attempt ATTEMPT_ID
integration run review-content RUN_ID --attempt ATTEMPT_ID --kind KIND
integration run output RUN_ID --attempt ATTEMPT_ID --stream stdout|stderr
integration codex-capacity
```

List/history/attempt/review collections use limit 100/max 500 and documented
ordering. Content reads default to 64 KiB/max 256 KiB per response. Detailed
validated review response is bounded by its existing 1 MiB capture contract;
the potentially larger prompt always uses chunks. No generic filesystem API.

## Final acceptance

The test Bridge client must obtain every resource/content reference via the
public CLI; only test setup may use local services/storage. It must prove
zero/one/multiple phase runs, exact frozen inputs, all review evidence, live and
completed streams, residual risk/final report, unavailable historical content,
capacity observations and major mismatch refusal. Existing supported scheduler
workflow/tests continue to pass. Tests use fake models/provider subprocesses;
live provider validation and Web UI deployment are not claimed.

Each execution reports contract ID → production path → test → exact validation
result. Missing mandatory behavior/tests remain incomplete work. Existing
review rules retain finding IDs and require all confirmed findings in the
first review, without promoting optional hardening into new scope.

## OpenQuestions

None. Public behavior introduced by these plans is stated explicitly rather
than left for Cursor to infer. If admission finds a missing prerequisite or
conflicting accepted contract, record the evidence and stop only dependent
work for a concrete decision; do not silently broaden the implementation.
