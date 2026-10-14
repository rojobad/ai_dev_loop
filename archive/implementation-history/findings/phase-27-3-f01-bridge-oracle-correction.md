# Phase 27.3 F-01 — public Bridge oracle correction

F-01: **fixed**. Complete public Bridge traversal passed; inherited C-12 validation debt is closed.
Contract: C-12, closure of inherited validation debt. Worktree:
`/home/rojobad/Projects/ai_dev_loop-phase27-execution`, branch
`codex/phase-27-dual-implementer`, HEAD `128b5cb`.

## Confirmed issue and correction

This session independently reproduced the failed public traversal: expected
`done: Implement the sample plan exactl`, captured
`done: This is a non-interactive run. D`. The supplied review also records its
reproduction on accepted 27.2; that baseline reproduction was not repeated or
counted as independently executed here.

Affected production paths are `runners/cursor.py::build_cursor_args`, which adds
`NON_INTERACTIVE_PROMPT` to the actual positional invocation, and
`runners/codex.py::build_codex_review_prompt`, which emits the provider-neutral
review footer. Neither production path was changed for this correction. The earlier implementation
report used a `sequence or c05` selection for the additional acceptance module,
which excluded this complete C-04 traversal and did not close that validation debt.

`tests/support/bridge_acceptance_constants.py` now includes an independently
specified literal invocation prefix when predicting the fake Cursor response
from the frozen payload. Its footer expectation includes the approved legacy-field
forwarding sentence. No expected bytes are derived from returned API content or
imported from the production invocation builder. The helper applies to initial
work and both correction iterations. Original prompt snapshots remain unchanged.

`tests/unit/integration_api/test_bridge_supervision_traversal.py` adds three
explicit expected-response checks (initial and two corrections) and rejection of
the obsolete footer. Existing content, exact-byte, privacy, pagination, integrity,
and complete traversal assertions were retained. Historical frozen fixtures,
production behavior, review skills/result schema and project YAML were unchanged.

## Independently executed validation

Pre-correction reproduction:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_NO_SYNC=1 uv run python -m pytest -q tests/integration/test_phase21_6_integration_acceptance.py::test_c04_full_supervision_traversal_public_cli_only
```

Result: **1 failed in 31.73s**, with the reported byte mismatch.

Post-correction focused selection:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_NO_SYNC=1 uv run python -m pytest -q tests/unit/integration_api/test_bridge_supervision_traversal.py tests/integration/test_phase21_6_integration_acceptance.py tests/unit/integration_api/test_contract.py tests/unit/integration_api/test_phase21_2_schemas.py
```

Result: **49 passed in 322.93s**, exit 0, no skips. This includes
`test_c04_full_supervision_traversal_public_cli_only` completing all assertions.
This selection runs the complete public CLI traversal, every
caller in its acceptance module, helper assertions, historical prompt unavailability,
pinned/additive consumers, schema fixtures and major-version rejection. Tests use
fake CLIs/backends, temporary Git repositories and isolated HOME/XDG roots.

```bash
UV_NO_SYNC=1 uv run python -m ruff format --check .
UV_NO_SYNC=1 uv run python -m ruff check .
UV_NO_SYNC=1 uv run python -m mypy src
git diff --check
git diff --cached --check
```

Results: **all exit 0**; 346 files already formatted; Ruff lint passed; mypy
reported no issues in 181 source files; both whitespace checks passed.
No documentation-site sources changed, so MkDocs was not rerun for F-01.

## Scope and remaining gates

The existing staged Phase 27.3 patch was preserved. Its binary staged-diff SHA-256
was unchanged before and after editing:
`6e4d615681817c633f15183605ae99a65dea8a2d0418869c1dc999be93979647`.
This correction and its report remain unstaged for manual staging/review.

No broader regression selection, repository-wide pytest, package pipeline/build,
operator installation, timers, live scheduler operations, real models, owned
sessions or commits were executed. No F-01 validation gap remains. Those separate gates remain pending; earlier
prerequisite/manual review results were not claimed as new validation. This
manual correction does not update scheduler acceptance.
