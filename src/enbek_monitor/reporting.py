"""Run report construction and workspace-local persistence.

The reporter is intentionally independent from orchestration.  It turns the
inputs collected by a run into one validated :class:`RunReport`, and publishes
that report below ``history/runs`` through :class:`WorkspaceGuard`.  Report
publication uses a same-directory temporary file, ``fsync`` and atomic rename;
by default an existing report is never replaced, so one run id can have only
one durable report.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import MonitorError, WorkspaceError
from .models import (
    ErrorInfo,
    LifecycleEvent,
    RunMode,
    RunReport,
    RunStatus,
)
from .storage.workspace import WorkspaceGuard

_REPORT_DIRECTORY = Path("history") / "runs"
_RUN_ID_INVALID_CHARS = frozenset({"/", "\\"})


def _run_id(value: Any) -> str:
    """Validate the part of the report path supplied by a caller."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("run_id must be a non-empty path-safe string")
    result = value.strip()
    if result in {".", ".."} or any(char in result for char in _RUN_ID_INVALID_CHARS):
        raise ValueError("run_id must be a non-empty path-safe string")
    if any(ord(char) < 32 for char in result):
        raise ValueError("run_id must not contain control characters")
    return result


def _utc_timestamp(value: datetime | str, field_name: str) -> datetime:
    """Return a second-precision timezone-aware UTC timestamp.

    The domain ``RunReport`` contract is second precision.  Rejecting
    fractional input avoids silently changing the duration arithmetic while
    serialising the timestamp.
    """

    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} must be a non-empty UTC timestamp")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO 8601 timestamp") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"{field_name} must be a datetime or ISO 8601 string")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if parsed.microsecond:
        raise ValueError(f"{field_name} must not contain fractional seconds")
    return parsed.astimezone(timezone.utc)


def _diagnostic(error: Any) -> ErrorInfo:
    """Convert one registered error into the report's structured model."""

    if isinstance(error, ErrorInfo):
        return ErrorInfo(
            stage=error.stage,
            reason=error.reason,
            url=error.url,
            path=error.path,
            attempts=error.attempts,
            status=error.status,
            fields=list(error.fields),
        )

    if isinstance(error, MonitorError):
        fields = list(error.missing_fields) + list(error.invalid_fields)
        return ErrorInfo(
            stage=error.stage,
            reason=error.reason,
            url=error.url,
            path=error.path,
            attempts=error.attempts,
            status=error.status,
            fields=fields,
        )

    if isinstance(error, Mapping):
        reason = error.get("reason")
        stage = error.get("stage", error.get("processing_stage"))
        url = error.get("url", error.get("source_url"))
        path = error.get("path", error.get("diagnostic_path"))
        attempts = error.get("attempts")
        status = error.get("status")
        fields: list[str] = []
        for key in ("fields", "missing_fields", "invalid_fields"):
            value = error.get(key, ())
            if value is None:
                continue
            if isinstance(value, str):
                fields.append(value)
            else:
                try:
                    fields.extend(value)
                except TypeError as exc:
                    raise TypeError(f"error.{key} must be a string or iterable") from exc
        return ErrorInfo(
            stage=stage or "unknown",
            reason=reason or "registered error",
            url=url,
            path=path,
            attempts=attempts,
            status=status,
            fields=fields,
        )

    reason = str(error).strip() or error.__class__.__name__
    return ErrorInfo(stage="unknown", reason=reason)


def _events(events: Iterable[LifecycleEvent | Mapping[str, Any]] | None) -> list[LifecycleEvent]:
    if events is None:
        return []
    result: list[LifecycleEvent] = []
    for event in events:
        if isinstance(event, LifecycleEvent):
            result.append(event)
        elif isinstance(event, Mapping):
            result.append(LifecycleEvent.from_dict(event))
        else:
            raise TypeError("planned_events must contain LifecycleEvent objects")
    return result


class RunReporter:
    """Build and atomically publish one report for a local run.

    ``workspace`` may be an existing ``WorkspaceGuard`` or a workspace path.
    The caller may hold the guard's run lock while publishing; the reporter
    does not acquire a second lock.  All path validation remains delegated to
    ``WorkspaceGuard``.
    """

    def __init__(self, workspace: WorkspaceGuard | str | Path) -> None:
        self.guard = workspace if isinstance(workspace, WorkspaceGuard) else WorkspaceGuard(workspace)

    def build_report(
        self,
        *,
        run_id: str,
        started_at: datetime | str,
        finished_at: datetime | str,
        registry_url: str,
        mode: RunMode | str = RunMode.SYNC,
        status: RunStatus | str = RunStatus.SUCCESS,
        counts: Mapping[str, Any] | None = None,
        total: int | None = None,
        baseline: int | None = None,
        new: int | None = None,
        updated: int | None = None,
        missing: int | None = None,
        removed: int | None = None,
        restored: int | None = None,
        unchanged: int | None = None,
        errors: Iterable[Any] | None = None,
        planned_events: Iterable[LifecycleEvent | Mapping[str, Any]] | None = None,
    ) -> RunReport:
        """Create a report without touching the filesystem."""

        safe_id = _run_id(run_id)
        started = _utc_timestamp(started_at, "started_at")
        finished = _utc_timestamp(finished_at, "finished_at")
        if finished < started:
            raise ValueError("finished_at must be greater than or equal to started_at")
        duration = (finished - started).total_seconds()
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("duration must be a finite non-negative number")

        try:
            chosen_mode = RunMode(mode)
            chosen_status = RunStatus(status)
        except (TypeError, ValueError) as exc:
            raise ValueError("mode/status is not supported") from exc

        provided_counts = dict(counts or {})
        count_values = {
            "total": total,
            "baseline": baseline,
            "new": new,
            "updated": updated,
            "missing": missing,
            "removed": removed,
            "restored": restored,
            "unchanged": unchanged,
        }
        for name, explicit in count_values.items():
            if explicit is None:
                count_values[name] = provided_counts.get(name, 0)
            elif name in provided_counts and provided_counts[name] != explicit:
                raise ValueError(f"conflicting values for count {name}")
        unknown_counts = set(provided_counts) - set(count_values)
        if unknown_counts:
            raise ValueError("unknown report count(s): " + ", ".join(sorted(unknown_counts)))

        diagnostics = [_diagnostic(error) for error in (errors or ())]
        if chosen_status is RunStatus.FAILED and not diagnostics:
            raise ValueError("failed report requires at least one registered error")

        planned = _events(planned_events) if chosen_mode is RunMode.DRY_RUN else []
        return RunReport(
            run_id=safe_id,
            started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            finished_at=finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            duration=duration,
            registry_url=registry_url,
            mode=chosen_mode,
            status=chosen_status,
            errors=diagnostics,
            planned_events=planned,
            **{name: value for name, value in count_values.items()},
        )

    # Public aliases make the pure construction operation convenient for
    # callers using either the design vocabulary or normal Python naming.
    create_report = build_report

    def report_path(self, run_id: str) -> Path:
        """Resolve the canonical report path without creating directories."""

        safe_id = _run_id(run_id)
        return self.guard.resolve_path(_REPORT_DIRECTORY / f"{safe_id}.json")

    def write_report(self, report: RunReport, *, overwrite: bool = False) -> Path:
        """Atomically publish one report under ``history/runs``.

        The temporary file is created in the destination directory, making
        ``os.replace`` atomic on the same filesystem.  ``overwrite=False`` is
        the default and protects the exactly-one-report-per-run invariant.
        """

        if not isinstance(report, RunReport):
            raise TypeError("report must be a RunReport")
        self.guard.preflight()
        destination = self.report_path(report.run_id)
        parent = destination.parent
        parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not overwrite:
            raise WorkspaceError(
                f"run report already exists: {report.run_id}",
                workspace_root=str(self.guard.root),
                path=str(destination),
                operation="write run report",
            )

        temporary_name: str | None = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=parent
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(report.to_json_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            if destination.exists() and not overwrite:
                raise WorkspaceError(
                    f"run report already exists: {report.run_id}",
                    workspace_root=str(self.guard.root),
                    path=str(destination),
                    operation="write run report",
                )
            os.replace(temporary_name, destination)
            temporary_name = None
            try:
                directory_fd = os.open(parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return destination
        except WorkspaceError:
            raise
        except OSError as exc:
            raise WorkspaceError(
                f"could not write run report: {exc}",
                workspace_root=str(self.guard.root),
                path=str(destination),
                operation="write run report",
                permission=str(exc),
            ) from exc
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass

    # Common storage-facing aliases.
    write = write_report

    def report(self, **kwargs: Any) -> Path:
        """Build and publish one report in one call."""

        return self.write_report(self.build_report(**kwargs))

    report_run = report


Run_Reporter = RunReporter

__all__ = ["RunReporter", "Run_Reporter"]
