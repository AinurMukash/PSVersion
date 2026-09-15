"""Immutable snapshot-generation persistence for the local monitor.

A snapshot is published in two stages.  First a complete generation is built
under a private directory and moved into ``data/snapshots/generations``.  Only
then is the small ``current.json`` or ``baseline.json`` pointer atomically
replaced.  Readers therefore observe either the previous pointer and its
complete generation or the new pointer and its complete generation; they
never read a partially-written record set.

The store is deliberately independent of orchestration and lifecycle logic.
It accepts a validated candidate (or an already committed ``SnapshotState``),
serializes every record through the canonical JSON printer, and keeps
lifecycle/derived data beside the immutable generation.  All filesystem paths
are resolved through :class:`WorkspaceGuard`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from ..canonical_json import CanonicalJsonParser, CanonicalJsonPrinter
from ..errors import CommitError, ParseError, WorkspaceError
from ..models import (
    CandidateSnapshot as ModelCandidateSnapshot,
    LifecycleState,
    LifecycleStatus,
    NormalizedRecord,
    SerializableModel,
    SnapshotState,
)
from .workspace import WorkspaceGuard

_SCHEMA_VERSION = "1"
_MANIFEST_NAME = "manifest.json"
_LIFECYCLE_NAME = "lifecycle.json"
_RECORDS_DIRECTORY = "standards"
_DERIVED_DIRECTORY = "derived"
_POINTER_NAMES = {"current": "current.json", "baseline": "baseline.json"}
_DERIVED_NAMES = ("registry.json", "categories.json", "stats.json", "sync_state.json")


def _safe_component(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommitError(f"{field_name} must be a non-empty path-safe string", stage="snapshot persistence")
    value = value.strip()
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise CommitError(f"{field_name} must be path-safe", stage="snapshot persistence")
    return value


def _json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
                separators=(",", ": "),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CommitError(
            f"snapshot metadata is not JSON serializable: {exc}",
            stage="snapshot serialization",
        ) from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("snapshot write made no progress")
        view = view[written:]


def _write_new_file(path: Path, content: bytes) -> None:
    """Create one immutable file and fsync it; never truncate an existing one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CommitError(
            "refusing to overwrite an immutable snapshot file",
            path=str(path),
            stage="snapshot generation",
        ) from exc
    try:
        _write_all(fd, content)
        os.fsync(fd)
    except OSError as exc:
        raise CommitError(
            f"could not write snapshot file: {exc}",
            path=str(path),
            stage="snapshot generation",
        ) from exc
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync; Windows does not expose it normally."""

    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _record_map(value: Mapping[int | str, NormalizedRecord]) -> dict[int, NormalizedRecord]:
    if not isinstance(value, Mapping):
        raise TypeError("snapshot records must be a mapping keyed by Enbek_ID")
    result: dict[int, NormalizedRecord] = {}
    for raw_id, raw_record in value.items():
        if isinstance(raw_id, bool):
            raise TypeError("snapshot records contain a boolean Enbek_ID")
        try:
            record_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise TypeError("snapshot records contain a non-integer Enbek_ID") from exc
        if not isinstance(raw_record, NormalizedRecord):
            if isinstance(raw_record, Mapping):
                raw_record = NormalizedRecord.from_dict(raw_record)
            else:
                raise TypeError("snapshot records must contain NormalizedRecord values")
        if record_id <= 0 or raw_record.enbek_id != record_id:
            raise ValueError(f"snapshot record key {raw_id!r} does not match record.enbek_id")
        result[record_id] = copy.deepcopy(raw_record)
    return result


def _lifecycle_map(value: Mapping[int | str, LifecycleStatus] | None, record_ids: set[int]) -> dict[int, LifecycleStatus]:
    if value is None:
        return {
            record_id: LifecycleStatus(state=LifecycleState.ACTIVE, missing_count=0)
            for record_id in sorted(record_ids)
        }
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
        if not isinstance(raw_status, LifecycleStatus):
            if not isinstance(raw_status, Mapping):
                raise TypeError("lifecycle values must be LifecycleStatus instances")
            raw_status = LifecycleStatus(**dict(raw_status))
        result[record_id] = copy.deepcopy(raw_status)
    return result


def _normalise_derived(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("derived manifests must be a mapping of filename to JSON value")
    result: dict[str, Any] = {}
    for raw_name, payload in value.items():
        name = _safe_component(raw_name, "derived manifest name")
        if not name.endswith(".json") or name == ".json":
            raise ValueError("derived manifest names must be JSON filenames")
        # Round-trip through the JSON encoder to reject non-JSON values and to
        # detach caller-owned mutable objects before generation is published.
        result[name] = json.loads(_json_bytes(payload).decode("utf-8"))
    return result


@dataclass(frozen=True)
class SnapshotPointer(SerializableModel):
    """Small atomic pointer from a logical snapshot name to a generation."""

    pointer: str
    generation_id: str
    manifest_path: str
    manifest_sha256: str
    schema_version: str = _SCHEMA_VERSION

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "pointer",
        "generation_id",
        "manifest_path",
        "manifest_sha256",
    )


@dataclass(frozen=True)
class SnapshotManifest(SerializableModel):
    """Validated metadata for one immutable generation."""

    schema_version: str
    generation_id: str
    run_id: str
    registry_url: str
    created_at: str | None
    complete: bool
    validated: bool
    record_count: int
    records: Mapping[str, Mapping[str, Any]]
    lifecycle_path: str
    lifecycle_sha256: str
    derived: Mapping[str, Mapping[str, Any]]
    content_manifest_hash: str

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "generation_id",
        "run_id",
        "registry_url",
        "created_at",
        "complete",
        "validated",
        "record_count",
        "records",
        "lifecycle_path",
        "lifecycle_sha256",
        "derived",
        "content_manifest_hash",
    )

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "SnapshotManifest":
        if not isinstance(document, Mapping):
            raise ParseError("snapshot manifest must be a JSON object", path="$", stage="snapshot reader")
        try:
            value = cls(**dict(document))
        except (TypeError, ValueError) as exc:
            raise ParseError(
                f"invalid snapshot manifest: {exc}",
                path="$.manifest",
                stage="snapshot reader",
            ) from exc
        return value


@dataclass(frozen=True)
class LoadedSnapshot(SerializableModel):
    """Generation read with verified canonical record bytes."""

    manifest: SnapshotManifest
    records: Mapping[int, NormalizedRecord]
    lifecycle: Mapping[int, LifecycleStatus]
    record_bytes: Mapping[int, bytes]
    derived: Mapping[str, Any]

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "manifest",
        "records",
        "lifecycle",
        "record_bytes",
        "derived",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "records": {str(key): value.to_dict() for key, value in sorted(self.records.items())},
            "lifecycle": {str(key): value.to_dict() for key, value in sorted(self.lifecycle.items())},
            "record_bytes": {str(key): value.decode("utf-8") for key, value in sorted(self.record_bytes.items())},
            "derived": copy.deepcopy(dict(self.derived)),
        }


class SnapshotStore:
    """Persist and atomically publish immutable snapshot generations."""

    def __init__(self, workspace: WorkspaceGuard | str | Path) -> None:
        self.guard = workspace if isinstance(workspace, WorkspaceGuard) else WorkspaceGuard(workspace)
        self.guard.preflight()

    @property
    def data_root(self) -> Path:
        return self.guard.resolve_path(
            self.guard.storage_dirs.get("data_dir", Path("data")), allow_directory=True
        )

    @property
    def snapshots_root(self) -> Path:
        return self.guard.resolve_path(self.data_root / "snapshots", allow_directory=True)

    @property
    def generations_root(self) -> Path:
        return self.guard.resolve_path(self.snapshots_root / "generations", allow_directory=True)

    def pointer_path(self, name: str = "current") -> Path:
        name = _safe_component(name, "pointer name")
        if name not in _POINTER_NAMES:
            raise ValueError("pointer name must be current or baseline")
        return self.guard.resolve_path(self.snapshots_root / _POINTER_NAMES[name])

    def generation_path(self, generation_id: str) -> Path:
        generation_id = _safe_component(generation_id, "generation_id")
        return self.guard.resolve_path(self.generations_root / generation_id, allow_directory=True)

    def write_generation(
        self,
        snapshot: ModelCandidateSnapshot | SnapshotState,
        *,
        lifecycle: Mapping[int | str, LifecycleStatus] | None = None,
        derived_manifests: Mapping[str, Any] | None = None,
    ) -> SnapshotManifest:
        """Build one complete immutable generation, but do not change pointers.

        A candidate that is incomplete or contains recorded errors is rejected
        before any generation directory is created.  This is the persistence
        boundary that prevents failed collection results from becoming a
        current/baseline pointer.
        """

        if isinstance(snapshot, ModelCandidateSnapshot):
            if not snapshot.complete or snapshot.errors:
                raise CommitError(
                    "incomplete or failed candidate cannot be persisted as a snapshot generation",
                    stage="snapshot validation",
                    path="$.candidate",
                )
            generation_id = snapshot.snapshot_id
            run_id = snapshot.run_id
            registry_url = snapshot.registry_url
            created_at = snapshot.collected_at
            records = _record_map(snapshot.records)
            is_validated = True
        elif isinstance(snapshot, SnapshotState):
            generation_id = snapshot.generation_id
            run_id = snapshot.committed_run_id
            registry_url = snapshot.source_registry_url
            created_at = None
            records = _record_map(snapshot.records)
            is_validated = True
            if lifecycle is None:
                lifecycle = snapshot.lifecycle
        else:
            raise TypeError("snapshot must be CandidateSnapshot or SnapshotState")

        generation_id = _safe_component(generation_id, "generation_id")
        if self.generation_path(generation_id).exists():
            raise CommitError(
                f"immutable snapshot generation already exists: {generation_id}",
                path=str(self.generation_path(generation_id)),
                stage="snapshot generation",
            )
        statuses = _lifecycle_map(lifecycle, set(records))
        derived = _normalise_derived(derived_manifests)
        # The four data manifests are generation-local and deterministic.  A
        # caller may replace any of them with a richer derived projection.
        defaults = self._default_derived(generation_id, registry_url, records, statuses)
        for name, payload in defaults.items():
            derived.setdefault(name, payload)

        canonical_records: dict[int, bytes] = {}
        record_entries: dict[str, dict[str, Any]] = {}
        printer = CanonicalJsonPrinter()
        for record_id in sorted(records):
            payload = printer.print(records[record_id])
            canonical_records[record_id] = payload
            record_entries[str(record_id)] = {
                "path": f"{_RECORDS_DIRECTORY}/{record_id}.json",
                "sha256": _sha256(payload),
                "byte_length": len(payload),
            }

        content_basis = _json_bytes({"records": record_entries})
        content_manifest_hash = _sha256(content_basis)
        lifecycle_payload = {
            str(record_id): statuses[record_id].to_dict()
            for record_id in sorted(statuses)
        }
        lifecycle_bytes = _json_bytes(lifecycle_payload)
        derived_entries = {
            name: {
                "path": f"{_DERIVED_DIRECTORY}/{name}",
                "sha256": _sha256(_json_bytes(payload)),
                "byte_length": len(_json_bytes(payload)),
            }
            for name, payload in sorted(derived.items())
        }
        manifest_document: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "generation_id": generation_id,
            "run_id": _safe_component(run_id, "run_id"),
            "registry_url": registry_url,
            "created_at": created_at,
            "complete": True,
            "validated": is_validated,
            "record_count": len(records),
            "records": record_entries,
            "lifecycle_path": _LIFECYCLE_NAME,
            "lifecycle_sha256": _sha256(lifecycle_bytes),
            "derived": derived_entries,
            "content_manifest_hash": content_manifest_hash,
        }
        manifest_bytes = _json_bytes(manifest_document)
        manifest = SnapshotManifest.from_document(manifest_document)

        staging = self.guard.resolve_path(
            self.generations_root / f".staging-{generation_id}-{secrets.token_hex(8)}",
            allow_directory=True,
        )
        final = self.generation_path(generation_id)
        try:
            staging.mkdir(parents=True, exist_ok=False)
            records_dir = self.guard.resolve_path(staging / _RECORDS_DIRECTORY, allow_directory=True)
            derived_dir = self.guard.resolve_path(staging / _DERIVED_DIRECTORY, allow_directory=True)
            records_dir.mkdir()
            derived_dir.mkdir()
            for record_id, payload in canonical_records.items():
                _write_new_file(
                    self.guard.resolve_path(records_dir / f"{record_id}.json"), payload
                )
            _write_new_file(self.guard.resolve_path(staging / _LIFECYCLE_NAME), lifecycle_bytes)
            for name, payload in derived.items():
                _write_new_file(
                    self.guard.resolve_path(derived_dir / name), _json_bytes(payload)
                )
            _write_new_file(self.guard.resolve_path(staging / _MANIFEST_NAME), manifest_bytes)
            _fsync_directory(records_dir)
            _fsync_directory(derived_dir)
            _fsync_directory(staging)
            if final.exists():
                raise CommitError(
                    f"immutable snapshot generation already exists: {generation_id}",
                    path=str(final),
                    stage="snapshot generation",
                )
            os.rename(staging, final)
            _fsync_directory(self.generations_root)
        except CommitError:
            self._remove_staging(staging)
            raise
        except (OSError, ValueError) as exc:
            self._remove_staging(staging)
            raise CommitError(
                f"could not publish immutable snapshot generation: {exc}",
                path=str(final),
                stage="snapshot generation",
            ) from exc
        return manifest

    def publish_current(self, generation: str | SnapshotManifest) -> SnapshotPointer:
        return self._publish_pointer("current", generation)

    def publish_baseline(self, generation: str | SnapshotManifest) -> SnapshotPointer:
        return self._publish_pointer("baseline", generation)

    def publish(
        self,
        snapshot: ModelCandidateSnapshot | SnapshotState,
        *,
        baseline: bool = False,
        lifecycle: Mapping[int | str, LifecycleStatus] | None = None,
        derived_manifests: Mapping[str, Any] | None = None,
    ) -> tuple[SnapshotManifest, SnapshotPointer]:
        """Persist a complete snapshot and atomically publish one pointer."""

        manifest = self.write_generation(
            snapshot, lifecycle=lifecycle, derived_manifests=derived_manifests
        )
        pointer = self.publish_baseline(manifest) if baseline else self.publish_current(manifest)
        return manifest, pointer

    def read_pointer(self, name: str = "current") -> SnapshotPointer | None:
        path = self.pointer_path(name)
        if not path.exists():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            pointer = SnapshotPointer(**document)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ParseError(
                f"invalid {name} snapshot pointer: {exc}",
                path=f"$.{name}",
                stage="snapshot reader",
            ) from exc
        if pointer.pointer != name:
            raise ParseError(
                f"snapshot pointer kind is {pointer.pointer!r}, expected {name!r}",
                path=f"$.{name}.pointer",
                stage="snapshot reader",
            )
        manifest_path = self.guard.resolve_path(pointer.manifest_path)
        if not manifest_path.is_file():
            raise ParseError(
                "snapshot pointer references a missing manifest",
                path=f"$.{name}.manifest_path",
                stage="snapshot reader",
            )
        if _sha256(manifest_path.read_bytes()) != pointer.manifest_sha256:
            raise ParseError(
                "snapshot pointer manifest hash does not match its file",
                path=f"$.{name}.manifest_sha256",
                stage="snapshot reader",
            )
        return pointer

    def read_generation(self, generation: str | SnapshotPointer) -> LoadedSnapshot:
        generation_id = generation.generation_id if isinstance(generation, SnapshotPointer) else generation
        generation_id = _safe_component(generation_id, "generation_id")
        root = self.generation_path(generation_id)
        manifest_path = self.guard.resolve_path(root / _MANIFEST_NAME)
        if not manifest_path.is_file():
            raise ParseError("snapshot generation manifest is missing", path="$.manifest", stage="snapshot reader")
        try:
            manifest_document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ParseError(f"could not read snapshot manifest: {exc}", path="$.manifest", stage="snapshot reader") from exc
        manifest = SnapshotManifest.from_document(manifest_document)
        if manifest.generation_id != generation_id or not manifest.complete or not manifest.validated:
            raise ParseError("snapshot generation is not complete and validated", path="$.complete", stage="snapshot reader")
        if manifest.content_manifest_hash != _sha256(_json_bytes({"records": manifest.records})):
            raise ParseError("snapshot content manifest hash does not match", path="$.content_manifest_hash", stage="snapshot reader")

        records: dict[int, NormalizedRecord] = {}
        record_bytes: dict[int, bytes] = {}
        parser = CanonicalJsonParser()
        for raw_id, entry in sorted(manifest.records.items(), key=lambda item: int(item[0])):
            try:
                record_id = int(raw_id)
                relative = entry["path"]
                path = self.guard.resolve_path(root / relative)
                payload = path.read_bytes()
            except (KeyError, TypeError, ValueError, OSError) as exc:
                raise ParseError(f"snapshot record {raw_id} cannot be read: {exc}", path=f"$.records.{raw_id}", stage="snapshot reader") from exc
            if _sha256(payload) != entry.get("sha256") or len(payload) != entry.get("byte_length"):
                raise ParseError("snapshot record bytes do not match manifest", path=f"$.records.{raw_id}", stage="snapshot reader")
            parsed = parser.parse(payload)
            if isinstance(parsed, ParseError):
                raise parsed
            if parsed.enbek_id != record_id:
                raise ParseError("snapshot record ID does not match its path", path=f"$.records.{raw_id}", stage="snapshot reader")
            records[record_id] = parsed
            record_bytes[record_id] = payload

        lifecycle_path = self.guard.resolve_path(root / manifest.lifecycle_path)
        try:
            lifecycle_document = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ParseError(f"could not read lifecycle sidecar: {exc}", path="$.lifecycle", stage="snapshot reader") from exc
        lifecycle = _lifecycle_map(lifecycle_document, set(records))
        lifecycle_bytes = _json_bytes({str(key): lifecycle[key].to_dict() for key in sorted(lifecycle)})
        if _sha256(lifecycle_bytes) != manifest.lifecycle_sha256:
            raise ParseError("lifecycle sidecar hash does not match manifest", path="$.lifecycle_sha256", stage="snapshot reader")

        derived: dict[str, Any] = {}
        for name, entry in sorted(manifest.derived.items()):
            path = self.guard.resolve_path(root / entry["path"])
            try:
                payload = path.read_bytes()
                value = json.loads(payload.decode("utf-8"))
            except (KeyError, OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ParseError(f"derived manifest {name} cannot be read: {exc}", path=f"$.derived.{name}", stage="snapshot reader") from exc
            if _sha256(payload) != entry.get("sha256") or len(payload) != entry.get("byte_length"):
                raise ParseError("derived manifest bytes do not match manifest", path=f"$.derived.{name}", stage="snapshot reader")
            derived[name] = value
        return LoadedSnapshot(
            manifest=manifest,
            records=records,
            lifecycle=lifecycle,
            record_bytes=record_bytes,
            derived=derived,
        )

    def load_current(self) -> LoadedSnapshot | None:
        pointer = self.read_pointer("current")
        return None if pointer is None else self.read_generation(pointer)

    def load_baseline(self) -> LoadedSnapshot | None:
        pointer = self.read_pointer("baseline")
        return None if pointer is None else self.read_generation(pointer)

    # Compatibility aliases for callers using persistence vocabulary.
    save_generation = write_generation
    store_generation = write_generation
    set_current = publish_current
    set_baseline = publish_baseline
    read_current = load_current
    read_baseline = load_baseline

    def _publish_pointer(self, name: str, generation: str | SnapshotManifest) -> SnapshotPointer:
        if isinstance(generation, SnapshotManifest):
            generation_id = generation.generation_id
        else:
            generation_id = _safe_component(generation, "generation_id")
        manifest = self.read_generation(generation_id).manifest
        if not manifest.complete or not manifest.validated:
            raise CommitError("only complete validated generations may become pointers", stage="snapshot pointer")
        generation_manifest_path = self.guard.resolve_path(
            self.generation_path(generation_id) / _MANIFEST_NAME
        )
        relative_manifest = generation_manifest_path.relative_to(self.guard.root).as_posix()
        pointer = SnapshotPointer(
            pointer=name,
            generation_id=generation_id,
            manifest_path=relative_manifest,
            manifest_sha256=_sha256(generation_manifest_path.read_bytes()),
        )
        payload = _json_bytes(pointer.to_dict())
        destination = self.pointer_path(name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.guard.resolve_path(
            destination.parent / f".{destination.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            _write_new_file(temporary, payload)
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise CommitError(
                f"could not atomically replace {name} snapshot pointer: {exc}",
                path=str(destination),
                stage="snapshot pointer",
            ) from exc
        return pointer

    def _default_derived(
        self,
        generation_id: str,
        registry_url: str,
        records: Mapping[int, NormalizedRecord],
        lifecycle: Mapping[int, LifecycleStatus],
    ) -> dict[str, Any]:
        return {
            "registry.json": {
                "generation_id": generation_id,
                "registry_url": registry_url,
                "records": [record_id for record_id in sorted(records)],
            },
            "categories.json": {
                "generation_id": generation_id,
                "records": {
                    str(record_id): records[record_id].categories.official.to_dict()
                    for record_id in sorted(records)
                },
            },
            "stats.json": {
                "generation_id": generation_id,
                "record_count": len(records),
                "lifecycle_counts": self._lifecycle_counts(lifecycle),
            },
            "sync_state.json": {
                "generation_id": generation_id,
                "registry_url": registry_url,
                "complete": True,
                "validated": True,
            },
        }

    @staticmethod
    def _lifecycle_counts(lifecycle: Mapping[int, LifecycleStatus]) -> dict[str, int]:
        counts = {state.value: 0 for state in LifecycleState}
        for status in lifecycle.values():
            counts[status.state.value] = counts.get(status.state.value, 0) + 1
        return counts

    @staticmethod
    def _remove_staging(path: Path) -> None:
        if not path.exists():
            return
        # A failed generation is private staging; it is safe to remove only
        # this path.  If cleanup itself fails, the final pointer is still safe.
        import shutil

        shutil.rmtree(path, ignore_errors=True)


# Design-document spellings and concise aliases.
Snapshot_Store = SnapshotStore
SnapshotGenerationStore = SnapshotStore
Snapshot_Pointer = SnapshotPointer
Snapshot_Manifest = SnapshotManifest
Loaded_Snapshot = LoadedSnapshot

__all__ = [
    "LoadedSnapshot",
    "Loaded_Snapshot",
    "SnapshotGenerationStore",
    "SnapshotManifest",
    "SnapshotPointer",
    "SnapshotStore",
    "Snapshot_Manifest",
    "Snapshot_Pointer",
    "Snapshot_Store",
]
