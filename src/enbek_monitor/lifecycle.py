"""Lifecycle state machine for validated normalized snapshots.

The tracker is a pure planning boundary.  It accepts one complete,
validated candidate and the last successfully committed current snapshot,
then returns classifications, lifecycle sidecar updates and immutable event
objects.  It never changes either input snapshot or any normalized record.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from .diff import DiffEngine
from .models import (
    CandidateSnapshot,
    CurrentSnapshot,
    EventType,
    LifecycleEvent,
    LifecycleState,
    LifecycleStatus,
    NormalizedRecord,
    RunMode,
    SourceReference,
)


_UNCHANGED = "UNCHANGED"
_BASELINE = "BASELINE"


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _record_map(source: Any) -> dict[int, NormalizedRecord]:
    """Return a detached, validated record map from a snapshot-like value."""

    raw_records = _value(source, "records", {})
    if raw_records is None:
        return {}
    if not isinstance(raw_records, Mapping):
        raise TypeError("snapshot records must be a mapping")

    records: dict[int, NormalizedRecord] = {}
    for raw_id, raw_record in raw_records.items():
        if isinstance(raw_id, bool):
            raise TypeError("snapshot record IDs must be positive integers")
        try:
            enbek_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise TypeError("snapshot record IDs must be positive integers") from exc
        if isinstance(raw_record, Mapping):
            raw_record = NormalizedRecord.from_dict(raw_record)
        if not isinstance(raw_record, NormalizedRecord):
            raise TypeError("snapshot records must contain NormalizedRecord values")
        if enbek_id <= 0 or raw_record.enbek_id != enbek_id:
            raise ValueError(f"snapshot record key {raw_id!r} does not match record.enbek_id")
        records[enbek_id] = copy.deepcopy(raw_record)
    return records


def _lifecycle_map(source: Any) -> dict[int, LifecycleStatus]:
    raw_lifecycle = _value(source, "lifecycle", {})
    if raw_lifecycle is None:
        return {}
    if not isinstance(raw_lifecycle, Mapping):
        raise TypeError("snapshot lifecycle must be a mapping")

    lifecycle: dict[int, LifecycleStatus] = {}
    for raw_id, raw_status in raw_lifecycle.items():
        if isinstance(raw_id, bool):
            raise TypeError("snapshot lifecycle IDs must be positive integers")
        try:
            enbek_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise TypeError("snapshot lifecycle IDs must be positive integers") from exc
        if enbek_id <= 0:
            raise ValueError("snapshot lifecycle IDs must be positive integers")
        if isinstance(raw_status, Mapping):
            raw_status = LifecycleStatus(**dict(raw_status))
        if not isinstance(raw_status, LifecycleStatus):
            raise TypeError("snapshot lifecycle values must be LifecycleStatus values")
        lifecycle[enbek_id] = copy.deepcopy(raw_status)
    return lifecycle


def _hash_values(record: NormalizedRecord | None) -> tuple[Any, Any, Any] | None:
    if record is None:
        return None
    hashes = record.hashes
    return (
        hashes.metadata_sha256,
        hashes.pdf_sha256,
        hashes.normalized_sha256,
    )


def _state(value: LifecycleStatus | None, *, has_record: bool) -> LifecycleStatus:
    """Resolve absent sidecar state without changing the supplied sidecar."""

    if value is not None:
        return copy.deepcopy(value)
    if has_record:
        return LifecycleStatus(state=LifecycleState.ACTIVE, missing_count=0)
    return LifecycleStatus(state=LifecycleState.ACTIVE, missing_count=0)


def _candidate_is_eligible(candidate: Any, validation: Any = None) -> bool:
    """Check the publication boundary without doing validation twice.

    A real ``snapshots.CandidateSnapshot`` exposes ``eligible_for_lifecycle``.
    The model-level candidate is also accepted when it is complete and has no
    collection errors; callers may additionally pass a ``ValidationResult``
    (or a boolean) to make the validation decision explicit.  Mapping-based
    candidates must declare ``validated`` when they use that lightweight
    contract, which prevents an unvalidated fixture from reaching lifecycle.
    """

    if validation is not None:
        if isinstance(validation, bool):
            explicit = validation
        else:
            explicit = getattr(validation, "eligible_for_lifecycle", None)
            if explicit is None:
                explicit = getattr(validation, "valid", None)
            if explicit is None and isinstance(validation, Mapping):
                explicit = validation.get("eligible_for_lifecycle", validation.get("valid"))
            if explicit is None:
                return False
        return bool(explicit) and _value(candidate, "complete", False) is True and not bool(
            _value(candidate, "errors", ())
        )

    explicit = _value(candidate, "validated", None)
    if explicit is not None:
        return explicit is True and _value(candidate, "complete", False) is True and not bool(
            _value(candidate, "errors", ())
        )

    eligible = _value(candidate, "eligible_for_lifecycle", None)
    if eligible is not None:
        return bool(eligible)

    return _value(candidate, "complete", False) is True and not bool(
        _value(candidate, "errors", ())
    )


@dataclass
class LifecycleResult:
    """Result of one lifecycle planning operation.

    ``classifications`` contains one classification for every ID observed in
    the candidate and for every ID that changes state due to absence.  A
    removed ID that remains absent is deliberately omitted because it has no
    new lifecycle event or classification.  ``lifecycle`` is a detached
    sidecar suitable for the next Current_Snapshot.
    """

    classifications: dict[int, str]
    events: list[LifecycleEvent]
    lifecycle: dict[int, LifecycleStatus]
    eligible: bool = True

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "classifications",
        "events",
        "lifecycle",
        "eligible",
    )

    @property
    def state(self) -> dict[int, LifecycleStatus]:
        """Alias for callers that refer to the sidecar as the resulting state."""

        return self.lifecycle

    @property
    def event_types(self) -> dict[int, str]:
        return {event.enbek_id: event.event_type.value for event in self.events}

    @property
    def planned_events(self) -> list[LifecycleEvent]:
        return self.events

    def to_dict(self) -> dict[str, Any]:
        return {
            "classifications": {
                str(enbek_id): classification
                for enbek_id, classification in sorted(self.classifications.items())
            },
            "events": [event.to_dict() for event in self.events],
            "lifecycle": {
                str(enbek_id): status.to_dict()
                for enbek_id, status in sorted(self.lifecycle.items())
            },
            "eligible": self.eligible,
        }


class LifecycleTracker:
    """Apply the lifecycle transition rules to one successful candidate."""

    def __init__(self, diff_engine: DiffEngine | None = None) -> None:
        self.diff_engine = diff_engine or DiffEngine()

    def track(
        self,
        candidate: CandidateSnapshot | Mapping[str, Any] | Any,
        current: CurrentSnapshot | Mapping[str, Any] | Any | None = None,
        validation: Any = None,
        *,
        mode: RunMode | str | None = None,
        baseline: bool = False,
    ) -> LifecycleResult:
        """Plan classifications/events without mutating ``candidate``/``current``.

        Failed, incomplete or invalid candidates are ignored and return the
        detached state from ``current``.  ``mode=bootstrap`` (or
        ``baseline=True``) classifies candidate records as ``BASELINE`` and
        emits no ``NEW`` events.
        """

        current_records = _record_map(current)
        current_lifecycle = _lifecycle_map(current)
        copied_state = {enbek_id: copy.deepcopy(status) for enbek_id, status in current_lifecycle.items()}

        try:
            eligible = _candidate_is_eligible(candidate, validation)
            candidate_records = _record_map(candidate) if eligible else {}
        except (TypeError, ValueError, KeyError):
            return LifecycleResult({}, [], copied_state, eligible=False)

        if not eligible:
            return LifecycleResult({}, [], copied_state, eligible=False)

        candidate_mode = mode
        if candidate_mode is None:
            candidate_mode = _value(candidate, "mode", None)
        is_baseline = baseline or candidate_mode in (RunMode.BOOTSTRAP, RunMode.BOOTSTRAP.value)

        classifications: dict[int, str] = {}
        events: list[LifecycleEvent] = []
        all_ids = set(current_records) | set(current_lifecycle) | set(candidate_records)
        run_id = _value(candidate, "run_id", "lifecycle-run") or "lifecycle-run"
        detected_at = _value(candidate, "collected_at", None)

        # One deterministic pass over the union guarantees at most one event
        # per Enbek_ID and stable event order for history/report generation.
        for enbek_id in sorted(all_ids):
            old_record = current_records.get(enbek_id)
            new_record = candidate_records.get(enbek_id)
            prior = _state(current_lifecycle.get(enbek_id), has_record=old_record is not None)

            if new_record is not None:
                if is_baseline and enbek_id not in current_records and enbek_id not in current_lifecycle:
                    classifications[enbek_id] = _BASELINE
                    copied_state[enbek_id] = LifecycleStatus(LifecycleState.ACTIVE, 0)
                    continue

                if prior.state in (LifecycleState.MISSING, LifecycleState.REMOVED):
                    classifications[enbek_id] = EventType.RESTORED.value
                    copied_state[enbek_id] = LifecycleStatus(LifecycleState.ACTIVE, 0)
                    events.append(
                        self._event(
                            candidate,
                            run_id=run_id,
                            detected_at=detected_at,
                            enbek_id=enbek_id,
                            event_type=EventType.RESTORED,
                            old_record=old_record,
                            new_record=new_record,
                            previous=prior,
                            missing_count=0,
                        )
                    )
                    continue

                if old_record is None:
                    classifications[enbek_id] = _BASELINE if is_baseline else EventType.NEW.value
                    copied_state[enbek_id] = LifecycleStatus(LifecycleState.ACTIVE, 0)
                    if not is_baseline:
                        events.append(
                            self._event(
                                candidate,
                                run_id=run_id,
                                detected_at=detected_at,
                                enbek_id=enbek_id,
                                event_type=EventType.NEW,
                                old_record=None,
                                new_record=new_record,
                                previous=None,
                                missing_count=0,
                            )
                        )
                    continue

                copied_state[enbek_id] = LifecycleStatus(LifecycleState.ACTIVE, 0)
                if _hash_values(old_record) != _hash_values(new_record):
                    classifications[enbek_id] = EventType.UPDATED.value
                    events.append(
                        self._event(
                            candidate,
                            run_id=run_id,
                            detected_at=detected_at,
                            enbek_id=enbek_id,
                            event_type=EventType.UPDATED,
                            old_record=old_record,
                            new_record=new_record,
                            previous=prior,
                            missing_count=0,
                        )
                    )
                else:
                    classifications[enbek_id] = _UNCHANGED
                continue

            # No candidate record: only successful complete candidates reach
            # this branch, so absence is meaningful for lifecycle purposes.
            if prior.state is LifecycleState.REMOVED:
                copied_state[enbek_id] = LifecycleStatus(LifecycleState.REMOVED, 2)
                # Repeated REMOVED absence is intentionally idempotent.
                continue
            if prior.state is LifecycleState.MISSING:
                copied_state[enbek_id] = LifecycleStatus(LifecycleState.REMOVED, 2)
                classifications[enbek_id] = EventType.REMOVED.value
                events.append(
                    self._event(
                        candidate,
                        run_id=run_id,
                        detected_at=detected_at,
                        enbek_id=enbek_id,
                        event_type=EventType.REMOVED,
                        old_record=old_record,
                        new_record=None,
                        previous=prior,
                        missing_count=2,
                    )
                )
                continue

            copied_state[enbek_id] = LifecycleStatus(LifecycleState.MISSING, 1)
            classifications[enbek_id] = EventType.MISSING.value
            events.append(
                self._event(
                    candidate,
                    run_id=run_id,
                    detected_at=detected_at,
                    enbek_id=enbek_id,
                    event_type=EventType.MISSING,
                    old_record=old_record,
                    new_record=None,
                    previous=prior,
                    missing_count=1,
                )
            )

        return LifecycleResult(classifications, events, copied_state, eligible=True)

    def _event(
        self,
        candidate: Any,
        *,
        run_id: str,
        detected_at: str | None,
        enbek_id: int,
        event_type: EventType,
        old_record: NormalizedRecord | None,
        new_record: NormalizedRecord | None,
        previous: LifecycleStatus | None,
        missing_count: int,
    ) -> LifecycleEvent:
        source_record = new_record or old_record
        candidate_registry_url = _value(candidate, "registry_url", None)
        timestamp = detected_at or (
            new_record.timestamps.last_seen_at
            if new_record is not None
            else old_record.timestamps.last_seen_at
            if old_record is not None
            else None
        )
        if event_type is EventType.NEW:
            changes = self.diff_engine.synthetic_new(new_record)
        elif event_type is EventType.REMOVED:
            changes = self.diff_engine.synthetic_removed(old_record) if old_record is not None else []
        elif event_type is EventType.UPDATED or event_type is EventType.RESTORED:
            changes = self.diff_engine.diff(old_record, new_record)
        else:
            changes = []

        return LifecycleEvent(
            event_id=f"{run_id}:{enbek_id}:{event_type.value}",
            run_id=str(run_id),
            enbek_id=enbek_id,
            code=source_record.code if source_record is not None else None,
            event_type=event_type,
            detected_at=timestamp,
            old_hashes=copy.deepcopy(old_record.hashes) if old_record is not None else None,
            new_hashes=copy.deepcopy(new_record.hashes) if new_record is not None else None,
            previous_state=previous.state if previous is not None else None,
            new_state=(LifecycleState.MISSING if event_type is EventType.MISSING else
                       LifecycleState.REMOVED if event_type is EventType.REMOVED else
                       LifecycleState.ACTIVE),
            missing_count=missing_count,
            changes=changes,
            source=copy.deepcopy(source_record.source)
            if source_record is not None
            else SourceReference(registry_url=candidate_registry_url),
        )

    # Public aliases used by orchestration code and older callers.
    plan = track
    compare = track
    transition = track
    calculate = track
    __call__ = track


Lifecycle_Tracker = LifecycleTracker
Lifecycle_Plan = LifecycleResult
Lifecycle_Classification = LifecycleResult


__all__ = [
    "LifecycleResult",
    "LifecycleTracker",
    "Lifecycle_Classification",
    "Lifecycle_Plan",
    "Lifecycle_Tracker",
]
