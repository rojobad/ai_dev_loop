# Phase 20.7 Findings — Sequence Phase Run Lineage

## Summary

Phase 20.7 adds versioned per-ordinal run-attempt lineage for materialized sequence phases. The seventh correction pass aligns JSON-native integral numbers (`1.0`) between Draft 2020-12 integer validation and Pydantic lineage models, while still rejecting booleans, numeric strings, and non-integral floats (`1.5`) on `generation` and `ordinal`.

## Commands Run

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_7_sequence_run_lineage.py \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_schema_readonly_historical.py \
  tests/unit/scheduler/test_phase20_1_sequence_prepare.py \
  tests/unit/scheduler/test_phase20_2_sequence_start.py \
  tests/unit/scheduler/test_phase20_4_sequence_lifecycle.py

TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler tests/integration

uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

## Validation Results

- Phase 20.7 lineage tests: **31 passed**
- Focused Phase 20.7 and regression scheduler tests: **89 passed, 1 skipped**
- Full scheduler/integration suite: **698 passed, 2 skipped**
- `ruff format --check`, `ruff check`, `mypy src`: **pass**
- `python -m build`: **pass**
- `mkdocs build --strict`: **pass**
- `git diff --check`: **pass**

## Correction Highlights

- **JSON-native integral numbers:** `generation`, `ordinal`, and `schema_version` coerce mathematically integral JSON numbers (for example `1.0`) to `int` in Pydantic while rejecting booleans, numeric strings, and non-integral floats; phase and aggregate custom format checkers use the same integral-number rule for generation/ordinal chains.
- **Integral parity tests:** `test_json_native_integer_model_schema_parity` exercises identical accept/reject behavior for `1.0`, `1.5`, booleans, and numeric strings on `generation` and `ordinal`.
- **Whitespace-aware strings:** lineage JSON Schemas use shared `nonEmptyString` / `nullableNonEmptyString` definitions with `pattern: ".*\\S.*"` for all `NonEmptyStr`-backed fields, including run IDs, materialization/resolution timestamps, and nullable source/accepted/resolved fields.
- **Primitive parity tests:** added identical model/schema rejection cases for numeric strings, booleans, whitespace-only required strings, and whitespace-only non-null nullable strings without weakening generation, source-chain, current-leaf, accepted-leaf, terminal-pair, or privacy invariants.

## Implementation Notes

- **Domain:** `sequence_run_lineage.py` defines attempt/phase/lineage models, JSON Schema validation helpers, successor-based historical projection, terminal/materialization validation, and `validate_persisted_lineage_aggregate`.
- **Migration:** `0009_sequence_run_lineage.sql` adds `scheduler_sequence_run_attempts`; schema version **9** backfills generation-1 rows from validated v8 sequence state without inventing recovery generations.
- **Store:** `sequence_run_lineage_store.py` provides unpaginated reads, idempotent insert/replay, strict write-once terminal resolution, ordered execution-leaf CAS, immutable-field-gated authoritative sync/backfill, and validated loads with corruption wrapping.
- **Runtime wiring:** generation-1 insert on sequence start; accepted handoff/finalization and blocked/aborted reconciliation resolve terminals without changing lifecycle transitions.

## Residual Risks

- `compare_and_swap_sequence_execution_leaf` is implemented and tested but not yet invoked from public recovery commands (Phase 20.8 scope).
- Multi-generation same-reviewer retry behavior remains unimplemented beyond store reservation and internal CAS APIs.
- JSON Schema `format` validators carry cross-field invariants that draft 2020-12 cannot express purely declaratively; they are registered alongside the schema files and exercised through the same aggregate validation entry point as Pydantic.

## Phase Boundary Confirmation

- No sequence-aware `scheduler review retry`, no second-attempt creation from tick/reconcile, no Git/process/recovery command behavior changes beyond persisting lineage on existing transitions.
- No Phase 20.6 artifacts imported or restored.
