"""Explainable first-pass matching between vacancies and standards."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .models import VacancyRecord

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]{3,}", re.IGNORECASE)
_STOPWORDS = {"для", "или", "при", "как", "the", "and", "with"}
MIN_CONFIDENCE = 0.15


def match_records(records: Iterable[VacancyRecord], standards_dir: Path) -> list[dict[str, Any]]:
    standards = _load_standards(standards_dir)
    matches: list[dict[str, Any]] = []
    for record in records:
        vacancy_tokens = _vacancy_tokens(record)
        if not vacancy_tokens:
            continue
        best: dict[str, Any] | None = None
        for standard in standards:
            overlap = vacancy_tokens & standard["tokens"]
            if not overlap:
                continue
            score = round(len(overlap) / max(len(vacancy_tokens), 1), 4)
            if score < MIN_CONFIDENCE:
                continue
            candidate = {
                "standard_id": standard["id"],
                "title": standard["title"],
                "confidence": score,
                "matched_by": [f"token:{token}" for token in sorted(overlap)],
            }
            if best is None or candidate["confidence"] > best["confidence"]:
                best = candidate
        if best is not None:
            matches.append({"vacancy_id": record.vacancy_id, **best})
    return matches


def _load_standards(standards_dir: Path) -> list[dict[str, Any]]:
    result = []
    if not standards_dir.exists():
        return result
    for path in sorted(standards_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            names = [payload.get("title") or ""]
            names.extend(item.get("name") or "" for item in payload.get("professions", []))
        except (OSError, ValueError, TypeError):
            continue
        tokens = _tokens(" ".join(names))
        if not tokens:
            continue
        result.append({"id": payload.get("enbek_id", path.stem), "title": payload.get("title"), "tokens": tokens})
    return result


def _vacancy_tokens(record: VacancyRecord) -> set[str]:
    parts = [record.title or ""]
    parts.extend(record.key_skills)
    parts.extend(record.requirements)
    return _tokens(" ".join(parts))


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN_RE.findall(value) if token.casefold() not in _STOPWORDS}
