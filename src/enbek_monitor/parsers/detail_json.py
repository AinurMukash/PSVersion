"""Pure JSON adapter for Enbek professional-standard detail pages."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..errors import ParseError
from ..network import SourceResponse
from .detail_mapping import map_detail_values
from .source_schema import (
    DETAIL_FIELD_PATHS,
    DetailPage,
    JsonResponseWrapper,
    ResponseFormat,
    SourceSchemaViolation,
    SourceFieldState,
    build_parse_error,
)


class DetailJsonParser:
    """Parse one already received ``$.data`` detail response.

    The parser only decodes ``SourceResponse.content``.  It never performs a
    request and never downloads the PDF referenced by the detail page.
    """

    def parse(self, url: str, response: SourceResponse) -> DetailPage | ParseError:
        try:
            wrapped = JsonResponseWrapper.from_response(response, source_url=url)
        except ParseError as error:
            return error

        payload = wrapped.payload
        violations: list[SourceSchemaViolation] = []
        if not isinstance(payload, Mapping):
            violations.append(
                SourceSchemaViolation("$", "detail response must be an object")
            )
        else:
            if "data" in payload and isinstance(payload["data"], Mapping):
                data = payload["data"]
            elif "id" in payload or "enbek_id" in payload:
                data = payload
            else:
                data = payload.get("data", _MISSING)

            if data is _MISSING:
                violations.append(
                    SourceSchemaViolation(
                        DETAIL_FIELD_PATHS.json_root,
                        "detail data object is required",
                        SourceFieldState.ABSENT,
                    )
                )
            elif not isinstance(data, Mapping):
                violations.append(
                    SourceSchemaViolation(
                        DETAIL_FIELD_PATHS.json_root,
                        "detail data must be an object",
                    )
                )
            else:
                adapted_data = self._adapt_enbek_fields(data)
                return map_detail_values(
                    adapted_data,
                    wrapped.source_url,
                    response_format=ResponseFormat.JSON,
                    paths=DETAIL_FIELD_PATHS,
                    response=response,
                )

        return build_parse_error(wrapped.source_url, violations, stage="detail_parser")

    @staticmethod
    def _adapt_enbek_fields(data: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(data)
        enbek_id = data.get("enbek_id") if data.get("enbek_id") is not None else data.get("id")
        if enbek_id is not None and "enbek_id" not in data:
            result["enbek_id"] = enbek_id
        if "code" not in data and "code_ps" in data:
            result["code"] = data["code_ps"]
        if "title" not in data and "name_ru" in data:
            result["title"] = data["name_ru"]
        if "government_body" not in data:
            agency = data.get("agency")
            if isinstance(agency, Mapping):
                sub = agency.get("agency")
                if isinstance(sub, Mapping) and sub.get("name_ru"):
                    result["government_body"] = sub["name_ru"]
            if "government_body" not in result:
                devs = data.get("developers")
                if isinstance(devs, list) and devs:
                    comp = devs[0].get("company") if isinstance(devs[0], Mapping) else None
                    if isinstance(comp, Mapping) and comp.get("name_ru"):
                        result["government_body"] = comp["name_ru"]
        if "description" not in data:
            desc = data.get("applicationArea_ru") or data.get("description_ru")
            if desc:
                result["description"] = desc
        if "version" in data and isinstance(data["version"], str):
            v_str = data["version"].strip()
            if v_str.isdigit():
                result["version"] = int(v_str)
            else:
                m = re.search(r'\b\d+\b', v_str)
                if m and any(kw in v_str.lower() for kw in ("версия", "нұсқа", "ver", "v", "ред")):
                    result["version"] = int(m.group(0))
        if "year" not in data and "version_year" in data:
            yr = data["version_year"]
            if isinstance(yr, str):
                yr_clean = yr.strip()
                result["year"] = int(yr_clean) if yr_clean.isdigit() else yr
            else:
                result["year"] = yr
        if "oked" not in data and "okeds" in data and isinstance(data["okeds"], list) and data["okeds"]:
            first_oked = data["okeds"][0]
            if isinstance(first_oked, Mapping):
                main_c = first_oked.get("main_code") if isinstance(first_oked.get("main_code"), Mapping) else {}
                sec_c = first_oked.get("secondary_code") if isinstance(first_oked.get("secondary_code"), Mapping) else {}
                result["oked"] = {
                    "section_code": main_c.get("code"),
                    "section_name": main_c.get("name_ru"),
                    "division_code": None,
                    "division_name": None,
                    "group_code": None,
                    "group_name": None,
                    "class_code": None,
                    "class_name": None,
                    "subclass_code": sec_c.get("code"),
                    "subclass_name": sec_c.get("name_ru"),
                }
        if "professions" in data and isinstance(data["professions"], list):
            adapted_profs = []
            for p in data["professions"]:
                if isinstance(p, Mapping):
                    p_name = p.get("name") if p.get("name") is not None else p.get("name_prof_ru")
                    adapted_profs.append({
                        "name": p_name,
                        "ork_level": p.get("ork_level"),
                    })
            result["professions"] = adapted_profs
        if "pdf_url" not in data and enbek_id:
            result["pdf_url"] = f"https://career.enbek.kz/ru/api/professionalstandart/{enbek_id}/download"
        if "pdf_published" not in data:
            result["pdf_published"] = True

        return result

    __call__ = parse


_MISSING = object()

# Public aliases cover the spelling used by source adapters and callers.
DetailJSONParser = DetailJsonParser
JSONDetailParser = DetailJsonParser
DetailParser = DetailJsonParser
parse_detail_json = DetailJsonParser().parse

__all__ = [
    "DetailJsonParser",
    "DetailJSONParser",
    "JSONDetailParser",
    "DetailParser",
    "parse_detail_json",
]
