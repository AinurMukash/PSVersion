"""Shared validation and mapping for JSON and HTML detail adapters.

The adapters in this package only decode an already received response.  This
module contains the format-independent part: source-field validation,
missing/null/empty semantics and construction of the typed ``DetailPage``
contract.  It intentionally has no network or filesystem dependencies.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from ..errors import ParseError
from ..models import DetailAttributes, OkedAttributes, ProfessionRecord, SourceAttributes
from ..network import SourceResponse
from .source_schema import (
    DETAIL_FIELD_PATHS,
    DetailFieldPaths,
    DetailPage,
    ResponseFormat,
    SourceFieldState,
    SourceSchemaViolation,
    build_parse_error,
    is_absolute_source_url,
    require_absolute_source_url,
)


_MISSING = object()
_NUMBER_TYPES = (int, float)
_OKED_FIELDS = (
    "section_code",
    "section_name",
    "division_code",
    "division_name",
    "group_code",
    "group_name",
    "class_code",
    "class_name",
    "subclass_code",
    "subclass_name",
)


# A stable mapping is exported for source-profile and test code that needs to
# refer to the detail contract without importing either concrete adapter.
DETAIL_JSON_MAPPING: dict[str, str] = {
    "root": DETAIL_FIELD_PATHS.json_root,
    "enbek_id": DETAIL_FIELD_PATHS.json_enbek_id,
    "code": DETAIL_FIELD_PATHS.json_code,
    "title": DETAIL_FIELD_PATHS.json_title,
    "government_body": DETAIL_FIELD_PATHS.json_government_body,
    "description": DETAIL_FIELD_PATHS.json_description,
    "version": DETAIL_FIELD_PATHS.json_version,
    "year": DETAIL_FIELD_PATHS.json_year,
    "revision_date": DETAIL_FIELD_PATHS.json_revision_date,
    "oked": DETAIL_FIELD_PATHS.json_oked,
    "professions": DETAIL_FIELD_PATHS.json_professions,
    "pdf_url": DETAIL_FIELD_PATHS.json_pdf_url,
    "pdf_published": DETAIL_FIELD_PATHS.json_pdf_published,
}

DETAIL_HTML_MAPPING: dict[str, str] = {
    "root": DETAIL_FIELD_PATHS.html_root,
    "enbek_id": DETAIL_FIELD_PATHS.html_enbek_id,
    "code": DETAIL_FIELD_PATHS.html_code,
    "title": DETAIL_FIELD_PATHS.html_title,
    "government_body": DETAIL_FIELD_PATHS.html_government_body,
    "description": DETAIL_FIELD_PATHS.html_description,
    "version": DETAIL_FIELD_PATHS.html_version,
    "year": DETAIL_FIELD_PATHS.html_year,
    "revision_date": DETAIL_FIELD_PATHS.html_revision_date,
    "oked": DETAIL_FIELD_PATHS.html_oked,
    "professions": DETAIL_FIELD_PATHS.html_professions,
    "pdf_url": DETAIL_FIELD_PATHS.html_pdf_url,
    "pdf_published": DETAIL_FIELD_PATHS.html_pdf_published,
}

# Short aliases are useful to source adapters and preserve an obvious public
# spelling for callers that use the terms from the requirements.
DETAIL_FIELD_MAPPING = DETAIL_JSON_MAPPING
DETAIL_SCHEMA_MAPPING = DETAIL_JSON_MAPPING


def _field(values: Mapping[str, Any], name: str) -> Any:
    return values[name] if name in values else _MISSING


def _violation(
    violations: list[SourceSchemaViolation],
    path: str,
    reason: str,
    state: SourceFieldState = SourceFieldState.TYPE_INVALID,
) -> None:
    violations.append(SourceSchemaViolation(path, reason, state))


def _optional_text(
    values: Mapping[str, Any],
    name: str,
    path: str,
    violations: list[SourceSchemaViolation],
) -> str | None:
    value = _field(values, name)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str):
        _violation(violations, path, "source field must be a string or null")
        return None
    return value


def _required_text(
    values: Mapping[str, Any],
    name: str,
    path: str,
    reason: str,
    violations: list[SourceSchemaViolation],
) -> str | None:
    value = _field(values, name)
    if value is _MISSING:
        _violation(violations, path, reason, SourceFieldState.ABSENT)
        return None
    if value is None:
        _violation(violations, path, reason, SourceFieldState.NULL)
        return None
    if not isinstance(value, str):
        _violation(violations, path, "source field must be a string")
        return None
    if not value.strip():
        _violation(violations, path, reason, SourceFieldState.EMPTY)
        return None
    return value


def _optional_number(
    values: Mapping[str, Any],
    name: str,
    path: str,
    violations: list[SourceSchemaViolation],
) -> int | float | None:
    value = _field(values, name)
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, _NUMBER_TYPES):
        _violation(violations, path, "source field must be a number or null")
        return None
    return value


def _optional_date(
    values: Mapping[str, Any],
    name: str,
    path: str,
    violations: list[SourceSchemaViolation],
) -> str | None:
    value = _field(values, name)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str):
        _violation(violations, path, "source field must be a date string or null")
        return None
    if not value.strip():
        return None
    try:
        if "T" in value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.date().isoformat()
        return date.fromisoformat(value).isoformat()
    except ValueError:
        _violation(violations, path, "source field must be a valid calendar date")
        return None


def _optional_pdf_url(
    values: Mapping[str, Any],
    *,
    path: str,
    published: bool | None,
    violations: list[SourceSchemaViolation],
) -> str | None:
    value = _field(values, "pdf_url")
    if value is _MISSING or value is None:
        if published is True:
            _violation(
                violations,
                path,
                "PDF URL must be absolute when PDF is published",
                SourceFieldState.ABSENT if value is _MISSING else SourceFieldState.NULL,
            )
        return None
    if not isinstance(value, str):
        _violation(violations, path, "PDF URL must be absolute")
        return None
    if not value.strip():
        if published is True:
            _violation(violations, path, "PDF URL must be absolute", SourceFieldState.EMPTY)
        return None
    if not is_absolute_source_url(value, allow_fixture=True):
        _violation(violations, path, "PDF URL must be absolute")
        return None
    return value


def _parse_oked(
    values: Mapping[str, Any],
    path: str,
    violations: list[SourceSchemaViolation],
) -> OkedAttributes | None:
    value = _field(values, "oked")
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, Mapping):
        _violation(violations, path, "OKED must be an object or null")
        return None
    parsed: dict[str, str | None] = {}
    for name in _OKED_FIELDS:
        item = value.get(name)
        if item is not None and not isinstance(item, str):
            _violation(violations, f"{path}.{name}", "OKED field must be a string or null")
            parsed[name] = None
        else:
            parsed[name] = item
    return OkedAttributes(**parsed)


def _parse_professions(
    values: Mapping[str, Any],
    path: str,
    violations: list[SourceSchemaViolation],
) -> list[ProfessionRecord]:
    value = _field(values, "professions")
    if value is _MISSING or value is None:
        return []
    if not isinstance(value, list):
        _violation(violations, path, "professions must be an array or null")
        return []

    result: list[ProfessionRecord] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, Mapping):
            _violation(violations, item_path, "profession must be an object")
            continue
        name = item.get("name")
        ork_level = item.get("ork_level")
        if name is not None and not isinstance(name, str):
            _violation(violations, f"{item_path}.name", "profession name must be a string or null")
            name = None
        if ork_level is not None and (
            isinstance(ork_level, bool) or not isinstance(ork_level, int) or ork_level < 0
        ):
            _violation(
                violations,
                f"{item_path}.ork_level",
                "profession ORK level must be a non-negative integer or null",
            )
            ork_level = None
        result.append(ProfessionRecord(name=name, ork_level=ork_level))
    return result


def _parse_pdf_published(
    values: Mapping[str, Any],
    path: str,
    violations: list[SourceSchemaViolation],
) -> bool | None:
    value = _field(values, "pdf_published")
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, bool):
        _violation(violations, path, "pdf_published must be a boolean or null")
        return None
    return value


def map_detail_values(
    values: Mapping[str, Any],
    source_url: str,
    *,
    response_format: ResponseFormat,
    paths: DetailFieldPaths = DETAIL_FIELD_PATHS,
    response: SourceResponse | None = None,
) -> DetailPage | ParseError:
    """Validate mapped source fields and construct one complete ``DetailPage``.

    ``values`` is produced by a concrete JSON or HTML adapter.  No field is
    coerced silently: only HTML's representation-level numeric attributes are
    converted by that adapter before this function is called.
    """

    try:
        require_absolute_source_url(source_url, path="$", allow_fixture=True)
    except (TypeError, ValueError) as exc:
        return ParseError(str(exc), url=str(source_url), path="$", invalid_fields=("$",))

    violations: list[SourceSchemaViolation] = []
    enbek_id = _field(values, "enbek_id")
    if enbek_id is _MISSING:
        _violation(violations, paths.json_enbek_id if paths is DETAIL_FIELD_PATHS else paths.html_enbek_id, "required Enbek_ID", SourceFieldState.ABSENT)
        parsed_id: int | None = None
    elif isinstance(enbek_id, bool) or not isinstance(enbek_id, int) or enbek_id <= 0:
        _violation(violations, paths.json_enbek_id if paths is DETAIL_FIELD_PATHS else paths.html_enbek_id, "Enbek_ID must be a positive integer")
        parsed_id = None
    else:
        parsed_id = enbek_id

    title_path = paths.json_title if paths is DETAIL_FIELD_PATHS else paths.html_title
    title = _required_text(values, "title", title_path, "required title", violations)
    code_path = paths.json_code if paths is DETAIL_FIELD_PATHS else paths.html_code
    code = _optional_text(values, "code", code_path, violations)
    government_body = _optional_text(
        values,
        "government_body",
        paths.json_government_body if paths is DETAIL_FIELD_PATHS else paths.html_government_body,
        violations,
    )
    description = _optional_text(
        values,
        "description",
        paths.json_description if paths is DETAIL_FIELD_PATHS else paths.html_description,
        violations,
    )
    version = _optional_number(
        values,
        "version",
        paths.json_version if paths is DETAIL_FIELD_PATHS else paths.html_version,
        violations,
    )
    year = _optional_number(
        values,
        "year",
        paths.json_year if paths is DETAIL_FIELD_PATHS else paths.html_year,
        violations,
    )
    revision_date = _optional_date(
        values,
        "revision_date",
        paths.json_revision_date if paths is DETAIL_FIELD_PATHS else paths.html_revision_date,
        violations,
    )
    oked = _parse_oked(
        values,
        paths.json_oked if paths is DETAIL_FIELD_PATHS else paths.html_oked,
        violations,
    )
    professions = _parse_professions(
        values,
        paths.json_professions if paths is DETAIL_FIELD_PATHS else paths.html_professions,
        violations,
    )
    pdf_published = _parse_pdf_published(
        values,
        paths.json_pdf_published if paths is DETAIL_FIELD_PATHS else paths.html_pdf_published,
        violations,
    )
    pdf_url = _optional_pdf_url(
        values,
        path=paths.json_pdf_url if paths is DETAIL_FIELD_PATHS else paths.html_pdf_url,
        published=pdf_published,
        violations=violations,
    )

    if violations:
        return build_parse_error(source_url, violations, stage="detail_parser")

    # The preceding validation guarantees these values are constructible.  A
    # defensive catch still converts a future model-schema mismatch into a
    # structured error rather than leaking a partial result.
    assert parsed_id is not None
    try:
        attributes = SourceAttributes(
            enbek_id=parsed_id,
            code=code,
            title=title,
            government_body=government_body,
            description=description,
            version=version,
            year=year,
            revision_date=revision_date,
            oked=oked,
            professions=professions,
            registry_url=None,
            page_url=source_url,
            pdf_url=pdf_url,
        )
    except (TypeError, ValueError) as exc:
        return ParseError(
            f"detail attributes do not satisfy source schema: {exc}",
            url=source_url,
            path="$",
            stage="detail_parser",
            invalid_fields=("$",),
        )
    return DetailPage(
        source_url=source_url,
        attributes=attributes,
        response_format=response_format,
        pdf_published=pdf_published,
        response=response,
    )


__all__ = [
    "DETAIL_FIELD_MAPPING",
    "DETAIL_HTML_MAPPING",
    "DETAIL_JSON_MAPPING",
    "DETAIL_SCHEMA_MAPPING",
    "map_detail_values",
]
