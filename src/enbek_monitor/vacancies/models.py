"""Small, source-neutral models for hh vacancy data."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


@dataclass(frozen=True, slots=True)
class VacancyRecord:
    """Normalized public vacancy record keyed by the hh vacancy ID."""

    vacancy_id: str
    title: str | None = None
    url: str | None = None
    alternate_url: str | None = None
    description: str | None = None
    employer: str | None = None
    area: str | None = None
    published_at: str | None = None
    updated_at: str | None = None
    experience: str | None = None
    employment: str | None = None
    schedule: str | None = None
    salary: dict[str, Any] = field(default_factory=dict)
    key_skills: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    fetched_at: str | None = None
    content_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "vacancy_id", _text(self.vacancy_id) or "")
        for name in (
            "title", "url", "alternate_url", "description", "employer",
            "area", "published_at", "updated_at", "experience",
            "employment", "schedule", "fetched_at",
        ):
            object.__setattr__(self, name, _text(getattr(self, name)))
        object.__setattr__(self, "key_skills", tuple(item for item in (_text(value) for value in self.key_skills) if item))
        object.__setattr__(self, "requirements", tuple(item for item in (_text(value) for value in self.requirements) if item))
        if not self.content_sha256:
            digest = hashlib.sha256(_canonical_json(self.to_dict(include_hash=False))).hexdigest()
            object.__setattr__(self, "content_sha256", digest)

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": "1",
            "vacancy_id": self.vacancy_id,
            "title": self.title,
            "url": self.url,
            "alternate_url": self.alternate_url,
            "description": self.description,
            "employer": self.employer,
            "area": self.area,
            "published_at": self.published_at,
            "updated_at": self.updated_at,
            "experience": self.experience,
            "employment": self.employment,
            "schedule": self.schedule,
            "salary": dict(self.salary),
            "key_skills": list(self.key_skills),
            "requirements": list(self.requirements),
            "fetched_at": self.fetched_at,
        }
        if include_hash:
            result["content_sha256"] = self.content_sha256
        return result


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")