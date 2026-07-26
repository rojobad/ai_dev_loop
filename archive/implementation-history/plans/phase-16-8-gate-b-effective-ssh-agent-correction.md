# Phase 16.8 Gate B Effective SSH-Agent Correction

## Goals

- Correct the `pr-review-v2` Git publication preflight so it honors the
  effective OpenSSH `IdentityAgent` configured for the bound SSH remote.
- Preserve the fail-closed requirement that an SSH agent contains a usable
  identity before a v2 push, while propagating the verified socket to the
  bounded Git transport used for that push.
- Reproduce the Gate B failure entirely offline: `SSH_AUTH_SOCK` is absent
  from the detached worker environment, yet `ssh -G` resolves a valid
  `IdentityAgent` socket with a loaded identity.
- Preserve existing v1 effective-agent behavior and allow the paused existing
  Gate B run to be resumed only after this correction has passed staged review
  and the local CLI is updated.

## Non-Goals

- Do not weaken SSH-agent preflight, remove it, or classify a missing/unusable
  agent as a transient write failure.
- Do not change remote identity checks, non-force push fencing, reconciliation,
  retry counts, leases, claims, supervisor lifecycle, or PR review state.
- Do not alter user WSL/OpenSSH/agent configuration, ask the operator to export
  a socket manually, or add a socket path to public project configuration.
- Do not change legacy `pr-review` behavior except a minimal shared-helper move
  that preserves its current effective-agent semantics exactly.
- Do not declare Gate B or Phase 16.8 complete in this correction.

## Scope

Expected production scope:

- `src/ai_dev_loop/pr_review_v2/infrastructure/git_write_transport.py`
- `src/ai_dev_loop/pr_review_v2/infrastructure/git_publication_gateway.py`
- the narrowest neutral/shared SSH-agent helper location needed to reuse the
  existing v1 `ssh -G`/`IdentityAgent` parsing safely, potentially moving the
  generic code now in `src/ai_dev_loop/runners/publish.py` without changing
  v1 behavior.
- Direct protocol/fake updates only where the changed preflight contract needs
  them.

Expected test scope:

- `tests/unit/pr_review_v2/test_git_write_transport.py`
- `tests/unit/pr_review_v2/test_git_publication_gateway.py`
- `tests/unit/test_phase15_14_effective_ssh_agent_preflight.py`
- existing write-helper fakes and the narrowest v2 Git-publication integration
  test(s), only when required to demonstrate the production boundary.

## Out of Scope

- The live acceptance checkout
  `/home/rojobad/Projects/parish360-poc-phase16-8-acceptance`.
- GitHub PR `rojobad/parish360-poc#4`, its branches, comments, commits, or
  reviews.
- The live run `prv2-617edf93c28019564a2a5d51653ba1a1`, including its XDG
  database, protected artifacts, processes, claims, leases, and launcher
  metadata.
- Any real GitHub, Git remote, Cursor, Codex, model, network, SSH-agent, or
  external CLI authentication call in production mode.
- `ai_dev_loop.yaml`, public schemas, secrets, shell profiles, `~/.ssh`, or
  user environment files.
- Commit, push, install, start, resume, abort, or any Git-state operation.

## Required Context

Read before implementation:

1. This complete plan.
2. `archive/implementation-history/plans/phase-16-8-pr-review-v2-resilience-and-live-acceptance.md`.
3. `archive/implementation-history/plans/phase-16-8-gate-b-supervisor-lease-runtime-correction.md`.
4. `src/ai_dev_loop/pr_review_v2/infrastructure/git_write_transport.py`.
5. `src/ai_dev_loop/pr_review_v2/infrastructure/git_publication_gateway.py`.
6. `src/ai_dev_loop/pr_review_v2/application/write_contracts.py`.
7. `src/ai_dev_loop/runners/publish.py`, especially its effective
   `IdentityAgent` resolution and preflight helpers.
8. The test files named in Scope and the current v2 transport protocol/fakes.
9. Every applicable `.cursor/rules/*.mdc` file.

Sanitized Gate B evidence defining the regression:

- The remote is SSH (`git@github.com:rojobad/parish360-poc.git`).
- The user's effective OpenSSH configuration resolves an `IdentityAgent` socket
  through `ssh -G`; that socket is intentionally not named in source, tests,
  logs, or surfaced errors.
- The detached v2 worker does not inherit `SSH_AUTH_SOCK`.
- v2 currently calls `ssh-add -l` using only its minimal inherited environment,
  so it blocks the push with `no usable SSH agent identity for push` before
  attempting the write.
- Existing v1 publication resolves `IdentityAgent` first and invokes
  `ssh-add -l` with the resolved socket. Thus v1 and ordinary SSH Git use the
  user's established agent configuration successfully.

Cursor must use this sanitized evidence only. It must not inspect the live run,
the test repository, shell environments, sockets, or protected logs.

## Cursor Rules And Skills

Read and obey every applicable rule:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

No repository-local Cursor skill is required for this narrow correction. Stop
and report an `OpenQuestions` item before dependent work if the safe shared
helper boundary or the existing contracts cannot support the change without an
architectural decision.

## Architecture Guardrails

- The remote URL and configured remote name remain the authoritative binding;
  never accept an agent socket from model output, a PR payload, persistent run
  state, public configuration, or an unvalidated command string.
- Resolve the effective agent only through a direct-argv `ssh -G` call for the
  validated SSH destination derived from the already-bound remote URL. Preserve
  all current option/whitespace/control-character validation.
- An effective valid `IdentityAgent` takes precedence over inherited
  `SSH_AUTH_SOCK`; if it is absent or `none`, retain the current inherited-socket
  fallback behavior.
- Verify the selected socket with `ssh-add -l` in a bounded, minimal,
  allowlisted environment. Never log, persist, return, or embed the socket
  path, environment, agent output, key fingerprints, remote URL, or stderr in
  an operator-visible error.
- Once preflight succeeds, the same selected socket must be available to the
  subsequent bounded v2 Git subprocesses for the affected transport. Do not
  mutate global `os.environ`; keep it instance/call scoped and testable.
- A missing, malformed, non-socket, unsafe, or identity-less effective agent
  fails closed as the existing typed authentication block. Do not fall back to
  a different identity after a configured effective agent fails.
- Reuse or extract the existing generic v1 effective-agent parsing instead of
  implementing a second divergent parser. The reusable module must remain
  infrastructure-neutral; v2 must not depend on legacy PR-review commands or
  state machinery.
- Keep direct argv, `shell=False`, timeouts, process-group cleanup, minimal env
  allowlisting, non-force push proof, remote identity validation, claim/lease
  fencing, and reconciliation unchanged.
- Tests use injected process runners and temporary paths only. No real agent,
  SSH, GitHub, Git network, user socket, or environment inspection is allowed.
- This is a control-plane/runtime correction: external staged A/B review is
  required after automated validation. Cursor must not modify Git state.

## Implementation Plan

### 1. Establish the exact v2 regression at the transport boundary

- Add a focused test where the v2 transport has no inherited `SSH_AUTH_SOCK`.
- Script the validated `ssh -G` query to return one usable `identityagent`
  value, and script `ssh-add -l` to succeed only when its environment contains
  that value.
- Drive the real v2 preflight path used by `GitPublicationGateway`, rather than
  testing a standalone parser only.
- Assert preflight succeeds, no socket/path/key material is exposed in errors
  or recorded command arguments, and the selected socket reaches the later
  bounded Git push call.
- Make this test fail against the current implementation, which calls
  `ssh-add -l` with the missing inherited environment.

### 2. Share the effective-agent resolver without coupling v2 to legacy review

- Extract or reuse the generic, already-tested destination parsing,
  `ssh -G` output parsing, effective-agent precedence, socket usability, and
  `ssh-add` verification behavior from v1 through an infrastructure-neutral
  helper.
- Preserve v1 public behavior and existing Phase 15.14 test coverage. Do not
  import a legacy PR-review command/controller module into v2.
- Use injected executable/process boundaries where current tests need them;
  preserve direct argv and bounded execution.
- Keep the helper's errors typed and privacy-safe. Translate only at the v2
  gateway/transport boundary to its existing authentication-block vocabulary.

### 3. Wire the resolved socket through v2 preflight and Git transport

- Extend the narrowest existing v2 transport/gateway contract so the preflight
  has the bound remote URL or remote name needed to resolve the effective
  agent. Update protocol fakes deliberately rather than bypassing the gateway.
- Ensure `ssh-add -l` receives the effective socket even with no inherited
  `SSH_AUTH_SOCK`.
- After successful validation, use that same socket in the transport's bounded
  per-instance environment for the subsequent Git operations. Do not expose it
  through a public API or mutate process-global state.
- Retain inherited `SSH_AUTH_SOCK` behavior when no `IdentityAgent` is
  configured, and retain the fail-closed behavior when neither path is usable.

### 4. Cover failure and precedence cases

Add or retain focused tests for all of these cases:

1. Effective `IdentityAgent`, absent inherited socket, loaded key: v2 preflight
   and push path succeed.
2. Effective `IdentityAgent` and a different inherited socket: the effective
   agent wins.
3. No effective agent plus a usable inherited socket: inherited fallback works.
4. Effective agent `none`, missing inherited socket, or unusable/missing
   configured socket: v2 fails closed with the existing safe authentication
   outcome; it does not attempt a push.
5. `ssh -G` failure or ambiguous/unsafe agent configuration: fail closed with
   no socket/path/output leak and no push.
6. `ssh-add -l` has no loaded identity: fail closed with no push.
7. Existing remote/ref validation and non-force push argv assertions remain
   intact.

### 5. Validate and hand off safely

- Run the focused v2 transport/gateway tests plus the existing Phase 15.14
  effective-agent regression tests.
- Run the relevant Phase 16 Git-publication integration tests with fakes, the
  Phase 16.1 A/B regression barrier, then the full suite and normal quality
  checks listed below.
- Do not run Gate B. In the final report, state only that, after staged review,
  commit/push, and local CLI installation, controller A may issue one:

  `ai_dev_loop pr-review-v2 resume prv2-617edf93c28019564a2a5d51653ba1a1`

- State that controller A must perform a single status check afterward, not
  polling; it must not recreate the PR or run.

## Testing Criteria

Automated evidence must prove:

1. v2 succeeds when `IdentityAgent` is configured and inherited
   `SSH_AUTH_SOCK` is absent.
2. The exact chosen socket is supplied to `ssh-add -l` and subsequent v2 Git
   transport subprocesses, without global environment mutation.
3. Effective-agent precedence and inherited fallback exactly match the existing
   v1 contract.
4. All unsafe/unavailable/identity-less resolution paths block before a push.
5. No test assertion, exception, log, status-like value, or command argv
   exposes socket paths, key identities, raw SSH output, or credentials.
6. Existing v1 effective-agent tests remain green without behavioral changes.
7. Existing v2 remote identity, no-force, timeout, ambiguity/reconciliation,
   privacy, and Phase 16.1 A/B barrier regressions remain green.

Use fake/injected process runners that record argv and environment maps. Do not
start `ssh-agent`, invoke `ssh-add` against the user's machine, or access a
real network remote.

## Validation

Run the narrowest applicable commands first, adapting paths only if existing
test layout requires it:

```bash
uv run pytest tests/unit/pr_review_v2/test_git_write_transport.py tests/unit/pr_review_v2/test_git_publication_gateway.py tests/unit/test_phase15_14_effective_ssh_agent_preflight.py -q
uv run pytest tests/integration/test_phase16_6_git_publication.py tests/integration/test_phase16_6_git_writes.py tests/integration/test_phase16_1_ab_regression_barrier.py -q
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run mkdocs build --strict
uv build
git diff --check
```

If an unrelated pre-existing formatting failure is encountered, report its
exact path separately; do not reformat unrelated files merely to make the
global command pass.

## Risks Or Recovery Notes

- The live run is paused before the push, so the correction must preserve its
  existing local commit/claim state and recovery fencing; it must not be
  recreated or manually edited.
- A successful authentication preflight does not authorize an extra push: the
  existing remote baseline and reconciliation rules remain the only authority
  for whether publication occurs.
- The full socket path is sensitive operational metadata. Keep it confined to
  process environment handling and fake temporary test paths; never surface a
  real path.
- If staged review identifies any behavior change in v1 or a mismatch between
  v2 process execution and OpenSSH effective configuration, stop Gate B and
  correct it before controller A resumes.

## OpenQuestions

None.
