"""Shared mapping and pagination validation for registry source adapters.

The JSON and HTML registry parsers differ only in how they extract source
fields.  Once extracted, both use this module to apply the same cardinality,
identity, URL, Business_Code and pagination rules and to build the shared
``RegistryPage`` contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, TypeAlias

from ..errors import ParseError
from ..models import RegistryEntry
from ..network import SourceResponse
from .source_schema import (
    PaginationMetadata,
    RegistryFieldPaths,
    RegistryPage,
    ResponseFormat,
    SourceFieldState,
    SourceSchemaViolation,
    build_parse_error,
    is_absolute_source_url,
)


_MISSING = object()
MissingValue = _MISSING
RegistryRecord: TypeAlias = Mapping[str, Any]
PathFactory: TypeAlias = Callable[[str, int], str]


@dataclass(frozen=True, slots=True)
class RegistryFieldValues:
    """Extracted fields for one source registry row."""

    enbek_id: Any
    detail_url: Any
    business_code: Any = _MISSING


def indexed_path(template: str, index: int) -> str:
    """Expand the ``{index}`` placeholder used by source schema paths."""

    return template.replace("{index}", str(index))


def _required_id_violation(value: Any, path: str, *, html: bool) -> SourceSchemaViolation | None:
    if value is _MISSING or value is None or (isinstance(value, str) and not value.strip()):
        reason = "empty Enbek_ID" if html else "required non-empty Enbek_ID"
        return SourceSchemaViolation(path, reason, SourceFieldState.EMPTY)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return SourceSchemaViolation(
            path,
            "Enbek_ID must be an integer greater than zero",
            SourceFieldState.TYPE_INVALID,
        )
    return None


def _detail_url_violation(value: Any, path: str, *, html: bool) -> SourceSchemaViolation | None:
    if value is _MISSING or value is None or (isinstance(value, str) and not value.strip()):
        reason = "empty Detail_Page URL" if html else "required absolute Detail_Page URL"
        return SourceSchemaViolation(path, reason, SourceFieldState.EMPTY)
    if not isinstance(value, str) or not is_absolute_source_url(value, allow_fixture=True):
        reason = "relative Detail_Page URL" if html else "required absolute Detail_Page URL"
        return SourceSchemaViolation(path, reason, SourceFieldState.TYPE_INVALID)
    return None


def _business_code_violation(value: Any, path: str) -> SourceSchemaViolation | None:
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str):
        return SourceSchemaViolation(
            path,
            "Business_Code must be a string or null",
            SourceFieldState.TYPE_INVALID,
        )
    return None


def _numeric_pagination_value(
    value: Any,
    path: str,
    *,
    html: bool,
) -> tuple[int | None, SourceSchemaViolation | None]:
    if value is _MISSING or value is None:
        return None, None
    if html:
        if not isinstance(value, str) or not value.strip() or not value.strip().isdigit():
            return None, SourceSchemaViolation(
                path,
                "pagination value must be a non-negative integer",
                SourceFieldState.TYPE_INVALID,
            )
        value = int(value.strip())
    elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, SourceSchemaViolation(
            path,
            "pagination value must be a non-negative integer",
            SourceFieldState.TYPE_INVALID,
        )
    return value, None


def parse_pagination(
    *,
    source_url: str,
    next_value: Any = _MISSING,
    next_path: str,
    page_value: Any = _MISSING,
    page_path: str,
    page_size_value: Any = _MISSING,
    page_size_path: str,
    total_value: Any = _MISSING,
    total_path: str,
    html: bool,
) -> tuple[PaginationMetadata | None, list[SourceSchemaViolation]]:
    """Validate a page's next link and optional numeric pagination metadata."""

    violations: list[SourceSchemaViolation] = []
    if next_value is _MISSING:
        next_state = SourceFieldState.ABSENT
        next_url = None
        terminal = True
    elif next_value is None:
        next_state = SourceFieldState.NULL
        next_url = None
        terminal = True
    elif isinstance(next_value, str) and not next_value.strip():
        next_state = SourceFieldState.EMPTY
        next_url = None
        terminal = True
    elif not isinstance(next_value, str) or not is_absolute_source_url(
        next_value, allow_fixture=True
    ):
        violations.append(
            SourceSchemaViolation(
                next_path,
                "pagination next URL must be absolute",
                SourceFieldState.TYPE_INVALID,
            )
        )
        next_state = SourceFieldState.TYPE_INVALID
        next_url = None
        terminal = True
    else:
        next_state = SourceFieldState.VALUE
        next_url = next_value
        terminal = False

    page, error = _numeric_pagination_value(page_value, page_path, html=html)
    if error is not None:
        violations.append(error)
    page_size, error = _numeric_pagination_value(page_size_value, page_size_path, html=html)
    if error is not None:
        violations.append(error)
    total, error = _numeric_pagination_value(total_value, total_path, html=html)
    if error is not None:
        violations.append(error)

    if violations:
        return None, violations
    return (
        PaginationMetadata(
            next_url=next_url,
            terminal=terminal,
            next_state=next_state,
            next_path=next_path,
            page_number=page,
            page_size=page_size,
            total=total,
        ),
        [],
    )


def build_registry_page(
    *,
    source_url: str,
    records: Sequence[Any],
    extract_record: Callable[[Any], RegistryFieldValues],
    paths: RegistryFieldPaths,
    response_format: ResponseFormat,
    response: SourceResponse,
    pagination: PaginationMetadata | None,
    pagination_violations: Sequence[SourceSchemaViolation] = (),
    html: bool,
) -> RegistryPage | ParseError:
    """Map extracted source rows to a complete ``RegistryPage`` or one error."""

    violations: list[SourceSchemaViolation] = list(pagination_violations)
    entries: list[RegistryEntry] = []
    for index, record in enumerate(records):
        item_path = indexed_path(paths.html_items if html else paths.json_items, index)
        if not isinstance(record, Mapping):
            violations.append(
                SourceSchemaViolation(
                    item_path,
                    "registry record must be an object",
                    SourceFieldState.TYPE_INVALID,
                )
            )
            continue
        values = extract_record(record)
        id_path = indexed_path(paths.html_enbek_id if html else paths.json_enbek_id, index)
        detail_path = indexed_path(paths.html_detail_url if html else paths.json_detail_url, index)
        code_path = indexed_path(paths.html_business_code if html else paths.json_business_code, index)
        id_error = _required_id_violation(values.enbek_id, id_path, html=html)
        if id_error is not None:
            violations.append(id_error)
        detail_error = _detail_url_violation(values.detail_url, detail_path, html=html)
        if detail_error is not None:
            violations.append(detail_error)
        code_error = _business_code_violation(values.business_code, code_path)
        if code_error is not None:
            violations.append(code_error)
        if id_error is None and detail_error is None and code_error is None:
            entries.append(
                RegistryEntry(
                    enbek_id=values.enbek_id,
                    detail_page_url=values.detail_url,
                    business_code=None if values.business_code is _MISSING else values.business_code,
                )
            )

    if violations:
        return build_parse_error(source_url, violations, stage="registry_parser")
    # ``pagination`` is only absent when the caller found a pagination error;
    # the branch above therefore protects this constructor from partial output.
    assert pagination is not None
    return RegistryPage(
        source_url=source_url,
        entries=tuple(entries),
        pagination=pagination,
        response_format=response_format,
        response=response,
    )


__all__ = [
    "MissingValue",
    "RegistryFieldValues",
    "RegistryRecord",
    "build_registry_page",
    "indexed_path",
    "parse_pagination",
]
