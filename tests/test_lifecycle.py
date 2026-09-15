"""Unit coverage for the complete-snapshot lifecycle state machine."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from enbek_monitor.lifecycle import LifecycleTracker
from enbek_monitor.models import ErrorInfo, LifecycleState, LifecycleStatus, NormalizedRecord
from enbek_monitor.snapshots import CandidateSnapshot, CurrentSnapshot


PAIR_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "snapshots" / "pairs"


def _record(fixture_manifest, relative_path: str) -> NormalizedRecord:
    path = fixture_manifest.resolve(relative_path)
    return NormalizedRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _snapshot_pair(fixture_manifest, scenario: str):
    document = json.loads((PAIR_ROOT / f"{scenario.lower()}.json").read_text(encoding="utf-8"))

    def records(payload):
        return {
            int(enbek_id): _record(fixture_manifest, relative_path)
            for enbek_id, relative_path in payload.get("records", {}).items()
        }

    current_payload = document["current_snapshot"]
    current_records = records(current_payload)
    current = CurrentSnapshot(
        generation_id=f"generation-{scenario.lower()}",
        records=current_records,
        lifecycle={
            int(enbek_id): LifecycleStatus(**status)
            for enbek_id, status in current_payload.get("lifecycle", {}).items()
        },
        source_registry_url="https://career.enbek.kz/ru/ps-registry",
        committed_run_id=f"previous-{scenario.lower()}",
        content_manifest_hash="a" * 64,
    )

    candidate_records = records(document["candidate_snapshot"])
    candidate = CandidateSnapshot(
        snapshot_id=f"snapshot-{scenario.lower()}",
        run_id=f"run-{scenario.lower()}",
        registry_url="https://career.enbek.kz/ru/ps-registry",
        collected_at="2025-01-20T08:00:00Z",
        complete=document["candidate_snapshot"]["complete"],
        records=candidate_records,
    )
    return document, current, candidate


@pytest.mark.parametrize("scenario", ["NEW", "UPDATED", "UNCHANGED", "MISSING", "REMOVED", "RESTORED"])
def test_tracker_matches_fixed_snapshot_pair(fixture_manifest, scenario: str) -> None:
    expected, current, candidate = _snapshot_pair(fixture_manifest, scenario)

    result = LifecycleTracker().track(candidate, current)

    assert result.eligible is True
    assert result.classifications == {
        int(enbek_id): classification
        for enbek_id, classification in expected["expected"]["classifications"].items()
    }
    assert {event.enbek_id: event.event_type.value for event in result.events} == {
        int(enbek_id): event_type
        for enbek_id, event_type in expected["expected"]["event_types"].items()
    }
    for enbek_id, status in expected["expected"].get("lifecycle", {}).items():
        actual = result.lifecycle[int(enbek_id)]
        assert actual.state.value == status["state"]
        assert actual.missing_count == status["missing_count"]

    if scenario == "UPDATED":
        updated = result.events[0]
        assert updated.event_type.value == "UPDATED"
        assert {change.path for change in updated.changes} >= set(expected["expected"]["diff_paths"])
    if scenario in {"NEW", "REMOVED"}:
        assert result.events[0].changes


def test_tracker_ignores_incomplete_candidate_and_preserves_current_state(fixture_manifest) -> None:
    record = _record(fixture_manifest, "normalized/valid/standard-1001.json")
    current = CurrentSnapshot(
        generation_id="generation-1",
        records={record.enbek_id: record},
        lifecycle={record.enbek_id: LifecycleStatus(LifecycleState.ACTIVE, 0)},
        source_registry_url=record.source.registry_url,
        committed_run_id="run-1",
        content_manifest_hash="b" * 64,
    )
    candidate = CandidateSnapshot(
        snapshot_id="snapshot-failed",
        run_id="run-failed",
        registry_url=record.source.registry_url,
        collected_at="2025-01-20T08:00:00Z",
        complete=False,
        records={},
        errors=[ErrorInfo(stage="detail", reason="detail unavailable", url=record.source.page_url)],
    )
    before = copy.deepcopy(current.to_dict())

    result = LifecycleTracker().track(candidate, current)

    assert result.eligible is False
    assert result.classifications == {}
    assert result.events == []
    assert result.to_dict()["lifecycle"] == {"1001": {"state": "ACTIVE", "missing_count": 0}}
    assert current.to_dict() == before


def test_bootstrap_classifies_records_as_baseline_without_new_events(fixture_manifest) -> None:
    expected, current, candidate = _snapshot_pair(fixture_manifest, "NEW")
    # Bootstrap normally has no current; passing one here also verifies that
    # the explicit mode never creates NEW for a candidate-only record.
    result = LifecycleTracker().track(candidate, current, mode="bootstrap")

    assert result.events == []
    assert result.classifications[1002] == "BASELINE"
    assert result.lifecycle[1002] == LifecycleStatus(LifecycleState.ACTIVE, 0)


def test_tracker_does_not_mutate_records_or_lifecycle_sidecar(fixture_manifest) -> None:
    _, current, candidate = _snapshot_pair(fixture_manifest, "UPDATED")
    current_before = copy.deepcopy(current.to_dict())
    candidate_before = copy.deepcopy(candidate.to_dict())

    result = LifecycleTracker().track(candidate, current)
    result.lifecycle[1001].missing_count = 99

    assert current.to_dict() == current_before
    assert candidate.to_dict() == candidate_before
