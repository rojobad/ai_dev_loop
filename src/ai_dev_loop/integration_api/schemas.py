"""Packaged Integration API JSON schema helpers."""

from __future__ import annotations

import json
from typing import Any, cast

import jsonschema  # type: ignore[import-untyped]
from referencing import Registry, Resource

from ai_dev_loop.paths import schema_path

ENVELOPE_SCHEMA = "integration-api-response-envelope-v1.json"
INFO_DATA_SCHEMA = "integration-api-info-data-v1.json"
ERROR_BODY_SCHEMA = "integration-api-error-body-v1.json"
ARTIFACT_CHUNK_SCHEMA = "integration-api-artifact-chunk-v1.json"
COLLECTION_PAGE_SCHEMA = "integration-api-collection-page-v1.json"


def load_integration_schema(name: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads(schema_path(name).read_text(encoding="utf-8")))


def integration_schema_names() -> tuple[str, ...]:
    return (
        ENVELOPE_SCHEMA,
        INFO_DATA_SCHEMA,
        ERROR_BODY_SCHEMA,
        ARTIFACT_CHUNK_SCHEMA,
        COLLECTION_PAGE_SCHEMA,
    )


def assert_packaged_schemas_exist() -> None:
    for name in integration_schema_names():
        path = schema_path(name)
        if not path.is_file():
            raise FileNotFoundError(path)


def integration_schema_registry() -> Registry:
    resources: list[tuple[str, Resource[Any]]] = []
    for name in integration_schema_names():
        schema = load_integration_schema(name)
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str):
            raise ValueError(f"Schema {name} is missing a string $id")
        resources.append((schema_id, Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def _integration_rfc3339_utc_format_checker(value: object) -> bool:
    if not isinstance(value, str):
        return False
    from ai_dev_loop.integration_api.errors import IntegrationApiError
    from ai_dev_loop.integration_api.validation import parse_observed_at_rfc3339_utc

    try:
        parse_observed_at_rfc3339_utc(value)
    except IntegrationApiError:
        return False
    return True


def validate_integration_instance(instance: Any, schema_name: str) -> None:
    schema = load_integration_schema(schema_name)
    registry = integration_schema_registry()
    format_checker = jsonschema.FormatChecker()
    format_checker.checks("integration-rfc3339-utc", raises=())(
        _integration_rfc3339_utc_format_checker
    )
    validator = jsonschema.Draft202012Validator(
        schema,
        registry=registry,
        format_checker=format_checker,
    )
    validator.validate(instance)
