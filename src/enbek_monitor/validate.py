"""Validation boundary for candidate snapshots.

The validator is deliberately separate from snapshot publication and lifecycle
tracking.  It returns a structured result; callers must inspect
``eligible_for_lifecycle``/``can_commit`` before publishing a candidate.  No
filesystem or network access is performed unless a caller explicitly supplies
PDF bytes, a PDF resolver, or a local PDF path/root.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from .errors import ValidationError
from .models import (
    ArtifactKind,
    CandidateSnapshot,
    CurrentSnapshot,
    NormalizedRecord,
    RawArtifact,
    RawArtifactRef,
    SnapshotState,
)

DEFAULT_PDF_MIN_BYTES = 1024
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MISSING = object()

PdfValue: TypeAlias = bytes | bytearray | memoryview | str | Path | Mapping[str, Any] | Any
PdfResolver: TypeAlias = Callable[..., PdfValue | None]


@dataclass(frozen=True)
class ValidationResult(Sequence[ValidationError]):
    """The complete validation outcome for one candidate snapshot.

    The result is sequence-compatible for callers that only need the list of
    errors, while the named guards make the publication policy explicit.  A
    failed result is never eligible for lifecycle processing or current-snapshot
    replacement.
    """

    errors: tuple[ValidationError, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "errors", tuple(self.errors))
        if any(not isinstance(error, ValidationError) for error in self.errors):
            raise TypeError("validation result errors must be ValidationError values")

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def accepted(self) -> bool:
        return self.valid

    @property
    def success(self) -> bool:
        return self.valid

    @property
    def failed(self) -> bool:
        return not self.valid

    @property
    def eligible_for_lifecycle(self) -> bool:
        """Whether lifecycle tracking may consume this candidate."""

        return self.valid

    @property
    def can_commit(self) -> bool:
        """Whether atomic Current_Snapshot replacement may proceed."""

        return self.valid

    @property
    def blocked(self) -> bool:
        return not self.valid

    @property
    def validation_errors(self) -> tuple[ValidationError, ...]:
        return self.errors

    def __bool__(self) -> bool:
        return self.valid

    def __len__(self) -> int:
        return len(self.errors)

    def __getitem__(self, index: int | slice) -> ValidationError | tuple[ValidationError, ...]:
        return self.errors[index]

    def __iter__(self):
        return iter(self.errors)

    def ensure_valid(self) -> None:
        """Raise the first structured error when publication is required."""

        if self.errors:
            raise self.errors[0]

    raise_if_invalid = ensure_valid


# Vocabulary aliases used by callers following the design document.
ValidationReport = ValidationResult
ValidatorResult = ValidationResult


class Validator:
    """Check a CandidateSnapshot before lifecycle processing and publication.

    A PDF is considered published when ``record.source.pdf_url`` is a
    non-blank value.  For such records the validator requires PDF bytes.  They
    can be supplied as ``pdf_documents`` (mapping Enbek_ID or URL to bytes), by
    ``pdf_resolver``, from ``raw_artifact_refs`` when ``workspace`` is set, or
    from ``pdf_root``.  This explicit input keeps validation deterministic and
    prevents a hash field from being treated as proof that a document exists.
    """

    def __init__(
        self,
        *,
        pdf_resolver: PdfResolver | None = None,
        pdf_documents: Mapping[Any, PdfValue] | None = None,
        workspace: str | Path | None = None,
        pdf_root: str | Path | None = None,
        pdf_min_bytes: int = DEFAULT_PDF_MIN_BYTES,
    ) -> None:
        if pdf_resolver is not None and not callable(pdf_resolver):
            raise TypeError("pdf_resolver must be callable")
        if pdf_documents is not None and not isinstance(pdf_documents, Mapping):
            raise TypeError("pdf_documents must be a mapping")
        if isinstance(pdf_min_bytes, bool) or not isinstance(pdf_min_bytes, int):
            raise TypeError("pdf_min_bytes must be an integer")
        if pdf_min_bytes < 1:
            raise ValueError("pdf_min_bytes must be positive")
        self.pdf_resolver = pdf_resolver
        self.pdf_documents = pdf_documents
        self.workspace = Path(workspace) if workspace is not None else None
        self.pdf_root = Path(pdf_root) if pdf_root is not None else None
        self.pdf_min_bytes = pdf_min_bytes

    def validate(
        self,
        candidate: CandidateSnapshot | Mapping[str, Any] | Any,
        current: CurrentSnapshot | SnapshotState | Mapping[str, Any] | Any | None = None,
        *,
        pdf_resolver: PdfResolver | None = None,
        pdf_documents: Mapping[Any, PdfValue] | None = None,
        workspace: str | Path | None = None,
        pdf_root: str | Path | None = None,
    ) -> ValidationResult:
        """Return all observable validation violations for ``candidate``.

        ``current`` is used only for the anomaly comparison.  Its records are
        never validated or modified.  Count comparison uses integer arithmetic
        (``candidate_count * 100 < current_count * 70``), so exactly 70 percent
        is accepted without floating-point boundary errors.
        """

        errors: list[ValidationError] = []
        complete = self._value(candidate, "complete", False)
        if complete is not True:
            errors.append(
                self._error(
                    "candidate snapshot is incomplete",
                    rule="complete",
                    path="$.complete",
                )
            )
        candidate_errors = self._value(candidate, "errors", ()) or ()
        if isinstance(candidate_errors, Iterable) and not isinstance(
            candidate_errors, (str, bytes, Mapping)
        ) and any(candidate_errors):
            errors.append(
                self._error(
                    "candidate snapshot contains collection errors and is not publishable",
                    rule="candidate_errors",
                    path="$.errors",
                )
            )

        records_value = self._value(candidate, "records", _MISSING)
        records = self._record_items(records_value)
        if not records:
            errors.append(
                self._error(
                    "candidate snapshot must contain at least one record",
                    rule="non_empty_snapshot",
                    path="$.records",
                )
            )

        seen_ids: dict[int, str] = {}
        for index, (mapping_key, raw_record) in enumerate(records):
            path = self._record_path(index, mapping_key)
            payload = self._record_payload(raw_record)
            record_id = payload.get("enbek_id", _MISSING)

            schema_version = payload.get("schema_version", _MISSING)
            if schema_version is _MISSING or not isinstance(schema_version, str) or not schema_version.strip():
                errors.append(
                    self._error(
                        "record schema_version must contain a non-whitespace character",
                        rule="schema_version",
                        path=f"{path}.schema_version",
                        invalid_fields=(f"{path}.schema_version",),
                    )
                )

            if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0:
                errors.append(
                    self._error(
                        "record enbek_id must be a positive integer",
                        rule="positive_enbek_id",
                        path=f"{path}.enbek_id",
                        invalid_fields=(f"{path}.enbek_id",),
                    )
                )
            else:
                if record_id in seen_ids:
                    errors.append(
                        self._error(
                            f"duplicate Enbek_ID {record_id} in candidate snapshot",
                            rule="unique_enbek_id",
                            path=f"{path}.enbek_id",
                            invalid_fields=(seen_ids[record_id], f"{path}.enbek_id"),
                        )
                    )
                else:
                    seen_ids[record_id] = f"{path}.enbek_id"
                if mapping_key is not None and self._mapping_key_id(mapping_key) not in (None, record_id):
                    errors.append(
                        self._error(
                            "record mapping key does not match record enbek_id",
                            rule="record_key_matches_id",
                            path=f"{path}.enbek_id",
                        )
                    )

            title = payload.get("title", _MISSING)
            if not isinstance(title, str) or not title.strip():
                errors.append(
                    self._error(
                        "record title must contain a non-whitespace character",
                        rule="nonblank_title",
                        path=f"{path}.title",
                        invalid_fields=(f"{path}.title",),
                    )
                )

            source = payload.get("source", _MISSING)
            if not isinstance(source, Mapping):
                errors.append(
                    self._error(
                        "record source must be an object",
                        rule="source_schema",
                        path=f"{path}.source",
                        invalid_fields=(f"{path}.source",),
                    )
                )
                source = {}
            page_url = source.get("page_url", _MISSING)
            if not isinstance(page_url, str) or not page_url.strip():
                errors.append(
                    self._error(
                        "record source.page_url must contain a non-whitespace character",
                        rule="nonblank_page_url",
                        path=f"{path}.source.page_url",
                        invalid_fields=(f"{path}.source.page_url",),
                    )
                )

            pdf_url = source.get("pdf_url")
            if isinstance(pdf_url, str) and pdf_url.strip():
                errors.extend(
                    self._validate_pdf(
                        record_id if isinstance(record_id, int) and not isinstance(record_id, bool) else None,
                        pdf_url,
                        payload,
                        candidate,
                        pdf_resolver=pdf_resolver if pdf_resolver is not None else self.pdf_resolver,
                        pdf_documents=pdf_documents if pdf_documents is not None else self.pdf_documents,
                        workspace=Path(workspace) if workspace is not None else self.workspace,
                        pdf_root=Path(pdf_root) if pdf_root is not None else self.pdf_root,
                        record_path=path,
                    )
                )
            elif pdf_url is not None and not isinstance(pdf_url, str):
                errors.append(
                    self._error(
                        "record source.pdf_url must be a string or null",
                        rule="pdf_url_schema",
                        path=f"{path}.source.pdf_url",
                    )
                )

        candidate_count = len(records)
        current_count = len(self._record_items(self._value(current, "records", _MISSING))) if current is not None else 0
        if current_count >= 1 and candidate_count * 100 < current_count * 70:
            errors.append(
                self._error(
                    f"candidate record count {candidate_count} is below 70% of current count {current_count}",
                    rule="record_count_anomaly",
                    path="$.records",
                )
            )

        return ValidationResult(tuple(errors))

    validate_candidate = validate
    check = validate

    def validate_or_raise(self, candidate: Any, current: Any = None, **kwargs: Any) -> ValidationResult:
        result = self.validate(candidate, current, **kwargs)
        result.ensure_valid()
        return result

    def _validate_pdf(
        self,
        record_id: int | None,
        pdf_url: str,
        payload: Mapping[str, Any],
        candidate: Any,
        *,
        pdf_resolver: PdfResolver | None,
        pdf_documents: Mapping[Any, PdfValue] | None,
        workspace: Path | None,
        pdf_root: Path | None,
        record_path: str,
    ) -> list[ValidationError]:
        errors: list[ValidationError] = []
        pdf_path = f"{record_path}.pdf"
        hashes = payload.get("hashes")
        hashes = hashes.to_dict() if hasattr(hashes, "to_dict") else hashes
        if not isinstance(hashes, Mapping):
            hashes = {}
        expected_hash = hashes.get("pdf_sha256")
        if not isinstance(expected_hash, str) or not _SHA256_RE.fullmatch(expected_hash):
            errors.append(
                self._error(
                    "published PDF requires a valid 64-character pdf_sha256",
                    rule="published_pdf_hash",
                    path=f"{record_path}.hashes.pdf_sha256",
                )
            )

        try:
            value = self._resolve_pdf(
                record_id,
                pdf_url,
                candidate,
                pdf_resolver=pdf_resolver,
                pdf_documents=pdf_documents,
                workspace=workspace,
                pdf_root=pdf_root,
            )
        except Exception as exc:
            errors.append(
                self._error(
                    f"published PDF cannot be resolved: {exc}",
                    rule="published_pdf_readable",
                    path=pdf_path,
                    url=pdf_url,
                )
            )
            return errors
        if value is None:
            errors.append(
                self._error(
                    "published PDF is missing or no readable PDF was supplied",
                    rule="published_pdf_exists",
                    path=pdf_path,
                    url=pdf_url,
                )
            )
            return errors

        try:
            body = self._read_pdf_value(value, workspace=workspace)
        except (OSError, TypeError, ValueError) as exc:
            errors.append(
                self._error(
                    f"published PDF cannot be read: {exc}",
                    rule="published_pdf_readable",
                    path=pdf_path,
                    url=pdf_url,
                )
            )
            return errors
        if body is None:
            errors.append(
                self._error(
                    "published PDF is missing or no readable PDF was supplied",
                    rule="published_pdf_exists",
                    path=pdf_path,
                    url=pdf_url,
                )
            )
            return errors
        if len(body) < self.pdf_min_bytes or not body.startswith(b"%PDF-"):
            errors.append(
                self._error(
                    "published PDF is not readable as a PDF document",
                    rule="published_pdf_readable",
                    path=pdf_path,
                    url=pdf_url,
                )
            )
        if isinstance(expected_hash, str) and _SHA256_RE.fullmatch(expected_hash):
            actual_hash = hashlib.sha256(body).hexdigest()
            if actual_hash != expected_hash.lower():
                errors.append(
                    self._error(
                        "published PDF sha256 does not match its content",
                        rule="published_pdf_hash_match",
                        path=f"{record_path}.hashes.pdf_sha256",
                        url=pdf_url,
                    )
                )
        return errors

    def _resolve_pdf(
        self,
        record_id: int | None,
        pdf_url: str,
        candidate: Any,
        *,
        pdf_resolver: PdfResolver | None,
        pdf_documents: Mapping[Any, PdfValue] | None,
        workspace: Path | None,
        pdf_root: Path | None,
    ) -> PdfValue | None:
        if pdf_resolver is not None:
            value = self._call_resolver(pdf_resolver, record_id, pdf_url)
            if value is not None:
                return value
        if pdf_documents is not None:
            value = self._mapping_value(pdf_documents, record_id, pdf_url)
            if value is not None:
                return value

        refs = self._value(candidate, "raw_artifact_refs", ()) or ()
        for ref in refs if isinstance(refs, Iterable) and not isinstance(refs, (str, bytes, Mapping)) else ():
            ref_id = self._value(ref, "enbek_id", None)
            kind = self._value(ref, "kind", None)
            kind_value = getattr(kind, "value", kind)
            if record_id is not None and ref_id != record_id:
                continue
            if kind_value not in (ArtifactKind.PDF.value, ArtifactKind.PDF):
                continue
            physical = self._value(ref, "physical_path", None)
            if physical:
                return self._path_with_workspace(physical, workspace)

        if pdf_root is not None and record_id is not None:
            for candidate_path in (pdf_root / str(record_id), pdf_root / f"{record_id}.pdf"):
                if candidate_path.is_file():
                    return candidate_path
        return None

    @staticmethod
    def _call_resolver(resolver: PdfResolver, record_id: int | None, pdf_url: str) -> Any:
        # Resolve the common two-argument, one-argument-ID and one-argument
        # record-independent resolver contracts without requiring an adapter.
        try:
            signature = inspect.signature(resolver)
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
            ]
            required = [parameter for parameter in positional if parameter.default is parameter.empty]
            if len(required) >= 2 or len(positional) >= 2:
                return resolver(record_id, pdf_url)
            if positional:
                return resolver(record_id)
            return resolver()
        except (TypeError, ValueError):
            try:
                return resolver(record_id, pdf_url)
            except TypeError:
                return resolver(record_id)

    @staticmethod
    def _mapping_value(mapping: Mapping[Any, Any], record_id: int | None, pdf_url: str) -> Any:
        for key in (record_id, str(record_id) if record_id is not None else None, pdf_url):
            if key is not None and key in mapping:
                return mapping[key]
        return None

    @staticmethod
    def _read_pdf_value(value: Any, *, workspace: Path | None) -> bytes | None:
        if isinstance(value, RawArtifact):
            value = value.physical_path
        elif isinstance(value, RawArtifactRef):
            value = value.physical_path
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        if isinstance(value, Mapping):
            for key in ("content", "body", "bytes", "data"):
                if key in value:
                    return Validator._read_pdf_value(value[key], workspace=workspace)
            for key in ("physical_path", "path"):
                if key in value:
                    return Validator._read_pdf_value(value[key], workspace=workspace)
            return None
        if hasattr(value, "content"):
            return Validator._read_pdf_value(value.content, workspace=workspace)
        if hasattr(value, "body"):
            return Validator._read_pdf_value(value.body, workspace=workspace)
        if hasattr(value, "read") and callable(value.read):
            body = value.read()
            return Validator._read_pdf_value(body, workspace=workspace)
        if isinstance(value, (str, Path)):
            path = Path(value)
            if not path.is_absolute() and workspace is not None:
                path = workspace / path
            return path.read_bytes()
        return None

    @staticmethod
    def _path_with_workspace(value: Any, workspace: Path | None) -> Path:
        path = Path(value)
        if not path.is_absolute() and workspace is not None:
            path = workspace / path
        return path

    @staticmethod
    def _value(value: Any, name: str, default: Any = None) -> Any:
        if value is None:
            return default
        if isinstance(value, Mapping):
            return value.get(name, default)
        return getattr(value, name, default)

    @staticmethod
    def _record_items(value: Any) -> list[tuple[Any, Any]]:
        if value is _MISSING or value is None:
            return []
        if isinstance(value, Mapping):
            return list(value.items())
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [(None, item) for item in value]
        return []

    @staticmethod
    def _record_payload(value: Any) -> Mapping[str, Any]:
        if isinstance(value, NormalizedRecord):
            return value.to_dict()
        if isinstance(value, Mapping):
            return value
        if hasattr(value, "to_dict") and callable(value.to_dict):
            result = value.to_dict()
            return result if isinstance(result, Mapping) else {}
        return {}

    @staticmethod
    def _mapping_key_id(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return None

    @staticmethod
    def _record_path(index: int, mapping_key: Any) -> str:
        if mapping_key is not None:
            return f"$.records[{mapping_key!r}]"
        return f"$.records[{index}]"

    @staticmethod
    def _error(
        reason: str,
        *,
        rule: str,
        path: str,
        url: str | None = None,
        invalid_fields: Iterable[str] = (),
    ) -> ValidationError:
        return ValidationError(
            reason,
            rule=rule,
            path=path,
            url=url,
            invalid_fields=tuple(invalid_fields),
            stage="validation",
        )


# Design-document spelling and functional convenience.
Validator_Service = Validator
Validation_Service = Validator


def validate(
    candidate: Any,
    current: Any = None,
    **kwargs: Any,
) -> ValidationResult:
    """Validate a candidate with a default stateless Validator."""

    return Validator(**{key: value for key, value in kwargs.items() if key in {
        "pdf_resolver", "pdf_documents", "workspace", "pdf_root", "pdf_min_bytes"
    }}).validate(candidate, current, **{
        key: value for key, value in kwargs.items() if key in {
            "pdf_resolver", "pdf_documents", "workspace", "pdf_root"
        }
    })


__all__ = [
    "DEFAULT_PDF_MIN_BYTES",
    "PdfResolver",
    "ValidationReport",
    "ValidationResult",
    "Validation_Service",
    "Validator",
    "ValidatorResult",
    "Validator_Service",
    "validate",
]
