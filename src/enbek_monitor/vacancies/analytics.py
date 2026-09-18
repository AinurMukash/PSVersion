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
    return {
        "vacancies": len(items),
        "skills": [
            {
                "name": name,
                "count": count,
                "share": round(count / len(items), 4) if items else 0,
            }
            for name, count in skills.most_common()
        ],
    }