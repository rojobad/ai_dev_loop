# Phase 25.5 — Integration gate and SDK-only cutover

## Goals

Close the production integration gate, retire Cursor CLI execution for new
admissions and deliver an actionable quiescent installation procedure. Preserve
historical queries, current timeout/recovery/Git behavior and explicit manual
acceptance of the project control-plane configuration.

## Non-Goals

Performing installation or live smoke automatically, implementing Phase 24,
changing review/timeout ceilings, deleting history/native stores, billing or
expanding providers/platforms beyond tested local Linux/WSL.

## Scope

Full-path integration/cutover tests, historical-reader gate, removal of temporary
SDK/CLI execution branches, package/extra distribution checks, operator docs,
project v2 YAML source update, affected rules and completion evidence.

## Out of Scope

Live agents/credentials/ledger/artifacts, timer/linger/bus changes, real package
installation, destructive cleanup/Git, push/PR/merge, Phase 24 plan edits,
planning/review skill edits and unrelated incident remediation.

## Required Context

Read [the overview](phase-25-cursor-sdk-and-token-usage.md) and accepted 25.1–25.4
plans/findings. Verify every F/W/R/U contract against code/tests. The preserved
[POC](../findings/phase-25-sdk-poc/README.md) does not establish production
acceptance. Inspect new config/domain schemas, all new SDK dispatch/recovery
callers, current CLI help, `docs/referencia/cli.md`, configuration, state/privacy,
complete-flow guides and `docs/operacion/timer-systemd-wsl.md`.
Inspect package build/installation/integration assets and the actual Integration
API query surfaces; there is no repository CI workflow to invent or claim ran.

## Cursor Rules And Skills

Follow `AGENTS.md` and all eight overview `.cursor/rules/`. Read
`.agents/skills/create-cursor-plan/SKILL.md` and
`.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md` for docs/evidence.
Keep them and `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`
unchanged. No `.cursor/skills/` exists. Update contradicted rule statements and
package-owned handoff/controller guidance only where the final supported SDK
surface requires it; do not change installed global assets during development.

## Architecture Guardrails

- Final new execution is SDK-local only. V1 CLI-backed records retain read-only
  parsers, not an executable fallback or silent SDK conversion. Old records may
  be settled/cancelled through existing non-agent paths but may not start/resume/
  retry/extend into new agent work. A historical query must never require SDK,
  CLI, credential, bridge, catalog, store or repository access beyond its
  established authenticated artifact contract.
- Remove CLI-only new-input flags/config and startup probes at the owning
  surfaces; provide actionable obsolete-input errors and current help/docs.
  V1 project configurations for a new run require an explicit v2 replacement;
  old prepared manifests/frozen definitions require fresh preparation. Preserve
  original records/bytes; never rewrite aliases, inputs or hashes in place.
- Root `ai_dev_loop.yaml` changes are control-plane work explicitly included in
  this slice and require separate manual acceptance. Automated loop acceptance
  alone is insufficient; leave final changes staged under normal runtime policy.
  Reviewer/planning skills and unrelated budgets stay unchanged.
- Development/testing uses isolated homes/DBs/repos and a stable installed
  implementation driver. Do not change live timer units or install editable WIP
  to keep an in-progress self-development run working.
- Numeric usage cannot affect review decisions/Git authority. Codex retains
  exact B/bootstrap/resume and explicit frozen model/effort. Standalone/final
  accepted changes remain staged; only existing approved intermediate sequence
  checkpoints commit locally.

## Implementation Plan

**G-01 — Final execution boundary.** Remove intermediate guards only after their
contracts are implemented. Route new supported SDK config through preflight,
owned worker, capture, staging, Codex review/correction/recovery and sequence
handoff. Retire the CLI create-chat/execute entrypoints from production scheduler
dispatch and new config/flag surfaces. Retain parsing helpers only where needed
for authentic historical evidence; remove obsolete fake CLI execution reliance
from tests for new runs. Explicitly gate all old input execution entrypoints,
including queued start/tick, cursor-retry, review-retry successors, extend and
prepared-sequence start/materialization/replacement/handoff. Read-only check
results must clearly report old-runtime execution as unsupported.

**G-02 — Comprehensive independent integration gate.** Add full-path fake-backed
scenarios: standalone initial+correction accepted; max review ceiling/extension;
timeout and capacity continuation; failed initial/correction successor; sequence
leaf replacement/checkpoint/next phase; abort and unavailable process observation;
known terminal replay and ambiguous send; partial usage/deduplication; historical
queries with no provider tools. Check exact conversation/store/B/prompt ownership
and no prohibited Git actions. Exercise installed package launch paths, not only
source imports or mocked reducer calls. Run the repository-wide suite once as
this integration gate; subsequent correction rounds focus on failed/affected
paths, broadening only for an identified risk.

**G-03 — Packaging and operational docs.** Build wheel/sdist with the pinned SDK
extra and verify clean isolated installation on Python 3.11 Linux/WSL, without
changing real global packages. Inspect base-package historical reads without
the extra. Document new config, explicit project settings/tool/sandbox policy,
credential file reference and rotation, mutable store location/retention,
whole-tree recovery, uncertainty holds, metric scopes/coverage and old queries.
Examples must follow current implemented CLI help and explicit reviewer inputs.
Keep fixed timeout docs intact; label Phase 24 unimplemented.

Deliver this operator cutover procedure using supported current commands:

1. Finish/settle old runs and sequences, including waits, recovery successors,
   prepared definitions and pending handoff/replacement intents. Use old runtime
   inspection/abort where needed, preserving records and worktree changes.
2. Verify no owned attempts or uncertain holds require execution reconciliation.
   Preserve a consistent operator backup of ledger, artifacts, config and native
   stores; do not copy an active SQLite WAL as if it were a consistent snapshot.
3. After explicit installation authorization, install the fully accepted package
   with its `cursor-sdk` extra using the normal `uv tool` workflow. Provision the
   private credential separately. Follow timer-systemd-wsl documentation only
   for any explicitly authorized timer action; changing this code does not grant it.
4. Verify old queries without modifying records. Convert target YAML/any intended
   future manifests explicitly and freshly submit/prepare new SDK definitions.
   A previously prepared CLI definition is not updated by the package install.
5. A separately authorized small real SDK smoke, if requested, uses an isolated
   synthetic repository/store and a bounded prompt. Preserve result/usage without
   credentials. Record it as executed or pending, distinct from fake integration.

No implementation test executes these workstation steps. The final installed
package can query any retained history even when the SDK extra is absent.

**G-04 — Project configuration and final acceptance.** Update source
`ai_dev_loop.yaml` to the overview's explicit v2 selection, SDK version,
`credential_ref: cursor-default`, project settings and disabled sandbox; retain
all current unrelated project/Codex/workflow/prompt values. Remove Cursor-only
CLI command/output_format/force/trust fields. No real credential is created.
This is a source-config change for future new admissions after installation,
not a mutation of active frozen inputs. Produce findings mapping all phase
contracts and record the required manual review of this exact YAML change.

Boundary: fully accepted package/config + old execution quiescence + explicit
operator installation authority -> new SDK-only admissions. Historical reads
remain available before/after. Interrupted installation invokes operator recovery
from a consistent backup; do not launch more work to guess installation state.
After new SDK records exist, rollback must use a reader-compatible package and
quiescent state; reinstalling a pre-25 reader alone is unsafe.

## Testing Criteria

Add `tests/integration/test_phase25_5_sdk_acceptance.py` and
`tests/unit/scheduler/test_phase25_5_cutover.py`, extending package/Integration API
acceptance tests where their real callers change. Fake SDK/Codex and systemd
backends, synthetic credentials, deterministic barriers and temp HOME/XDG/repos
remain mandatory. No new live inference belongs in pytest.

| Contract | Gate evidence |
| --- | --- |
| G-01 | Every new-input path dispatches SDK; old-runtime mutating paths fail before provider/staging/review work. Historical status/list/history/timeline/usage and explicit inspection remain available. |
| G-02 | Full standalone/sequence/recovery/abort/review-budget matrix runs through installed production callers, proving identities, known/partial metrics, reservations and Git boundaries. Full repository pytest results are reported separately. |
| G-03 | Python 3.11 package with extra runs bounded bridge health; package without extra reads genuine historical fixtures. Strict MkDocs build passes and docs match help; no workstation changes occur. |
| G-04 | Exact v2 source YAML validates with fake credentials/catalog and preserves unrelated values; manual control-plane acceptance is explicitly pending or recorded, never inferred from reviewer acceptance. |

Recheck every mandatory predecessor contract. Retain historical fixtures with
baseline/source provenance and unchanged hashes. A live smoke is supplemental
and its pending status does not replace required deterministic production tests.

## Validation

Focused development/review:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/integration/test_phase25_5_sdk_acceptance.py tests/unit/scheduler/test_phase25_5_cutover.py tests/integration/test_phase21_6_integration_acceptance.py tests/integration/test_phase20_9_multi_run_sequence_end_to_end.py
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
.venv/bin/python -m build
git diff --check
```

Separate repository integration gate:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q
```

Record G-01–G-04 and F/W/R/U contract mapping, exact executed/failed/unexecuted
commands, isolated package tests, pipeline result, manual YAML acceptance and
pending operator/live smoke steps in
`archive/implementation-history/findings/phase-25-sdk-integration-acceptance.md`.
Do not claim deployed operation, a CI run or complete billing from these tests.

## Risks Or Recovery Notes

Source implementation may be complete before manual config acceptance and real
installation. Keep those states explicit. No cleanup/install/timer action is
authorized by this plan's automated execution. After SDK data exists, retain
reader compatibility during any operator rollback and protect the conversation
store required for still-recoverable SDK work.

## OpenQuestions

None for implementation. Manual control-plane acceptance, installation and any
live smoke are separately authorized operator actions, not missing design choices.
