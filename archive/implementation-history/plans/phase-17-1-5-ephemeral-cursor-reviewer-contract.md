# Phase 17.1.5 — Ephemeral Cursor reviewer contract

## Goal

Amend Phase 17 before Phase 17.2 so scheduler runs no longer require a
pre-existing or resumable Codex reviewer session (B). A scheduler run must bind
controller A exactly, freeze a review-only Cursor CLI profile plus immutable
review inputs, and later create one fresh reviewer attempt for each review pass.

The reviewer is not a dispatcher and never implements or corrects code. It
reads the approved plan and the complete staged implementation, performs only
the authorized review/validation work, and returns a schema-validated decision
that the scheduler can use to drive the existing Cursor implementation chat.

This phase changes the *contract* and prepares later Phase 17 plans. It does
not make any tick launch a real reviewer. Phase 17.1 must first be manually
validated and committed; this phase is then committed on top of it. Phase 17.2
starts only after both commits are integrated in the same baseline.

## Non-Goals

- Do not re-run, recover, alter, or delete the historical Phase 17.1 A/B runs.
- Do not launch Cursor, Codex, systemd, a tick, preflight, Git staging, or a
  review attempt.
- Do not implement the Phase 17.3 process backend, Phase 17.4 implementation
  turns, or the Phase 17.5 review/fix loop.
- Do not make a reviewer dispatch a Cursor implementation/correction process,
  commit, push, reset, clean, stash, unstage, or mutate the target repository.
- Do not preserve the legacy local `prepare`/`launch` A/B workflow by silently
  changing its reviewer identity. It retains its documented exact-session
  behavior until the Phase 17.7 cutover.
- Do not read, copy, bridge, or write Codex Desktop/WSL session rollouts or
  SQLite state for scheduler submission.

## Scope

- Replace the Phase 17 scheduler-only B Codex-session/runtime binding with a
  typed, immutable fresh Cursor-reviewer binding. `scheduler submit` requires
  only A's exact controller session ID; it must not accept, read, or infer a B
  session ID.
- Freeze the review provider/profile, plan/prompt/config hashes, repository
  identity, baseline, limits, and immutable reviewer-input contract in the
  central ledger and protected artifact tree. The idempotency identity changes
  with every frozen reviewer-relevant value.
- Add the explicit central-schema/version migration or incompatibility path
  selected in `OpenQuestions`; never hand-edit JSON snapshots or SQLite rows.
- Revise the Phase 17 master and future Phase 17.2–17.7 plans, all applicable
  Cursor rules, package-owned handoff/controller instructions, and factual
  submission/configuration docs so they distinguish legacy exact-session A/B
  from the new scheduler fresh-reviewer contract.
- Define the precise deferred handoff into Phase 17.5: one fresh, review-only
  Cursor attempt per review pass; no `--resume`, no reviewer chat continuity,
  no `--last`, no agent-created workflow dispatch, and structured results only.

## Out of Scope

- `scheduler start`, tick leadership, reservations, effects, attempts, systemd
  units, abort, retry, Cursor chat creation, staging, review result ingestion,
  correction execution, and terminalization.
- Any change to PR-review v2's existing Codex session contract.
- User-global installation, hook trust, desktop bridges, or actual updates to
  the user's installed skills. Package assets may change, but their installation
  remains a separately authorized manual acceptance action.
- A legacy scheduler-state importer, automatic conversion of a submitted
  session-bound scheduler run into a fresh-reviewer run, or automatic deletion
  of obsolete state.

## Required Context

Read before editing:

- `archive/implementation-history/plans/phase-17-tick-based-central-run-scheduler.md`,
  Phase 17.1, and Phases 17.2–17.7, especially 17.4 and 17.5.
- All eight `.cursor/rules/*.mdc`, which must be revised only where the
  scheduler fresh-reviewer decision supersedes an exact-session statement.
- `src/ai_dev_loop/scheduler/{domain,application,infrastructure}/`,
  `commands/scheduler.py`, `cli.py`, `config.py`, scheduler schemas/migration,
  and `tests/unit/scheduler/` plus `tests/integration/test_phase17_1_submit.py`.
- `runners/cursor.py`, `runners/codex.py`, `review_result.py`,
  `response_schema.py`, `workflow_engine.py`, `review_runtime.py`, and the
  legacy launch/recovery code. Characterize them; do not change legacy behavior
  unless this plan explicitly says so.
- `src/ai_dev_loop/integrations/codex/{assets.py,skill/SKILL.md,controller_skill/SKILL.md}`
  and `tests/unit/test_codex_integration_assets.py`.
- `ai_dev_loop.yaml`, `docs/referencia/{cli,configuracion}.md`,
  `docs/operacion/{estado-artefactos,seguridad-privacidad,troubleshooting}.md`,
  and the supplied reference
  `/home/rojobad/Projects/celatex360-platform/.codex/agents/cursor-supervisor.toml`.

## Cursor Rules And Skills

Follow all eight repository rules:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

The current exact-Codex-session language is intentionally superseded only for
the Phase 17 central scheduler by the approved fresh-reviewer decision. Keep
the legacy local A/B and PR-review v2 contracts explicit and unchanged. Use
`.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md` for factual
documentation and manual-acceptance material. Do not invoke either staged-review
skill while implementing this phase.

## Architecture Guardrails

- **Authority:** SQLite snapshots/events/effects and hash-verified protected
  artifacts remain the sole scheduler truth. No `state.json`, session rollout,
  mutable config reread, chat transcript, or log becomes a second authority.
- **Identity:** controller A's exact session ID remains the authorization key.
  Scheduler B has no pre-existing session ID, no fork, no `codex exec resume`,
  and no `--last`. A fresh reviewer attempt is identified by its durable
  attempt/effect identity in later phases, not by a reusable conversation.
- **Provider separation:** implementation Cursor continuity remains exactly one
  persisted Cursor chat per run. The review-only Cursor process is separate and
  must never create, resume, or alter that implementation chat.
- **Read-only review:** a reviewer must not run in a target worktree unless the
  selected invocation provides a verified non-mutating boundary. A prompt alone
  is insufficient. Resolve the isolation design before adding executable review
  code; fail closed on unsupported capability or unexpected repository mutation.
- **Structured decisions:** a review result must be versioned and schema plus
  cross-field validated. Markdown, stream transcripts, and reviewer prose are
  audit artifacts only; none may decide pass/fail, findings, severity, test
  status, or the Cursor fix prompt.
- **Prompt ownership:** B's reviewed inputs are the frozen plan, exact original
  Cursor prompt, frozen staged-patch artifact, and bounded execution metadata.
  B may author a fix prompt only in a validated structured result. The scheduler
  may persist and forward it byte-for-byte but never rewrite it.
- **Process safety:** later systemd units use argv arrays, explicit CWD, bounded
  stdin/output, process-group/cgroup timeout/abort ownership, redacted metadata,
  and protected artifacts. A tick never waits. This phase only defines those
  future contracts and must not approximate them with a detached local child.
- **Compatibility:** do not reinterpret submitted v1 session-bound contexts as
  fresh-reviewer contexts. Preserve them read-only or fail the explicit upgrade
  path chosen below; no automatic migration or cleanup.
- **Control plane:** any YAML/schema, package skill, master-plan, or integration
  asset change requires human acceptance in addition to fake-backed tests. Do
  not install skills or edit hook trust during automated validation.

## Implementation Plan

1. Resolve every `OpenQuestions` item and record the decisions in this plan
   before changing code. Stop if a Cursor CLI capability cannot supply the
   required review isolation or a schema-valid result contract.
2. Amend the Phase 17 master and Phase 17.2–17.7 plans to replace only central
   scheduler B-session assumptions with the fresh review-only Cursor contract.
   Keep the phase order: 17.1.5 precedes 17.2; actual reviewer launch remains
   in revised 17.5. State the legacy A/B and PR-review v2 exceptions explicitly.
3. Introduce the selected typed `FreshReviewerBinding` and versioned submitted
   context/state schema. Remove scheduler-only `CodexRuntimeBinding`, session
   runtime artifacts, B-ID equality validation, B-ID CLI option, B-ID redacted
   projections, and session-derived review-runtime resolution from the central
   submit path. Retain controller A binding and frozen repository/plan/prompt/
   configuration/baseline/limits.
4. Add a migration-audited database/version strategy. It must be transactional,
   checksum tested, and preserve all pre-existing artifacts. If the approved
   strategy refuses non-empty v1 scheduler ledgers, return a precise safe action
   without changing them; if it supports read-only v1 inspection, make execution
   refusal explicit. Never update snapshot JSON by ad-hoc SQL.
5. Define the protected review-input/result artifact names, size limits,
   permissions, redaction rules, deterministic wrapper prompt template, and
   reviewer result schema version needed by revised Phase 17.5. The template
   must require plan reading, complete staged-change review, no implementation,
   no dispatch, and no Git history/index mutation. Do not wire a process launch
   in this phase.
6. Update configuration models/schema/defaults/overrides only as required by the
   approved Cursor reviewer profile. Freeze every execution-relevant selected
   value at submit; no later tick may inherit a mutable CLI default. Keep
   implementation-Cursor settings distinct from reviewer settings when their
   safety requirements differ.
7. Update package-owned handoff/controller assets and their tests so the new
   scheduler flow does not create or message B. Preserve the installed legacy
   handoff behavior until cutover, or route the two flows by explicit command
   name with no ambiguity. Do not modify real user-global files.
8. Update factual CLI/config/artifact/privacy/troubleshooting docs to say that
   `scheduler submit` needs A but no reviewer session and does not run a
   reviewer. Do not claim that a fresh reviewer is executable until revised
   Phase 17.5 passes its tests. Add a manual control-plane acceptance checklist.

## Testing Criteria

- **Unit/schema:** prove model/schema alignment, strict extra-field rejection,
  canonical idempotency changes when the frozen reviewer profile changes, no
  full identifiers in projections, and protected artifact path/hash/permission
  validation. Cover malformed reviewer profile, missing/oversized review inputs,
  and invalid structured-result payloads with fakes only.
- **SQLite/migration:** fresh bootstrap, migration checksum, newer-version
  refusal, transactional rollback, and the selected non-empty-v1 behavior.
  Prove old v1 snapshots are never silently reinterpreted or modified.
- **Command/integration:** disposable repository plus temporary HOME/XDG proves
  scheduler submit reads stdin once, invokes no session-runtime reader,
  subprocess, probe, model, Git mutation, or reviewer; exact duplicate reuses;
  a changed reviewer profile creates a distinct identity; worktree conflicts,
  status, and list stay deterministic and redacted.
- **Future-contract regressions:** fake Cursor reviewer fixtures characterize
  fresh/no-resume argv construction, no implementation-chat ID, deterministic
  wrapper content, schema validation, protected outputs, and fail-closed
  isolation. They must not run real Cursor or Codex and must not expose a review
  effect before Phase 17.5.
- **Compatibility/control-plane:** legacy `prepare`/`launch` A/B and PR-review
  v2 tests preserve their exact-session behavior. Package asset tests assert
  the scheduler instructions do not request a fork/B session while legacy
  instructions remain explicit. Docs tests, if present, stay factual.

## Validation

Run focused tests, then:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler \
  tests/integration/test_phase17_1_submit.py \
  tests/unit/test_codex_integration_assets.py \
  tests/unit/test_controller_launch.py \
  tests/integration/test_launch_worker.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
git diff --check
```

Do not run real Cursor, Codex, systemd, GitHub, session bridge, skill
installation, or target-repository mutation as automated validation.

## Risks Or Recovery Notes

The demonstrated active-writer failure is evidence that a pre-existing Codex
thread cannot be a scheduler-owned detached reviewer. The correction must not
replace that failure with an unconfined Cursor process that can edit the target
repository or an unstructured Markdown parser.

The Phase 17.1 commit and its staged-patch artifacts remain independently
recoverable/manual-reviewable. This phase changes future scheduler submission
only. Central v1 scheduler data, if any, must be preserved untouched according
to the selected explicit migration/refusal policy. A user who has manually
validated and committed 17.1 may merge this phase's commit afterward; 17.2 then
uses the combined HEAD.

## OpenQuestions

1. **Cursor review isolation:** What verified non-mutating boundary will the
   fresh Cursor CLI reviewer use: a documented native read-only mode (only if
   the installed CLI probe proves it), or a scheduler-built disposable review
   snapshot/worktree outside the target worktree? The current implementation
   Cursor profile permits mutable execution, so its settings cannot be reused
   blindly for B.
2. **Structured Cursor result:** Which supported Cursor CLI mechanism produces
   the final schema-valid review result: a native output-schema capability, or
   a strict JSON payload in the final structured stream event with fail-closed
   validation? The chosen mechanism determines the frozen reviewer profile and
   the result artifact/parser contract.
3. **Central v1 runs:** On upgrade, should a non-empty Phase 17.1 scheduler
   ledger be rejected with a non-destructive re-submit/export instruction, or
   should v1 rows remain readable for status only while execution is blocked?
   No automatic conversion is permitted.

