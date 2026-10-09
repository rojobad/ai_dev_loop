# Cursor SDK POC evidence for Phase 25

This directory preserves selected evidence from the isolated POC completed on
2026-10-09 UTC at `/tmp/cursor-sdk-poc-o80OgDfU`, against repository baseline
`58944326a06cf319df521d595050fec091d870da`. It is prerequisite evidence, not
production integration acceptance. The original report and copied data/scripts
are verbatim; [source-manifest.json](source-manifest.json) records byte counts and
SHA-256 for all 71 copied files. Venv/cache, native conversation stores, volatile
PID files and the full synthetic workspace were not copied.
The local Ruff exclusion keeps the verbatim experiment scripts outside product
format/lint discovery; their bytes and offline behavior are checked separately.

## Reading the preserved report

- [Full adoption report](SDK_ADOPTION_REPORT.md)
- [Parent planning handoff](PARENT_CHAT_HANDOFF.md)
- [Verification and deduplicated token summary](verification-summary.json)
- [Original final credential/process audit](final-audit.json)
- [Recorded first-turn usage](first.json)
- [Cancellation evidence](cancel.json)
- [Runtime crash](runtime-crash.json) and [stale snapshot](runtime-crash-reopened-snapshot.json)
- [Registered cancel](runtime-crash-cancel.json) and [recovered conversation](runtime-crash-recovered.json)
- [Replay cursor](replay-cursor.json), [reopened result reads](inspect.json)
- [Historical-reader evidence](historical-read.json)
- [Billing unavailable](billed-usage.json)
- [Offline verification script](test_evidence.py)

Original absolute links beginning `/tmp/cursor-sdk-poc-o80OgDfU/` in the verbatim
report/handoff refer to the original experiment. For copied files, replace that
prefix with this directory; links to the original workspace/native state remain
historical references, not claims that those excluded directories are archived.
The source manifest's original location is provenance, not a runtime dependency.

## What was verified

SDK Python 1.0.37 on Python 3.13.14/WSL exposed exact agent/run identities,
project rules/skills/AGENTS/MCP, editing, cross-process resume, per-turn usage and
result/replay. The 27 checks consist of 22 assertions over recorded evidence and
five simulated transport contracts. They do not rerun the original model calls.
The audit is the original POC's recorded result, not a fresh workstation audit.

Known total: 245062 tokens from 11 distinct completed turns; two interrupted
turns have missing counters. This is a lower bound, not billed usage.
Runtime SIGKILL left a tool alive and native run running; public registered
get-run/cancel/confirm/resume recovery succeeded after tool cleanup.

## Rechecking without model calls

Use an isolated environment with `cursor-sdk==1.0.37`; then run that environment's
Python against `test_evidence.py`. The included `workspace/calc.py` is the only
workspace fixture needed by these offline checks. No key or live bridge is needed.
The main chat repeated the original checks and the archived checks while planning;
both passed all 27. A fresh no-model check does not imply a new production smoke.

Other scripts are preserved experiment sources. They may require the original
workspace, current repository fixtures and a newly supplied credential; they are
not portable one-command acceptance tests and must not be run as part of planning
or repository pytest. The POC's instrumented private runtime-PID inspection must
not become production code. Do not copy/create credentials when replaying data.

The five Phase 25 plans specify production contracts, Python 3.11, supervision,
dispatch uncertainty, historical compatibility and integration gates still needed.
