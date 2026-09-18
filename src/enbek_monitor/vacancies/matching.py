"""Explainable first-pass matching between vacancy titles and standards."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .models import VacancyRecord

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]{3,}", re.IGNORECASE)
_STOPWORDS = {"для", "или", "при", "как", "the", "and", "with"}


def match_records(records: Iterable[VacancyRecord], standards_dir: Path) -> list[dict[str, Any]]:
    standards = _load_standards(standards_dir)
    matches: list[dict[str, Any]] = []
    for record in records:
        vacancy_tokens = _tokens(record.title or "")
        candidates = []
        for standard in standards:
            standard_tokens = _tokens(" ".join(standard["names"]))
            overlap = vacancy_tokens & standard_tokens
            if overlap:
                score = round(len(overlap) / max(len(vacancy_tokens), 1), 4)
                candidates.append({
                    "standard_id": standard["id"],
                    "title": standard["title"],
                    "confidence": score,
                    "matched_by": [f"title:{token}" for token in sorted(overlap)],
                })
        if candidates:
            best = max(candidates, key=lambda item: item["confidence"])
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
            result.append({"id": payload.get("enbek_id", path.stem), "title": payload.get("title"), "names": names})
        except (OSError, ValueError, TypeError):
            continue
    return result


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN_RE.findall(value) if token.casefold() not in _STOPWORDS}