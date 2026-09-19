"""Vacancy snapshot history: diff current/ against a new collection.

Gives hh vacancies the same NEW/UPDATED/REMOVED audit trail the enbek
standards have via ``history/changes.jsonl`` — but scoped to
``data/vacancies/`` so the two domains never mix:

* ``data/vacancies/current/{vacancy_id}.json`` — latest snapshot
* ``data/vacancies/reports/{run_id}.json`` — per-run report
* ``data/vacancies/changes.jsonl`` — append-only change log
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .models import VacancyRecord

CHANGES_FILENAME = "changes.jsonl"
CURRENT_DIRNAME = "current"
REPORTS_DIRNAME = "reports"

_COMPARE_FIELDS = (
    "title", "url", "alternate_url", "description", "employer", "area",
    "published_at", "updated_at", "experience", "employment", "schedule",
    "salary", "key_skills", "requirements",
)


def load_previous(current_dir: Path) -> dict[str, dict[str, Any]]:
    """Load the previous snapshot keyed by vacancy_id (tolerant reader)."""
    previous: dict[str, dict[str, Any]] = {}
    if not current_dir.exists():
        return previous
    for path in sorted(current_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        vacancy_id = str(payload.get("vacancy_id") or path.stem).strip()
        if vacancy_id:
            previous[vacancy_id] = payload
    return previous


def diff_snapshots(
    previous: dict[str, dict[str, Any]],
    records: Iterable[VacancyRecord],
    *,
    run_id: str,
    detected_at: str,
) -> dict[str, Any]:
    """Diff normalized records against the previous snapshot."""
    current = {record.vacancy_id: record for record in records}
    new: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    unchanged = 0

    for vacancy_id, record in current.items():
        old = previous.get(vacancy_id)
        if old is None:
            new.append(_event(run_id, detected_at, "NEW", record, None))
            continue
        if old.get("content_sha256") == record.content_sha256:
            unchanged += 1
            continue
        changed_fields = _changed_fields(old, record.to_dict(include_hash=False))
        updated.append(_event(run_id, detected_at, "UPDATED", record, old, changed_fields))

    removed: list[dict[str, Any]] = []
    for vacancy_id, old in previous.items():
        if vacancy_id not in current:
            removed.append({
                "run_id": run_id,
                "detected_at": detected_at,
                "event_type": "REMOVED",
                "vacancy_id": vacancy_id,
                "title": old.get("title"),
                "old_content_sha256": old.get("content_sha256"),
                "new_content_sha256": None,
                "changed_fields": [],
            })

    return {
        "new": new,
        "updated": updated,
        "removed": removed,
        "counts": {
            "new": len(new),
            "updated": len(updated),
            "removed": len(removed),
            "unchanged": unchanged,
            "total": len(current),
        },
    }


def persist_snapshot(
    data_dir: Path,
    records: Iterable[VacancyRecord],
    changes: dict[str, Any],
    *,
    dry_run: bool = False,
) -> None:
    """Write current/ snapshot, prune REMOVED files, append changes log."""
    if dry_run:
        return
    current_dir = data_dir / CURRENT_DIRNAME
    current_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for record in records:
        seen.add(record.vacancy_id)
        (current_dir / f"{record.vacancy_id}.json").write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    for event in changes.get("removed", []):
        path = current_dir / f"{event['vacancy_id']}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
    events = list(changes.get("new", [])) + list(changes.get("updated", [])) + list(changes.get("removed", []))
    if events:
        log_path = data_dir / CHANGES_FILENAME
        with log_path.open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _event(
    run_id: str,
    detected_at: str,
    event_type: str,
    record: VacancyRecord,
    old: dict[str, Any] | None,
    changed_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "detected_at": detected_at,
        "event_type": event_type,
        "vacancy_id": record.vacancy_id,
        "title": record.title,
        "old_content_sha256": (old or {}).get("content_sha256"),
        "new_content_sha256": record.content_sha256,
        "changed_fields": changed_fields or [],
    }


def _changed_fields(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    changed = []
    for field in _COMPARE_FIELDS:
        if old.get(field) != new.get(field):
            changed.append(field)
    return changed
