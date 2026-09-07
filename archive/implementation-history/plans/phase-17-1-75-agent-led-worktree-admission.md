# Phase 17.1.75 — Agent-led worktree admission

## Goal

After Phase 17.1.5 is manually accepted and committed, simplify central
scheduler repository handling. Scheduler submit must record a durable target and
immutable inputs without trying to reproduce Git status semantics.

The first eligible tick owns the one permitted worktree gate: it confirms that
the target is still the same repository and, when workflow.require_clean_worktree
is true, refuses a dirty starting worktree. After admission, repository
reservation, protected artifacts, and explicit Cursor/Codex decisions protect
the loop. The scheduler must not continuously compare a frozen worktree baseline
or emulate Git edge cases between agent turns.

This phase implements the submission-side simplification and defines the
deferred Phase 17.2 admission contract. It does not implement a tick, invoke an
agent, stage, or commit.

## Non-Goals

- Do not weaken repository reservation, immutable plan/prompt/config artifacts,
  agent identity, schema validation, redaction, or non-destructive Git limits.
- Do not accept an arbitrary non-repository directory. Submit must still resolve
  one existing repository worktree root without executing Git or inspecting
  mutable Git state.
- Do not implement initial preflight, scheduler start, scheduler tick, agent
  attempts, staging, review ingestion, correction turns, or commits.
- Do not execute or silently reinterpret a currently queued run, or delete its
  preserved artifacts.
- Do not make this phase a Git library, status monitor, or compatibility layer
  for index, pack, ignore, attributes, rename, or configuration semantics.

## Scope

- Replace the filesystem-only Git status dependency with a narrow,
  side-effect-free repository-target resolver. It may walk upward to locate a
  .git directory or linked-worktree .git file and return the canonical worktree
  root. It must not read HEAD, refs, index, packs, Git configuration, ignores,
  attributes, or status data.
- Persist only target root and deterministic worktree reservation key in the
  submitted repository binding. Remove submission-time branch, HEAD,
  git-dir/common-dir, porcelain status, staged-path, and baseline-status
  artifact dependencies.
- Remove the custom repository_binding.py status/index/object/config parser and
  replace its tests with narrow repository-target resolver coverage.
- Evolve the submitted context/schema and central-ledger migration according to
  the compatibility policy selected and implemented by Phase 17.1.5. New
  snapshots omit stale baseline fields; pre-existing rows and artifacts remain
  untouched and inspectable under that policy.
- Amend the master and Phase 17.2 plan so its first preflight performs one real
  Git admission check before first Cursor launch: verify resolved root, capture
  branch/HEAD/status as an auditable artifact/event, and enforce
  require_clean_worktree only there. It must not create a recurring
  baseline-comparison gate after admission.
- Update factual scheduler CLI, state-layout, safety, and troubleshooting
  documentation to distinguish submission target binding from initial worktree
  admission.

## Out of Scope

- ai_dev_loop.yaml, hook installation, global skills, desktop bridges, and
  user-global Git configuration.
- Any change to Phase 17.1.5 fresh-reviewer contract, migration decision, or
  legacy A/B and PR-review-v2 behavior.
- Changing public Git behavior outside the new scheduler submission path.
- Git status, git rev-parse, subprocess probes, model invocation, or
  target-repository mutation during scheduler submit.
- Continuous post-admission dirty-worktree, branch, HEAD, index, staged-path,
  or porcelain-baseline checks. Later phases may validate a specific staged
  patch submitted for review; that is agent-decision integrity, not generic
  worktree policing.
- Autonomous commit, push, reset, clean, stash, checkout/switch, unstage,
  merge, rebase, tag, or target-repository cleanup.

## Required Context

Read before editing:

- AGENTS.md; Phase 17 master; Phases 17.1, 17.1.5, and 17.2. Phase 17.1.5 must
  be complete and its submitted-context/ledger compatibility strategy is binding.
- Scheduler submission, domain state/events, SQLite store/migrations, protected
  artifacts, schemas, and CLI projection code.
- scheduler infrastructure repository_binding.py; runners/git.py; and legacy
  commands/start_preflight.py. Use the legacy command only to characterize
  deferred admission; do not wire a tick in this phase.
- Scheduler unit/integration tests, migration fixtures, and linked-worktree
  fixtures.
- Current CLI, configuration, artifact-state, safety, and troubleshooting docs.

## Cursor Rules And Skills

Follow all repository rules:

- .cursor/rules/ai-dev-loop-governance.mdc
- .cursor/rules/ai-dev-loop-orchestrator-contracts.mdc
- .cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc
- .cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc
- .cursor/rules/ai-dev-loop-abort-contracts.mdc
- .cursor/rules/ai-dev-loop-codex-review-contracts.mdc
- .cursor/rules/ai-dev-loop-global-integrations-contracts.mdc
- .cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc
- AGENTS.md

Use .agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md for factual
documentation and manual-acceptance material. No .cursor/skills directory
exists. Do not invoke a staged-review skill while implementing this phase.

## Architecture Guardrails

- Submission boundary: scheduler submit remains no-agent and no-Git-CLI. It may
  validate only path shape/confinement, an existing repository marker, immutable
  inputs, Phase 17.1.5 bindings, and reservation/idempotency data.
- Minimal repository target: resolve a canonical worktree root without loading
  mutable Git semantics. A linked-worktree .git file is a marker, not permission
  to parse its Git directory. The root-derived worktree_key remains the conflict
  and reservation key.
- Preflight ownership: Phase 17.2 owns the first real Git admission check and
  its audit artifact/event. It runs once before initial Cursor launch, respects
  frozen require_clean_worktree, reports a specific blocked action, and never
  rewrites immutable submitted context.
- Agent-led execution: after admission, Codex and Cursor decide reviewed work,
  staging, corrections, and any explicitly phase-authorized commit. Preserve
  reservation, immutable-input, artifact, attempt-identity, and specifically
  reviewed staged-patch guarantees; do not add recurring baseline equality,
  status emulation, or speculative drift refusal.
- State/migration: use a strict schema and audited migration/checksum path
  consistent with completed Phase 17.1.5. Never hand-edit SQLite, mutate
  historical snapshots, silently reinterpret a queued run, or delete old
  baseline artifacts.
- No destructive broadening: this phase must not stage, commit, unstage, reset,
  clean, stash, switch, merge, rebase, tag, push, or invoke a model.
- Privacy: normal output remains redacted. Do not add raw Git status, branch,
  HEAD, prompts, patches, or agent output to normal status/list output.

## Implementation Plan

1. Confirm Phase 17.1.5 is committed and manually accepted. Read its final
   submitted-context version and explicit non-empty-ledger policy. Stop if that
   decision is absent; do not invent a second migration policy.
2. Replace GitRepositoryInfo at the submission boundary with a typed repository
   target containing canonical worktree root and root-derived reservation key.
   Implement a local root resolver that walks upward from repo-path or CWD to an
   existing .git directory or file. Preserve plan/prompt confinement.
3. Remove discover_repository_binding, all Git object/index/pack/config/
   porcelain parsing, and baseline-status artifact handling from scheduler
   submit. Delete only tests proving retired parser behavior; replace them with
   target-root and no-subprocess coverage. Do not alter the legacy Git runner or
   preflight implementation.
4. Introduce the next submitted-context/schema version under the Phase 17.1.5
   policy. Remove frozen branch/HEAD/git-dir/common-dir and baseline references
   from new contexts, idempotency data, manifests, projections, and validators.
   Retain canonical root and reservation identity. Add the approved migration or
   refusal/read-only path; historical rows and artifacts remain intact.
5. Update master and Phase 17.2 plan wording to make initial preflight a durable
   admission effect: Git root equals submitted target; branch/HEAD/status become
   a protected admission artifact; require_clean_worktree is enforced; an
   admission event/checkpoint is appended; no later equality comparison occurs
   only because agents changed the worktree.
6. Update docs and CLI help where behavior changes. Submit freezes a repository
   target, not a Git baseline; queued runs are not admitted; future first tick
   performs the clean-worktree gate. Keep legacy prepare/start documentation
   separate and factual.
7. Review every changed projection, schema, migration, and test for redaction,
   side-effect freedom, idempotency, reservation conflict, path safety, and
   Phase 17.1.5 compatibility. Do not hide a Git CLI call or watcher in submit.

## Testing Criteria

- Unit, repository target: root and nested-path discovery; linked-worktree .git
  files; missing/invalid markers; canonical containment; stable root-derived
  worktree key. Prove no process helper or status/index/object/pack read occurs.
- Unit, domain/schema/migration: strict new context; extra-field rejection;
  meaningful identity changes; no baseline fields in new snapshots; migration
  checksum/rollback; final Phase 17.1.5 policy for prior rows. Assert old
  artifacts are never deleted or rewritten.
- Integration, submit: disposable clean, dirty, renamed, staged, and linked
  worktrees all exercise the same no-subprocess submit boundary. Their Git state
  must not affect submission except normal immutable inputs/reservation
  conflicts. Assert no git/baseline-status.txt is written for a new run.
- Integration, contracts: retain duplicate reuse, target conflict, stdin-once,
  protected artifact integrity, redacted status/list, and no legacy run-root
  writes. Use fake session/runtime input required by completed Phase 17.1.5; do
  not invoke real Cursor, Codex, Git, network, or systemd.
- Deferred regression: add only plan-level or narrowly unit-testable assertions
  for the Phase 17.2 admission artifact interface. Full preflight behavior
  belongs to Phase 17.2 with a fake Git runner.

## Validation

Run focused tests, then:

~~~bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler \
  tests/integration/test_phase17_1_submit.py \
  tests/unit/test_codex_integration_assets.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
git diff --check
~~~

Do not run a real scheduler tick, preflight, Cursor, Codex, systemd, GitHub, or
target-repository mutation as automated validation.

## Risks Or Recovery Notes

The primary risk is replacing excessive worktree policing with missing admission
control. Prevent that by making Phase 17.2 one-time preflight an explicit,
durable effect before initial Cursor launch, not by retaining a partial Git
emulator in submission.

The second risk is persistence incompatibility after Phase 17.1.5. Follow its
accepted migration/refusal decision exactly. New contexts may omit baseline
fields; existing rows and protected baseline artifacts remain recoverable and
untouched. If the policy cannot safely accommodate the new version, stop with a
precise operator action.

A reservation protects against another scheduler run, not every human edit.
That is intentional after admission: reviewers and implementers decide what is
accepted. Specific staged-patch integrity remains a review boundary, but generic
worktree drift is not a scheduler failure condition.

## OpenQuestions

None. This phase has a hard prerequisite: Phase 17.1.5 must be accepted and
committed, including its explicit schema/ledger compatibility decision. If that
decision is unavailable at implementation time, stop before dependent changes.

