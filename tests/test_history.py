"""Focused tests for append-only lifecycle event history."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from enbek_monitor.errors import WorkspaceError
from enbek_monitor.models import (
    EventType,
    Hashes,
    LifecycleEvent,
    LifecycleState,
    SourceReference,
)
from enbek_monitor.storage.history import HistoryStore, HistoryStoreError
from enbek_monitor.storage.workspace import WorkspaceGuard


DETECTED_AT = "2025-01-02T03:04:05Z"


def event(event_id: str, *, event_type: EventType = EventType.UPDATED) -> LifecycleEvent:
    return LifecycleEvent(
        event_id=event_id,
        run_id="run-1",
        enbek_id=1001,
        code="001",
        event_type=event_type,
        detected_at=DETECTED_AT,
        old_hashes=Hashes(),
        new_hashes=Hashes(),
        previous_state=LifecycleState.ACTIVE,
        new_state=LifecycleState.ACTIVE,
        missing_count=0,
        changes=[],
        source=SourceReference(
            registry_url="https://example.test/registry",
            page_url="https://example.test/standard/1001",
            pdf_url=None,
        ),
    )


def test_append_writes_one_complete_json_object_and_preserves_existing_prefix(
    tmp_path: Path,
) -> None:
    store = HistoryStore(tmp_path)
    first = event("event-1")
    second = event("event-2", event_type=EventType.NEW)

    store.append_event(first)
    prefix = store.history_path.read_bytes()
    store.append_event(second)
    after = store.history_path.read_bytes()

    assert after.startswith(prefix)
    assert len(after.splitlines()) == 2
    documents = [json.loads(line) for line in after.splitlines()]
    assert [item["event_id"] for item in documents] == ["event-1", "event-2"]
    assert set(documents[0]) == {
        "event_id",
        "run_id",
        "enbek_id",
        "code",
        "event_type",
        "detected_at",
        "old_hashes",
        "new_hashes",
        "previous_state",
        "new_state",
        "missing_count",
        "changes",
        "source",
    }
    assert documents[0]["source"]["page_url"] == "https://example.test/standard/1001"
    assert documents[0]["changes"] == []
    assert store.read_events() == (first, second)


def test_append_batch_validates_before_writing_and_rejects_duplicate_ids(
    tmp_path: Path,
) -> None:
    store = HistoryStore(tmp_path)
    store.append_event(event("existing"))
    before = store.history_path.read_bytes()

    with pytest.raises(HistoryStoreError, match="already present"):
        store.append([event("new"), event("existing")])
    assert store.history_path.read_bytes() == before

    with pytest.raises(HistoryStoreError, match="already present"):
        store.append([event("new-a"), event("new-a")])
    assert store.history_path.read_bytes() == before


def test_empty_append_for_unchanged_failed_or_dry_run_is_a_no_op(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)

    assert store.append([]) == ()
    assert store.append(None) == ()
    assert not store.history_path.exists()


def test_invalid_event_schema_does_not_modify_existing_history(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.append_event(event("existing"))
    before = store.history_path.read_bytes()
    invalid = event("invalid").to_dict()
    del invalid["source"]

    with pytest.raises(HistoryStoreError, match="missing required fields.*source"):
        store.append(invalid)

    assert store.history_path.read_bytes() == before


def test_existing_invalid_jsonl_is_rejected_without_appending(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.history_path.parent.mkdir(parents=True)
    store.history_path.write_bytes(b"not-json\n")
    before = store.history_path.read_bytes()

    with pytest.raises(HistoryStoreError, match="not valid JSON"):
        store.append_event(event("event-1"))

    assert store.history_path.read_bytes() == before


def test_history_path_uses_workspace_guard_and_rejects_escape(tmp_path: Path) -> None:
    store = HistoryStore(WorkspaceGuard(tmp_path))
    assert store.history_path == tmp_path / "history" / "changes.jsonl"
    assert store.history_path.is_relative_to(tmp_path)

    with pytest.raises(WorkspaceError):
        store.guard.resolve_path("../changes.jsonl")

    with pytest.raises(ValueError, match="single path-safe"):
        HistoryStore(tmp_path, filename="../outside.jsonl")
