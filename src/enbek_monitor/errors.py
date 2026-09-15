"""Structured, serializable errors used by the local monitor.

The source, parser, normalization, validation, storage and presentation layers
use these exceptions as data-bearing failure results.  They deliberately do
not contain a partial domain result: callers either receive a complete domain
object or raise one of these errors.

Every error has the fields needed by a failed-run report.  Optional diagnostic
fields are represented as JSON ``null`` or empty arrays rather than being
silently dropped, which keeps error serialization predictable for both CLI and
persistence layers.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Protocol, TypeAlias, TypedDict, runtime_checkable

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_DOCUMENT_PATH = "$"


class ErrorPayload(TypedDict):
    """Stable JSON shape consumed by ``Run_Report.errors``."""

    error_type: str
    reason: str
    stage: str
    url: str | None
    path: str
    attempts: int | None
    status: int | None
    missing_fields: list[str]
    invalid_fields: list[str]


@runtime_checkable
class SerializableError(Protocol):
    """Protocol implemented by errors that can be stored in a run report."""

    reason: str
    stage: str

    def to_dict(self) -> dict[str, JSONValue]:
        """Return a JSON-compatible report payload."""
        ...

    def to_report_dict(self) -> dict[str, JSONValue]:
        """Return the report-compatible representation of the error."""
        ...


# A descriptive alias for callers that want to type a structured monitor error
# without depending on the concrete class hierarchy.
ErrorContract = SerializableError


def _require_text(value: Any, field_name: str) -> str:
    """Validate a required non-blank text field and return its trimmed value."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> str | None:
    """Validate an optional text field while preserving its meaningful value."""

    if value is None:
        return None
    return _require_text(value, field_name)


def _normalise_path(value: Any) -> str:
    """Return a non-empty diagnostic path; ``None`` denotes the document root."""

    if value is None:
        return _DOCUMENT_PATH
    if not isinstance(value, str):
        raise ValueError("path must be a string or None")
    value = value.strip()
    return value or _DOCUMENT_PATH


def _normalise_fields(value: Sequence[str] | None, field_name: str) -> tuple[str, ...]:
    """Validate and de-duplicate field paths without changing their order."""

    if value is None:
        return ()
    if isinstance(value, str):
        values: Sequence[str] = (value,)
    else:
        values = value

    result: list[str] = []
    seen: set[str] = set()
    for field in values:
        normalised = _require_text(field, f"{field_name} entries")
        if normalised not in seen:
            result.append(normalised)
            seen.add(normalised)
    return tuple(result)


def _normalise_attempts(value: int | None) -> int | None:
    """Validate the network-attempt range required by Source_Error."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3:
        raise ValueError("attempts must be an integer between 1 and 3")
    return value


def _normalise_status(value: int | None) -> int | None:
    """Validate an optional HTTP status code."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        raise ValueError("status must be an HTTP status integer between 100 and 599")
    return value


def _resolve_alias(
    primary: Any,
    alias: Any,
    *,
    primary_name: str,
    alias_name: str,
) -> Any:
    """Resolve two equivalent keyword names and reject conflicting values."""

    if primary is not None and alias is not None and primary != alias:
        raise ValueError(f"{primary_name} and {alias_name} contain different values")
    return primary if primary is not None else alias


def _json_safe(value: Any, *, path: str = "$") -> JSONValue:
    """Copy a JSON-compatible value and reject non-serializable diagnostics."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{path} contains a non-string or empty object key")
            result[key] = _json_safe(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path} contains a value that is not JSON serializable")


class MonitorError(Exception):
    """Base structured error with a common failed-run report contract.

    ``path`` is a JSON/HTML/source diagnostic path.  ``$`` is always used for
    document-level failures.  ``stage`` receives a concrete default in every
    specialized error, so even an error without a source URL is valid for a
    failed-run report (which requires a reason and a URL or processing stage).
    """

    error_type: ClassVar[str] = "MonitorError"
    default_stage: ClassVar[str] = "monitor"

    def __init__(
        self,
        reason: str,
        *,
        url: str | None = None,
        attempts: int | None = None,
        status: int | None = None,
        stage: str | None = None,
        path: str | None = _DOCUMENT_PATH,
        missing_fields: Sequence[str] | None = None,
        invalid_fields: Sequence[str] | None = None,
    ) -> None:
        self.reason = _require_text(reason, "reason")
        self.url = _optional_text(url, "url")
        self.attempts = _normalise_attempts(attempts)
        self.status = _normalise_status(status)
        self.stage = _optional_text(stage, "stage") or self.default_stage
        self.path = _normalise_path(path)
        self.missing_fields = _normalise_fields(missing_fields, "missing_fields")
        self.invalid_fields = _normalise_fields(invalid_fields, "invalid_fields")

        # Keep the normal exception representation useful for callers that do
        # not yet consume the structured contract.
        super().__init__(self.reason)

    @property
    def source_url(self) -> str | None:
        """Alias used by source/parser callers for the diagnostic URL."""

        return self.url

    @property
    def processing_stage(self) -> str:
        """Alias matching the terminology used by Run_Report."""

        return self.stage

    @property
    def diagnostic_path(self) -> str:
        """Alias for the JSON/HTML/document diagnostic path."""

        return self.path

    @property
    def missing(self) -> tuple[str, ...]:
        """Short alias for the missing-field paths."""

        return self.missing_fields

    @property
    def invalid(self) -> tuple[str, ...]:
        """Short alias for the invalid-field paths."""

        return self.invalid_fields

    def _extra_payload(self) -> dict[str, JSONValue]:
        """Return subclass-specific JSON fields."""

        return {}

    def to_dict(self) -> dict[str, JSONValue]:
        """Return a fresh JSON-compatible error record.

        The result intentionally contains no parser/normalizer output.  A
        failed operation therefore cannot accidentally be persisted together
        with a partial domain object.
        """

        payload: dict[str, JSONValue] = {
            "error_type": self.error_type,
            "reason": self.reason,
            "stage": self.stage,
            "url": self.url,
            "path": self.path,
            "attempts": self.attempts,
            "status": self.status,
            "missing_fields": list(self.missing_fields),
            "invalid_fields": list(self.invalid_fields),
        }
        payload.update(self._extra_payload())
        # Validate the final payload as a defensive guarantee for future
        # subclass fields as well.
        return _json_safe(payload)  # type: ignore[return-value]

    def to_report_dict(self) -> dict[str, JSONValue]:
        """Return the representation expected in ``Run_Report.errors``."""

        return self.to_dict()

    def as_dict(self) -> dict[str, JSONValue]:
        """Backward-friendly alias for :meth:`to_dict`."""

        return self.to_dict()

    def serialize(self) -> dict[str, JSONValue]:
        """Alias emphasizing that the result is persistence-ready."""

        return self.to_dict()

    def to_json(self) -> str:
        """Serialize the error deterministically as UTF-8-compatible JSON."""

        return (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )

    def to_json_bytes(self) -> bytes:
        """Return the deterministic JSON representation encoded as UTF-8."""

        return self.to_json().encode("utf-8")


# The name is useful in code that wants to emphasize that this is a structured
# error rather than a generic exception.  It remains the same runtime class.
StructuredError = MonitorError


class SourceError(MonitorError):
    """Failure to obtain a source response or a valid HTTP source response."""

    error_type = "SourceError"
    default_stage = "network"

    def __init__(
        self,
        reason: str,
        *,
        url: str,
        attempts: int = 1,
        status: int | None = None,
        stage: str | None = None,
        path: str | None = _DOCUMENT_PATH,
        missing_fields: Sequence[str] | None = None,
        invalid_fields: Sequence[str] | None = None,
    ) -> None:
        # A source failure always identifies the URL that could not be
        # obtained; callers should not be able to create an untraceable one.
        super().__init__(
            reason,
            url=_require_text(url, "url"),
            attempts=attempts,
            status=status,
            stage=stage,
            path=path,
            missing_fields=missing_fields,
            invalid_fields=invalid_fields,
        )


class ParseError(MonitorError):
    """Source or canonical JSON does not satisfy its expected schema."""

    error_type = "ParseError"
    default_stage = "parser"

    def __init__(
        self,
        reason: str,
        *,
        url: str | None = None,
        source_url: str | None = None,
        path: str | None = _DOCUMENT_PATH,
        source_path: str | None = None,
        json_path: str | None = None,
        html_path: str | None = None,
        stage: str | None = None,
        attempts: int | None = None,
        status: int | None = None,
        missing_fields: Sequence[str] | None = None,
        invalid_fields: Sequence[str] | None = None,
    ) -> None:
        resolved_url = _resolve_alias(
            url,
            source_url,
            primary_name="url",
            alias_name="source_url",
        )
        resolved_path = path
        for alias_name, alias_value in (
            ("source_path", source_path),
            ("json_path", json_path),
            ("html_path", html_path),
        ):
            if alias_value is None:
                continue
            if resolved_path not in (None, _DOCUMENT_PATH) and resolved_path != alias_value:
                raise ValueError(f"path and {alias_name} contain different values")
            if resolved_path in (None, _DOCUMENT_PATH):
                resolved_path = alias_value

        super().__init__(
            reason,
            url=resolved_url,
            attempts=attempts,
            status=status,
            stage=stage,
            path=resolved_path,
            missing_fields=missing_fields,
            invalid_fields=invalid_fields,
        )


class NormalizationError(MonitorError):
    """Source attributes cannot form one complete canonical record."""

    error_type = "NormalizationError"
    default_stage = "normalization"

    def __init__(
        self,
        reason: str,
        *,
        enbek_id: int | None = None,
        url: str | None = None,
        source_url: str | None = None,
        path: str | None = _DOCUMENT_PATH,
        stage: str | None = None,
        attempts: int | None = None,
        status: int | None = None,
        missing_fields: Sequence[str] | None = None,
        invalid_fields: Sequence[str] | None = None,
    ) -> None:
        resolved_url = _resolve_alias(
            url,
            source_url,
            primary_name="url",
            alias_name="source_url",
        )
        if enbek_id is not None and (
            isinstance(enbek_id, bool) or not isinstance(enbek_id, int) or enbek_id <= 0
        ):
            raise ValueError("enbek_id must be a positive integer or None")
        self.enbek_id = enbek_id
        super().__init__(
            reason,
            url=resolved_url,
            attempts=attempts,
            status=status,
            stage=stage,
            path=path,
            missing_fields=missing_fields,
            invalid_fields=invalid_fields,
        )

    def _extra_payload(self) -> dict[str, JSONValue]:
        payload = super()._extra_payload()
        payload["enbek_id"] = self.enbek_id
        return payload


class ValidationError(MonitorError):
    """Candidate/current snapshot violates a validation rule."""

    error_type = "ValidationError"
    default_stage = "validation"

    def __init__(
        self,
        reason: str,
        *,
        rule: str | None = None,
        path: str | None = _DOCUMENT_PATH,
        snapshot_path: str | None = None,
        url: str | None = None,
        stage: str | None = None,
        missing_fields: Sequence[str] | None = None,
        invalid_fields: Sequence[str] | None = None,
    ) -> None:
        if snapshot_path is not None:
            if path not in (None, _DOCUMENT_PATH) and path != snapshot_path:
                raise ValueError("path and snapshot_path contain different values")
            resolved_path = snapshot_path if path in (None, _DOCUMENT_PATH) else path
        else:
            resolved_path = path
        self.rule = _optional_text(rule, "rule")
        super().__init__(
            reason,
            url=url,
            stage=stage,
            path=resolved_path,
            missing_fields=missing_fields,
            invalid_fields=invalid_fields,
        )

    def _extra_payload(self) -> dict[str, JSONValue]:
        payload = super()._extra_payload()
        payload["rule"] = self.rule
        return payload


class WorkspaceError(MonitorError):
    """Workspace preflight, path-boundary or permission failure."""

    error_type = "WorkspaceError"
    default_stage = "workspace"

    def __init__(
        self,
        reason: str,
        *,
        workspace_root: str | None = None,
        root: str | None = None,
        path: str | None = _DOCUMENT_PATH,
        url: str | None = None,
        stage: str | None = None,
        operation: str | None = None,
        permission: str | None = None,
    ) -> None:
        resolved_root = _resolve_alias(
            workspace_root,
            root,
            primary_name="workspace_root",
            alias_name="root",
        )
        self.workspace_root = _optional_text(resolved_root, "workspace_root")
        self.operation = _optional_text(operation, "operation")
        self.permission = _optional_text(permission, "permission")
        super().__init__(reason, url=url, stage=stage, path=path)

    def _extra_payload(self) -> dict[str, JSONValue]:
        payload = super()._extra_payload()
        payload.update(
            {
                "workspace_root": self.workspace_root,
                "operation": self.operation,
                "permission": self.permission,
            }
        )
        return payload


class CommitError(MonitorError):
    """Atomic persistence or commit-recovery failure."""

    error_type = "CommitError"
    default_stage = "commit"

    def __init__(
        self,
        reason: str,
        *,
        stage: str | None = None,
        path: str | None = _DOCUMENT_PATH,
        journal_path: str | None = None,
        recovery_status: str | None = None,
        url: str | None = None,
    ) -> None:
        self.journal_path = _optional_text(journal_path, "journal_path")
        self.recovery_status = _optional_text(recovery_status, "recovery_status")
        super().__init__(reason, url=url, stage=stage, path=path)

    def _extra_payload(self) -> dict[str, JSONValue]:
        payload = super()._extra_payload()
        payload.update(
            {
                "journal_path": self.journal_path,
                "recovery_status": self.recovery_status,
            }
        )
        return payload


class GenerationError(MonitorError):
    """Markdown or other derived-artifact generation failure."""

    error_type = "GenerationError"
    default_stage = "generation"

    def __init__(
        self,
        reason: str,
        *,
        artifact_path: str | None = None,
        path: str | None = _DOCUMENT_PATH,
        url: str | None = None,
        stage: str | None = None,
        missing_fields: Sequence[str] | None = None,
        invalid_fields: Sequence[str] | None = None,
    ) -> None:
        self.artifact_path = _optional_text(artifact_path, "artifact_path")
        super().__init__(
            reason,
            url=url,
            stage=stage,
            path=path,
            missing_fields=missing_fields,
            invalid_fields=invalid_fields,
        )

    def _extra_payload(self) -> dict[str, JSONValue]:
        payload = super()._extra_payload()
        payload["artifact_path"] = self.artifact_path
        return payload


def serialize_error(error: BaseException) -> dict[str, JSONValue]:
    """Serialize a structured error, with a safe fallback for foreign errors.

    Pipeline boundaries normally receive :class:`MonitorError`.  The fallback
    keeps an unexpected exception reportable without exposing a partial domain
    result or losing the fact that it failed at a processing stage.
    """

    if isinstance(error, MonitorError):
        return error.to_report_dict()

    reason = str(error).strip() or error.__class__.__name__
    return {
        "error_type": error.__class__.__name__,
        "reason": reason,
        "stage": "unknown",
        "url": None,
        "path": _DOCUMENT_PATH,
        "attempts": None,
        "status": None,
        "missing_fields": [],
        "invalid_fields": [],
    }


__all__ = [
    "CommitError",
    "ErrorContract",
    "ErrorPayload",
    "GenerationError",
    "JSONValue",
    "MonitorError",
    "NormalizationError",
    "ParseError",
    "SerializableError",
    "SourceError",
    "StructuredError",
    "ValidationError",
    "WorkspaceError",
    "serialize_error",
]
