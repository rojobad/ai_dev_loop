"""Schema validation for Integration API 1.1 run inspection responses."""

from __future__ import annotations

import json
from pathlib import Path

from ai_dev_loop.integration_api.schemas import (
    RUN_INSPECT_DATA_SCHEMA,
    RUN_LIST_DATA_SCHEMA,
    assert_packaged_schemas_exist,
    validate_integration_instance,
)

FIXTURES_V1_1 = Path(__file__).resolve().parents[2] / "fixtures" / "integration_api" / "v1_1"


def test_f02_packaged_run_schemas_exist() -> None:
    assert_packaged_schemas_exist()


def test_f02_v1_1_fixture_examples_validate() -> None:
    validate_integration_instance(
        json.loads((FIXTURES_V1_1 / "run_list_data_minimal.json").read_text(encoding="utf-8")),
        RUN_LIST_DATA_SCHEMA,
    )
    validate_integration_instance(
        json.loads((FIXTURES_V1_1 / "run_inspect_data_minimal.json").read_text(encoding="utf-8")),
        RUN_INSPECT_DATA_SCHEMA,
    )
