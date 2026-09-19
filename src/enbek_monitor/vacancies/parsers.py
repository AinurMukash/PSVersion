"""Pure hh API parsers: no network, no filesystem.

Split out of the collector so search/detail mapping is unit-testable
without fakes, mirroring the enbek ``parsers/`` boundary.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any

from ..errors import ParseError


def parse_search(payload: Any, *, url: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return ``(items, page_info)`` from an hh search response payload.

    ``page_info`` carries ``found/pages/page/per_page`` when present so the
    collector can stop at the API-declared last page instead of guessing.
    """
    if not isinstance(payload, dict):
        raise ParseError(
            "hh search response must be an object",
            url=url, stage="hh search", path="$", invalid_fields=("$",),
        )
    items = payload.get("items")
    if not isinstance(items, list):
        raise ParseError(
            "hh search response must contain items",
            url=url, stage="hh search", path="$.items", invalid_fields=("$.items",),
        )
    page_info = {
        name: payload.get(name)
        for name in ("found", "pages", "page", "per_page")
        if isinstance(payload.get(name), int)
    }
    return [item for item in items if isinstance(item, dict)], page_info


def search_identity(item: dict[str, Any], *, url: str) -> tuple[str, str]:
    """Return ``(vacancy_id, detail_url)`` for one search item."""
    vacancy_id = str(item.get("id", "")).strip()
    if not vacancy_id:
        raise ParseError(
            "hh search item must contain id",
            url=url, stage="hh search", path="$.items.id", invalid_fields=("$.items.id",),
        )
    detail_url = str(item.get("url") or f"https://api.hh.ru/vacancies/{vacancy_id}")
    return vacancy_id, detail_url


def map_detail(payload: Any) -> dict[str, Any]:
    """Map an hh detail payload to normalized ``VacancyRecord`` kwargs."""
    if not isinstance(payload, dict) or not payload.get("id"):
        raise ParseError("hh vacancy detail must contain id", stage="hh detail", path="$.id")
    return {
        "vacancy_id": str(payload["id"]),
        "title": payload.get("name"),
        "url": payload.get("url"),
        "alternate_url": payload.get("alternate_url"),
        "description": plain_text(payload.get("description")),
        "employer": nested_name(payload.get("employer")),
        "area": nested_name(payload.get("area")),
        "published_at": payload.get("published_at"),
        "updated_at": payload.get("updated_at"),
        "experience": nested_name(payload.get("experience")),
        "employment": nested_name(payload.get("employment")),
        "schedule": nested_name(payload.get("schedule")),
        "salary": normalize_salary(payload.get("salary")),
        "key_skills": tuple(
            item.get("name", "") for item in payload.get("key_skills", []) if isinstance(item, dict)
        ),
        "requirements": professional_roles(payload),
    }


def nested_name(value: Any) -> str | None:
    return value.get("name") if isinstance(value, dict) else None


def normalize_salary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("from", "to", "currency", "gross"):
        item = value.get(key)
        if item is None:
            continue
        if key in ("from", "to"):
            if isinstance(item, bool):
                continue
            if isinstance(item, (int, float)):
                result[key] = item
            continue
        if key == "gross":
            if isinstance(item, bool):
                result[key] = item
            continue
        text = str(item).strip()
        if text:
            result[key] = text.upper() if key == "currency" else text
    return result


def professional_roles(payload: dict[str, Any]) -> tuple[str, ...]:
    roles = payload.get("professional_roles")
    if not isinstance(roles, list):
        return ()
    names = []
    for item in roles:
        if isinstance(item, dict) and item.get("name"):
            text = str(item["name"]).strip()
            if text:
                names.append(text)
    return tuple(names)


def plain_text(value: Any) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", str(value)))).strip() or None
