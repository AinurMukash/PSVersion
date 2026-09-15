"""Snapshot generation and comparison-state API.

This module is the boundary between a collected candidate and the last
successfully published snapshot.  It deliberately reuses the typed models in
:mod:`enbek_monitor.models`: normalized records remain canonical domain
objects, while lifecycle state is kept in a separate sidecar map.

A candidate may contain records collected before a later failure.  Such a
candidate is still useful for diagnostics, but ``eligible_for_lifecycle`` is
false unless the candidate is complete and has no recorded errors.  A missing
current snapshot is represented by :class:`SnapshotComparisonView.empty`,
which gives diff/lifecycle code a stable empty base instead of a special
``None`` branch.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .canonical_json import CanonicalJsonParser, CanonicalJsonPrinter
from .errors import MonitorError, ParseError
from .models import (
    CandidateSnapshot as ModelCandidateSnapshot,
    CurrentSnapshot as ModelCurrentSnapshot,
    ErrorInfo,
    LifecycleStatus,
    NormalizedRecord,
    SerializableModel,
    SnapshotState,
)


JSONValue = Any


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be a non-empty string")
    return value


def _record_map(value: Mapping[int | str, NormalizedRecord] | None) -> dict[int, NormalizedRecord]:
    """Validate and detach a map keyed by the record's Enbek_ID."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("records must be a mapping keyed by Enbek_ID")
    result: dict[int, NormalizedRecord] = {}
    for raw_id, raw_record in value.items():
        if isinstance(raw_id, bool):
            raise TypeError("records contains a boolean Enbek_ID")
        try:
            record_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise TypeError("records contains a non-integer Enbek_ID") from exc
        if isinstance(raw_record, Mapping):
            raw_record = NormalizedRecord.from_dict(raw_record)
        if not isinstance(raw_record, NormalizedRecord):
            raise TypeError("records values must be NormalizedRecord instances")
        if record_id <= 0 or raw_record.enbek_id != record_id:
            raise ValueError(f"records[{raw_id}] does not match record.enbek_id")
        result[record_id] = copy.deepcopy(raw_record)
    return result


def _lifecycle_map(value: Mapping[int | str, LifecycleStatus] | None) -> dict[int, LifecycleStatus]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("lifecycle must be a mapping keyed by Enbek_ID")
    result: dict[int, LifecycleStatus] = {}
    for raw_id, raw_status in value.items():
        if isinstance(raw_id, bool):
            raise TypeError("lifecycle contains a boolean Enbek_ID")
        try:
            record_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise TypeError("lifecycle contains a non-integer Enbek_ID") from exc
        if record_id <= 0:
            raise ValueError("lifecycle Enbek_ID must be greater than zero")
        if isinstance(raw_status, Mapping):
            raw_status = LifecycleStatus(**dict(raw_status))
        if not isinstance(raw_status, LifecycleStatus):
            raise TypeError("lifecycle values must be LifecycleStatus instances")
        result[record_id] = copy.deepcopy(raw_status)
    return result


@dataclass
class SnapshotGeneration(SerializableModel):
    """Immutable generation metadata shared by snapshot and comparison APIs."""

    generation_id: str
    run_id: str
    registry_url: str
    created_at: str | None = None
    complete: bool = True
    validated: bool = False

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "generation_id",
        "run_id",
        "registry_url",
        "created_at",
        "complete",
        "validated",
    )

    def __post_init__(self) -> None:
        self.generation_id = _required_text(self.generation_id, "generation_id")
        self.run_id = _required_text(self.run_id, "run_id")
        self.registry_url = _required_text(self.registry_url, "registry_url")
        if self.created_at is not None:
            self.created_at = _required_text(self.created_at, "created_at")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be boolean")
        if not isinstance(self.validated, bool):
            raise TypeError("validated must be boolean")

    @classmethod
    def from_candidate(cls, candidate: ModelCandidateSnapshot) -> "SnapshotGeneration":
        if not isinstance(candidate, ModelCandidateSnapshot):
            raise TypeError("candidate must be CandidateSnapshot")
        return cls(
            generation_id=candidate.snapshot_id,
            run_id=candidate.run_id,
            registry_url=candidate.registry_url,
            created_at=candidate.collected_at,
            complete=candidate.complete,
            validated=candidate.complete and not candidate.errors,
        )

    @classmethod
    def from_current(cls, current: SnapshotState) -> "SnapshotGeneration":
        if not isinstance(current, SnapshotState):
            raise TypeError("current must be CurrentSnapshot")
        return cls(
            generation_id=current.generation_id,
            run_id=current.committed_run_id,
            registry_url=current.source_registry_url,
            complete=True,
            validated=True,
        )


@dataclass
class SnapshotComparisonView(SerializableModel):
    """Read-only-shaped view of the last successful comparison base."""

    generation: SnapshotGeneration | None = None
    records: dict[int, NormalizedRecord] = field(default_factory=dict)
    lifecycle: dict[int, LifecycleStatus] = field(default_factory=dict)

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "generation",
        "records",
        "lifecycle",
    )

    def __post_init__(self) -> None:
        if self.generation is not None and not isinstance(self.generation, SnapshotGeneration):
            if isinstance(self.generation, Mapping):
                self.generation = SnapshotGeneration.from_dict(self.generation)
            else:
                raise TypeError("generation must be SnapshotGeneration or null")
        self.records = _record_map(self.records)
        self.lifecycle = _lifecycle_map(self.lifecycle)

    @classmethod
    def empty(cls) -> "SnapshotComparisonView":
        """Return the comparison base used when no current snapshot exists."""

        return cls()

    @classmethod
    def from_current(cls, current: ModelCurrentSnapshot | None) -> "SnapshotComparisonView":
        if current is None:
            return cls.empty()
        if not isinstance(current, SnapshotState):
            raise TypeError("current must be CurrentSnapshot or None")
        return cls(
            generation=SnapshotGeneration.from_current(current),
            records=current.records,
            lifecycle=current.lifecycle,
        )

    @property
    def is_empty(self) -> bool:
        return self.generation is None and not self.records and not self.lifecycle

    @property
    def record_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self.records))

    def get(self, enbek_id: int) -> NormalizedRecord | None:
        return self.records.get(enbek_id)

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "generation": self.generation.to_dict() if self.generation else None,
            "records": {
                str(enbek_id): self.records[enbek_id].to_dict()
                for enbek_id in sorted(self.records)
            },
            "lifecycle": {
                str(enbek_id): self.lifecycle[enbek_id].to_dict()
                for enbek_id in sorted(self.lifecycle)
            },
        }


class CandidateSnapshot(ModelCandidateSnapshot):
    """Collected snapshot, eligible only when complete and error-free."""

    @property
    def incomplete(self) -> bool:
        return not self.complete

    @property
    def failed(self) -> bool:
        return bool(self.errors)

    @property
    def eligible_for_lifecycle(self) -> bool:
        """Whether this candidate may be used for lifecycle comparison."""

        return self.complete and not self.errors

    @property
    def generation_metadata(self) -> SnapshotGeneration:
        return SnapshotGeneration.from_candidate(self)

    @property
    def records_by_enbek_id(self) -> dict[int, NormalizedRecord]:
        return self.records

    def comparison_base(self, current: "CurrentSnapshot | None" = None) -> SnapshotComparisonView:
        """Return the last successful current, or an empty base if absent."""

        return SnapshotComparisonView.from_current(current)

    last_successful_comparison = comparison_base

    def canonical_record(self, enbek_id: int) -> bytes:
        """Serialize one candidate record with the canonical record printer."""

        record = self.records.get(enbek_id)
        if record is None:
            raise KeyError(enbek_id)
        return CanonicalJsonPrinter().print(record)

    def canonical_records(self) -> dict[int, bytes]:
        """Serialize every record independently in deterministic ID order."""

        return {
            enbek_id: CanonicalJsonPrinter().print(self.records[enbek_id])
            for enbek_id in sorted(self.records)
        }


class CurrentSnapshot(ModelCurrentSnapshot):
    """Last successfully validated and committed snapshot generation."""

    @property
    def complete(self) -> bool:
        return True

    @property
    def validated(self) -> bool:
        return True

    @property
    def eligible_for_lifecycle(self) -> bool:
        return True

    @property
    def generation_metadata(self) -> SnapshotGeneration:
        return SnapshotGeneration.from_current(self)

    @property
    def records_by_enbek_id(self) -> dict[int, NormalizedRecord]:
        return self.records

    @property
    def last_successful_comparison(self) -> SnapshotComparisonView:
        return SnapshotComparisonView.from_current(self)

    def canonical_record(self, enbek_id: int) -> bytes:
        record = self.records.get(enbek_id)
        if record is None:
            raise KeyError(enbek_id)
        return CanonicalJsonPrinter().print(record)

    def canonical_records(self) -> dict[int, bytes]:
        """Serialize every record independently in deterministic ID order."""

        return {
            enbek_id: CanonicalJsonPrinter().print(self.records[enbek_id])
            for enbek_id in sorted(self.records)
        }


# Baseline has the same persisted generation contract as CurrentSnapshot.  The
# bootstrap/report layer distinguishes the classification without putting it
# into the record content or lifecycle sidecar.
Baseline = CurrentSnapshot


def empty_comparison_base() -> SnapshotComparisonView:
    """Return an empty base for the first run without a current snapshot."""

    return SnapshotComparisonView.empty()


def last_successful_comparison(current: CurrentSnapshot | None) -> SnapshotComparisonView:
    """Build a comparison view from only the supplied successful current."""

    return SnapshotComparisonView.from_current(current)


def canonical_record_bytes(record: NormalizedRecord) -> bytes:
    """Encode a normalized record using the canonical JSON contract."""

    return CanonicalJsonPrinter().print(record)


def parse_canonical_record(payload: bytes | bytearray | memoryview) -> NormalizedRecord | ParseError:
    """Decode a canonical normalized record without returning partial data."""

    return CanonicalJsonParser().parse(payload)


__all__ = [
    "Baseline",
    "CandidateSnapshot",
    "CurrentSnapshot",
    "SnapshotComparisonView",
    "SnapshotGeneration",
    "canonical_record_bytes",
    "empty_comparison_base",
    "last_successful_comparison",
    "parse_canonical_record",
]
