"""Append-only lifecycle event history storage.

The history store is the persistence boundary for ``history/changes.jsonl``.
It validates every event before writing, verifies the existing journal, and
performs no write at all when validation or global event-ID uniqueness fails.
All paths are resolved through :class:`WorkspaceGuard`; this module never
creates files outside the configured local workspace.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..models import LifecycleEvent
from .workspace import WorkspaceGuard

_HISTORY_FILENAME = "changes.jsonl"
_REQUIRED_EVENT_FIELDS: tuple[str, ...] = (
    "event_id",
    "run_id",
    "enbek_id",
    "code",
    "event_type",
    "detected_at",
    "old_hashes",
    "new_hashes",
    "changes",
    "source",
)


class HistoryStoreError(ValueError):
    """A history journal or lifecycle event violates its storage contract."""


class HistoryStore:
    """Persist lifecycle events as an append-only JSONL journal.

    The store deliberately does not acquire the workspace run lock: the
    orchestrator owns that lock for a sync commit.  A process-local lock still
    makes the read/validate/append sequence atomic for callers sharing one
    store instance, and all writes use ``O_APPEND`` so an existing prefix is
    never rewritten.
    """

    def __init__(
        self,
        workspace: WorkspaceGuard | str | Path,
        *,
        filename: str = _HISTORY_FILENAME,
    ) -> None:
        self.guard = workspace if isinstance(workspace, WorkspaceGuard) else WorkspaceGuard(workspace)
        self.guard.preflight()
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("history filename must be a non-empty string")
        candidate = Path(filename)
        if candidate.is_absolute() or candidate.name != filename or filename in {".", ".."}:
            raise ValueError("history filename must be a single path-safe filename")
        if any(char in filename for char in ("/", "\\", "\x00")):
            raise ValueError("history filename must be a single path-safe filename")
        self.filename = filename
        self._history_lock = threading.RLock()

    @property
    def history_path(self) -> Path:
        """Return the guard-validated ``history/changes.jsonl`` path."""

        history_root = self.guard.storage_dirs.get("history_dir", Path("history"))
        return self.guard.resolve_path(Path(history_root) / self.filename)

    @property
    def changes_path(self) -> Path:
        """Alias for :attr:`history_path` matching the artifact name."""

        return self.history_path

    @property
    def path(self) -> Path:
        """Short alias used by persistence callers."""

        return self.history_path

    def append(
        self,
        events: LifecycleEvent | Mapping[str, Any] | Iterable[LifecycleEvent | Mapping[str, Any]] | None,
    ) -> tuple[LifecycleEvent, ...]:
        """Append validated lifecycle events and return the appended events.

        ``None`` and an empty iterable are no-ops: they do not even create the
        history file.  This is the path used for ``UNCHANGED``, failed and
        dry-run classifications, which must add zero lifecycle event lines.
        """

        batch = self._materialize_events(events)
        if not batch:
            return ()

        with self._history_lock:
            existing_ids = self._read_existing_event_ids()
            seen: set[str] = set()
            for event in batch:
                if event.event_id in existing_ids or event.event_id in seen:
                    raise HistoryStoreError(
                        f"event_id is already present in history: {event.event_id}"
                    )
                seen.add(event.event_id)

            lines = b"".join(self._event_line(event) for event in batch)
            self._append_bytes(lines)
        return batch

    def append_event(
        self, event: LifecycleEvent | Mapping[str, Any]
    ) -> LifecycleEvent:
        """Append exactly one event, returning the validated event."""

        return self.append(event)[0]

    # Explicit aliases keep the storage boundary convenient without introducing
    # a second implementation or a second serialization format.
    add = append_event
    append_events = append
    save = append
    write = append

    def read_events(self) -> tuple[LifecycleEvent, ...]:
        """Validate and return all existing journal events in file order."""

        with self._history_lock:
            return tuple(self._read_existing_events())

    @property
    def event_ids(self) -> frozenset[str]:
        """Return globally used event IDs after validating the journal."""

        with self._history_lock:
            return frozenset(self._read_existing_event_ids())

    @classmethod
    def validate_event(
        cls, event: LifecycleEvent | Mapping[str, Any]
    ) -> LifecycleEvent:
        """Validate and coerce one complete ``LifecycleEvent`` schema value."""

        if isinstance(event, LifecycleEvent):
            value = event
            document = value.to_dict()
        elif isinstance(event, Mapping):
            document = dict(event)
            missing = [name for name in _REQUIRED_EVENT_FIELDS if name not in document]
            if missing:
                raise HistoryStoreError(
                    "lifecycle event is missing required fields: "
                    + ", ".join(missing)
                )
            try:
                value = LifecycleEvent.from_dict(document)
            except (TypeError, ValueError, KeyError) as exc:
                raise HistoryStoreError(
                    f"invalid lifecycle event schema: {exc}"
                ) from exc
            document = value.to_dict()
        else:
            raise TypeError("event must be a LifecycleEvent or object")

        missing = [name for name in _REQUIRED_EVENT_FIELDS if name not in document]
        if missing:
            raise HistoryStoreError(
                "lifecycle event is missing required fields: " + ", ".join(missing)
            )
        # ``LifecycleEvent`` intentionally has no UNCHANGED variant; an
        # unchanged classification is a run-report count, not a history event.
        if not value.event_id.strip():
            raise HistoryStoreError("lifecycle event_id must be non-empty")
        return value

    def _materialize_events(
        self,
        events: LifecycleEvent | Mapping[str, Any] | Iterable[LifecycleEvent | Mapping[str, Any]] | None,
    ) -> tuple[LifecycleEvent, ...]:
        if events is None:
            return ()
        if isinstance(events, (LifecycleEvent, Mapping)):
            source = (events,)
        else:
            if isinstance(events, (str, bytes, bytearray)):
                raise TypeError("events must contain lifecycle event objects")
            try:
                source = tuple(events)
            except TypeError as exc:
                raise TypeError("events must be an event or iterable of events") from exc
        return tuple(self.validate_event(event) for event in source)

    def _read_existing_events(self) -> list[LifecycleEvent]:
        path = self.history_path
        if not path.exists():
            return []
        if not path.is_file():
            raise HistoryStoreError(f"history path is not a file: {path}")
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise HistoryStoreError(f"could not read history journal: {exc}") from exc

        if not raw:
            return []
        lines = text.splitlines(keepends=True)
        result: list[LifecycleEvent] = []
        for number, line in enumerate(lines, start=1):
            payload = line.rstrip("\r\n")
            if not payload.strip():
                raise HistoryStoreError(f"history line {number} is blank")
            try:
                document = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise HistoryStoreError(
                    f"history line {number} is not valid JSON: {exc.msg}"
                ) from exc
            if not isinstance(document, Mapping):
                raise HistoryStoreError(f"history line {number} must be a JSON object")
            try:
                result.append(self.validate_event(document))
            except (HistoryStoreError, TypeError, ValueError) as exc:
                raise HistoryStoreError(
                    f"invalid lifecycle event on history line {number}: {exc}"
                ) from exc
        return result

    def _read_existing_event_ids(self) -> set[str]:
        events = self._read_existing_events()
        result: set[str] = set()
        for event in events:
            if event.event_id in result:
                raise HistoryStoreError(
                    f"history contains duplicate event_id: {event.event_id}"
                )
            result.add(event.event_id)
        return result

    @staticmethod
    def _event_line(event: LifecycleEvent) -> bytes:
        try:
            payload = json.dumps(
                event.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise HistoryStoreError(f"could not serialize lifecycle event: {exc}") from exc
        return (payload + "\n").encode("utf-8")

    def _append_bytes(self, content: bytes) -> None:
        path = self.history_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        except OSError as exc:
            raise HistoryStoreError(f"could not open history journal: {exc}") from exc
        try:
            if path.stat().st_size:
                with path.open("rb") as existing:
                    existing.seek(-1, os.SEEK_END)
                    if existing.read(1) != b"\n":
                        self._write_all(fd, b"\n")
            self._write_all(fd, content)
            os.fsync(fd)
        except OSError as exc:
            raise HistoryStoreError(f"could not append history journal: {exc}") from exc
        finally:
            os.close(fd)

    @staticmethod
    def _write_all(fd: int, content: bytes) -> None:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("history journal write made no progress")
            view = view[written:]


# Design-document vocabulary alias.
History_Store = HistoryStore

__all__ = ["HistoryStore", "History_Store", "HistoryStoreError"]
