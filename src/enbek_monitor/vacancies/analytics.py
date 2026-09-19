"""Deterministic aggregates for weekly vacancy snapshots."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from .models import VacancyRecord


def summarize(records: Iterable[VacancyRecord]) -> dict[str, Any]:
    items = tuple(records)
    skills = Counter(
        skill.casefold()
        for record in items
        for skill in record.key_skills
        if skill.strip()
    )
    areas = Counter(record.area or "unknown" for record in items)
    employers = Counter(record.employer or "unknown" for record in items)
    experience = Counter(record.experience or "unknown" for record in items)
    employment = Counter(record.employment or "unknown" for record in items)
    currencies = Counter(
        str((record.salary or {}).get("currency", "unknown")).upper() for record in items
    )
    total = len(items)
    return {
        "vacancies": total,
        "skills": _top(skills, total),
        "areas": _top(areas, total),
        "employers": _top(employers, total),
        "experience": _top(experience, total),
        "employment": _top(employment, total),
        "currencies": _top(currencies, total),
    }


def _top(counter: Counter[str], total: int, limit: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "count": count,
            "share": round(count / total, 4) if total else 0,
        }
        for name, count in counter.most_common(limit)
    ]
