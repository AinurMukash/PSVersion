from __future__ import annotations

import json
from pathlib import Path

import pytest

from enbek_monitor.models import (
    EventType,
    Hashes,
    LifecycleEvent,
    LifecycleState,
    NormalizedRecord,
    SourceReference,
    Timestamps,
)
from enbek_monitor.storage.history import HistoryStore
from enbek_monitor.sync import SyncPipeline


def test_sync_pipeline_lifecycle_event_generation_and_persistence(tmp_path: Path) -> None:
    pipeline = SyncPipeline(workspace_root=tmp_path)
    pipeline._ensure_dirs()

    # Create dummy existing standard 1001
    source = SourceReference(
        registry_url="https://career.enbek.kz/ru/api/professionalstandart?page=1",
        page_url="https://career.enbek.kz/ru/api/professionalstandart/1001",
        pdf_url="https://career.enbek.kz/ru/api/professionalstandart/1001/download",
    )
    existing_rec = NormalizedRecord(
        enbek_id=1001,
        code="TEST01",
        title="Existing Standard",
        hashes=Hashes(
            metadata_sha256="a" * 64,
            pdf_sha256="b" * 64,
            normalized_sha256="c" * 64,
        ),
        timestamps=Timestamps(
            first_seen_at="2026-09-01T00:00:00Z",
            last_seen_at="2026-09-01T00:00:00Z",
            last_changed_at="2026-09-01T00:00:00Z",
        ),
        source=source,
    )
    (pipeline.standards_dir / "1001.json").write_text(existing_rec.to_json(), encoding="utf-8")

    # Updated standard 1001 and New standard 1002
    updated_rec = NormalizedRecord(
        enbek_id=1001,
        code="TEST01",
        title="Updated Standard Title",
        hashes=Hashes(
            metadata_sha256="d" * 64,
            pdf_sha256="b" * 64,
            normalized_sha256="e" * 64,
        ),
        timestamps=Timestamps(
            first_seen_at="2026-09-01T00:00:00Z",
            last_seen_at="2026-10-05T00:00:00Z",
            last_changed_at="2026-10-05T00:00:00Z",
        ),
        source=source,
    )
    new_rec = NormalizedRecord(
        enbek_id=1002,
        code="TEST02",
        title="New Standard",
        hashes=Hashes(
            metadata_sha256="1" * 64,
            pdf_sha256="2" * 64,
            normalized_sha256="3" * 64,
        ),
        timestamps=Timestamps(
            first_seen_at="2026-10-05T00:00:00Z",
            last_seen_at="2026-10-05T00:00:00Z",
            last_changed_at="2026-10-05T00:00:00Z",
        ),
        source=source,
    )

    candidates = {1001: updated_rec, 1002: new_rec}
    existing_records = {1001: existing_rec}
    run_date = "2026-10-05"
    start_iso = "2026-10-05T12:00:00Z"

    new_events: list[LifecycleEvent] = []
    for enbek_id, candidate in candidates.items():
        existing = existing_records.get(enbek_id)
        if existing is None:
            diff_res = pipeline.diff_engine.synthetic_new(candidate)
            ev = LifecycleEvent(
                event_id=f"run-{run_date}:{enbek_id}:NEW",
                run_id=f"run-{run_date}",
                enbek_id=enbek_id,
                code=candidate.code,
                event_type=EventType.NEW,
                detected_at=start_iso,
                new_hashes=candidate.hashes,
                new_state=LifecycleState.ACTIVE,
                changes=diff_res,
                source=candidate.source,
            )
            new_events.append(ev)
        else:
            if candidate.hashes.normalized_sha256 != existing.hashes.normalized_sha256:
                diff_res = pipeline.diff_engine.diff(existing, candidate)
                ev = LifecycleEvent(
                    event_id=f"run-{run_date}:{enbek_id}:UPDATED",
                    run_id=f"run-{run_date}",
                    enbek_id=enbek_id,
                    code=candidate.code,
                    event_type=EventType.UPDATED,
                    detected_at=start_iso,
                    old_hashes=existing.hashes,
                    new_hashes=candidate.hashes,
                    previous_state=LifecycleState.ACTIVE,
                    new_state=LifecycleState.ACTIVE,
                    changes=diff_res,
                    source=candidate.source,
                )
                new_events.append(ev)

    assert len(new_events) == 2
    # Append events through HistoryStore
    pipeline.history_store.append(new_events)

    changes_file = pipeline.history_dir / "changes.jsonl"
    assert changes_file.exists()
    lines = changes_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    parsed_events = [json.loads(line) for line in lines]
    assert parsed_events[0]["event_type"] == "UPDATED"
    assert parsed_events[0]["enbek_id"] == 1001
    assert parsed_events[1]["event_type"] == "NEW"
    assert parsed_events[1]["enbek_id"] == 1002

    # Save candidates to standards_dir and verify doc generator runs smoothly
    (pipeline.standards_dir / "1001.json").write_text(updated_rec.to_json(), encoding="utf-8")
    (pipeline.standards_dir / "1002.json").write_text(new_rec.to_json(), encoding="utf-8")
    pipeline.doc_generator.generate_all({"standards_total": 2, "updated_count": 1, "new_count": 1})

    assert (tmp_path / "README.md").exists()
    assert (tmp_path / "CHANGELOG.md").exists()
    assert (tmp_path / "docs" / "standards" / "1001.md").exists()
    assert (tmp_path / "docs" / "standards" / "1002.md").exists()

    issue_summary = pipeline.doc_generator.generate_issue_summary()
    assert "Резюме обновления" in issue_summary
    assert "1001" in issue_summary
    assert "1002" in issue_summary
    assert len(issue_summary) < 60000
