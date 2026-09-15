"""Pure JSON adapter for Enbek registry pages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import ParseError
from ..network import SourceResponse
from .registry_mapping import (
    MissingValue,
    RegistryFieldValues,
    build_registry_page,
    parse_pagination,
)
from .source_schema import (
    REGISTRY_PATHS,
    JsonResponseWrapper,
    RegistryPage,
    ResponseFormat,
    SourceFieldState,
    SourceSchemaViolation,
    is_absolute_source_url,
)


class RegistryJsonParser:
    """Parse one already received JSON registry response without network I/O."""

    format = ResponseFormat.JSON

    def parse(self, url: str, response: SourceResponse) -> RegistryPage | ParseError:
        """Return a complete page or one aggregated schema error."""

        if not isinstance(response, SourceResponse):
            return ParseError(
                "registry parser requires a SourceResponse",
                url=str(url),
                path="$",
                invalid_fields=("$",),
                stage="registry_parser",
            )
        if not isinstance(url, str) or not is_absolute_source_url(url, allow_fixture=True):
            return ParseError(
                "registry source URL must be absolute",
                url=str(url),
                path="$",
                invalid_fields=("$",),
                stage="registry_parser",
            )

        try:
            wrapped = JsonResponseWrapper.from_response(response, source_url=url)
        except ParseError as error:
            return error

        document = wrapped.payload
        violations: list[SourceSchemaViolation] = []
        data: Mapping[str, Any] | None
        if not isinstance(document, Mapping):
            violations.append(
                SourceSchemaViolation(
                    "$",
                    "registry response root must be an object",
                    SourceFieldState.TYPE_INVALID,
                )
            )
            records = []
            next_value = page_value = page_size_value = total_value = MissingValue
        else:
            raw_data = document.get("data", MissingValue)
            if isinstance(raw_data, list):
                # Live Enbek REST API structure: {"data": [...], "next_page_url": ..., "current_page": ...}
                records = raw_data
                next_value = document.get("next_page_url", MissingValue)
                page_value = document.get("current_page", MissingValue)
                page_size_value = document.get("per_page", MissingValue)
                total_value = document.get("total", MissingValue)
            elif isinstance(raw_data, Mapping):
                # Test fixture structure: {"data": {"items": [...], "next": ...}}
                raw_items = raw_data.get("items", MissingValue)
                if not isinstance(raw_items, list):
                    violations.append(
                        SourceSchemaViolation(
                            REGISTRY_PATHS.json_items,
                            "registry records collection must be an array",
                            SourceFieldState.ABSENT
                            if raw_items is MissingValue
                            else SourceFieldState.TYPE_INVALID,
                        )
                    )
                    records = []
                else:
                    records = raw_items
                next_value = raw_data.get("next", MissingValue)
                page_value = raw_data.get("page", MissingValue)
                page_size_value = raw_data.get("page_size", MissingValue)
                total_value = raw_data.get("total", MissingValue)
            else:
                state = (
                    SourceFieldState.ABSENT
                    if raw_data is MissingValue
                    else SourceFieldState.TYPE_INVALID
                )
                violations.append(
                    SourceSchemaViolation(
                        "$.data",
                        "registry data object is required",
                        state,
                    )
                )
                records = []
                next_value = page_value = page_size_value = total_value = MissingValue

        pagination, pagination_violations = parse_pagination(
            source_url=url,
            next_value=next_value,
            next_path=REGISTRY_PATHS.json_next,
            page_value=page_value,
            page_path=REGISTRY_PATHS.json_page,
            page_size_value=page_size_value,
            page_size_path=REGISTRY_PATHS.json_page_size,
            total_value=total_value,
            total_path=REGISTRY_PATHS.json_total,
            html=False,
        )
        violations.extend(pagination_violations)

        def extract(record: Mapping[str, Any]) -> RegistryFieldValues:
            enbek_id = record.get("enbek_id") if record.get("enbek_id") is not None else record.get("id", MissingValue)
            detail_url = record.get("detail_url")
            if detail_url is None and enbek_id is not MissingValue and isinstance(enbek_id, int):
                detail_url = f"https://career.enbek.kz/ru/api/professionalstandart/{enbek_id}"
            return RegistryFieldValues(
                enbek_id=enbek_id,
                detail_url=detail_url if detail_url is not None else MissingValue,
                business_code=record.get("code") or record.get("code_ps", MissingValue),
            )

        return build_registry_page(
            source_url=url,
            records=records,
            extract_record=extract,
            paths=REGISTRY_PATHS,
            response_format=ResponseFormat.JSON,
            response=response,
            pagination=pagination,
            pagination_violations=violations,
            html=False,
        )

    parse_response = parse
    __call__ = parse


# Naming aliases keep the concrete adapter discoverable for callers that use
# either the JSON-first or registry-first spelling.
JSONRegistryParser = RegistryJsonParser
RegistryJSONParser = RegistryJsonParser
RegistryParser = RegistryJsonParser


def parse_registry_json(url: str, response: SourceResponse) -> RegistryPage | ParseError:
    """Functional convenience wrapper around :class:`RegistryJsonParser`."""

    return RegistryJsonParser().parse(url, response)


__all__ = [
    "JSONRegistryParser",
    "RegistryJSONParser",
    "RegistryJsonParser",
    "RegistryParser",
    "parse_registry_json",
]
