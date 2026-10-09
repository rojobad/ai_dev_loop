# Phase 25 planning delivery and validation

Date: 2026-10-09 (America/La_Paz).

## Scope and authority

The operator approved local Cursor SDK adoption with private per-conversation
storage, explicit POC-tested model parameters, project settings, current disabled
sandbox, a pinned SDK version and private credential references. Historical data
must remain readable; historical CLI execution need not be supported. Installation
is deferred until existing work is settled. Phase 24 remains unexecuted.

The operator requested five execution plans/prompts and commit/push of the planning
delivery. The repository's principal branch is `main`; remote inspection found no
`master`. The operator explicitly confirmed using `main` after that clarification.
The local baseline is `58944326a06cf319df521d595050fec091d870da`, one existing
Phase 24 planning commit ahead of remote `6792e0f` at inspection. Publishing this
delivery also publishes that existing ancestor; it does not execute Phase 24.

## Artifacts

- [Overview and approved cross-phase contracts](../plans/phase-25-cursor-sdk-and-token-usage.md)
- [25.1 — Foundation/configuration](../plans/phase-25-1-sdk-foundation-and-configuration.md)
- [25.2 — Identity/worker/evidence](../plans/phase-25-2-sdk-identity-worker-and-evidence.md)
- [25.3 — Continuity/recovery](../plans/phase-25-3-sdk-continuity-and-recovery.md)
- [25.4 — Usage/history](../plans/phase-25-4-token-usage-and-historical-queries.md)
- [25.5 — Integration/cutover](../plans/phase-25-5-sdk-integration-and-cutover-acceptance.md)
- Five corresponding concise `prompt_phase-25-*.txt` inputs linked in the overview.
  They are deliberately tracked despite the general prompt ignore convention.
- [Durable POC evidence](phase-25-sdk-poc/README.md): 71 verbatim selected files,
  700886 bytes, plus an independent source SHA-256 manifest and index. Venv/cache,
  native stores, PID files and unrelated workspace files were not copied.

## Executed verification

| Check | Result |
| --- | --- |
| Original POC offline script using its isolated venv | 27 passed; 22 recorded-evidence assertions and five simulated transport contracts. |
| Same script from the archived directory, using `/tmp/cursor-sdk-poc-o80OgDfU/venv/bin/python archive/implementation-history/findings/phase-25-sdk-poc/test_evidence.py` | 27 passed; no credential, bridge or model call. |
| Plan structure/link/prompt verification via a temporary Python check | Six documents contain all 12 required plan sections; five prompts match their plan paths; relative document/governance links resolve. |
| Evidence manifest/hash/JSON/NDJSON/Python syntax verification | All 71 copied file hashes/sizes match; copied source parses; JSON and NDJSON parse. Credential-pattern scan reported no actual-key matches. |
| `.venv/bin/python -m ruff check .` | Passed. Verbatim POC scripts are excluded through their local `.ruff.toml`; their bytes/syntax/offline behavior are verified separately. |
| `.venv/bin/python -m ruff format --check .` | Failed on 23 unchanged pre-existing source/test files; 314 maintained files already formatted. No production formatting change was made. |
| `.venv/bin/python -m mkdocs build --strict` | Passed; the archive plans themselves are outside MkDocs navigation, so their relative links were checked separately. |
| Final temporary contract/staging check | All 20 F/W/R/U/G contracts have matching acceptance rows; 86 staged paths are limited to Phase 25 plans/prompts/evidence/findings. No unstaged tracked changes. |
| `git diff --cached --check` | Passed; no whitespace errors in the planning delivery. |

The temporary structure check also required all five prompts to be substantially
shorter than their plans and found zero missing links/sections/hash errors. The
preserved POC audit is historical experiment evidence, not a new security/process
audit of the workstation.

## Scope review and pending validation

The plans require coherent ownership across original run and successors, exact
native/Codex IDs, whole-tree termination, conservative ambiguous-dispatch handling,
versioned historic readers, provider-specific token accounting and read-only
inspection. Earlier schema-changing slices own their historic-read compatibility;
25.4/25.5 cannot postpone that requirement. Every subphase has contract IDs,
production entrypoints, independent acceptance tests and focused commands.

Root `ai_dev_loop.yaml`, production code/tests, existing Phase 24 plans, governance
rules/skills, live engine/artifacts and installed runtime/timer were unchanged.
No implementation run, model call, credential provisioning, package installation
or cleanup was performed. Generated MkDocs site output remains ignored.

Repository-wide pytest and implementation-specific F/W/R/U/G tests were not run:
the delivery is planning/evidence only and those future tests do not yet exist.
The full repository integration gate belongs to 25.5. Python 3.11 SDK verification,
production SDK workflow tests, manual 25.5 YAML acceptance, real cutover and any
separately authorized live smoke remain future work. POC checks are not production
acceptance. The plans' `OpenQuestions` are `None` for the approved design; failed
prerequisites require evidence and a stop before dependent implementation.
