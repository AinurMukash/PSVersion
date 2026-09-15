"""Fixture-friendly collector for official PDF documents.

The collector owns the PDF-specific boundary between :class:`NetworkClient`
and :class:`RawArtifactStore`.  It does not parse or transform PDF bytes: the
exact response body is passed to the raw store only after the required HTTP,
size and content-type checks succeed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..errors import MonitorError, SourceError
from ..models import RawArtifact
from ..network import NetworkClient, SourceResponse
from ..storage.raw import DEFAULT_PDF_MIN_BYTES, RawArtifactStore


@dataclass(frozen=True, slots=True)
class PdfCollectionResult:
    """Typed result of one PDF collection attempt.

    ``complete`` is false for every failure, making the result directly usable
    by an orchestrator when deciding whether a candidate snapshot is complete.
    A failed result never contains an artifact; only a response that passed all
    checks and was accepted by ``RawArtifactStore`` can be successful.
    """

    enbek_id: int
    pdf_url: str
    success: bool
    complete: bool
    artifact: RawArtifact | None = None
    error: MonitorError | None = None

    def __post_init__(self) -> None:
        if isinstance(self.enbek_id, bool) or not isinstance(self.enbek_id, int) or self.enbek_id <= 0:
            raise ValueError("enbek_id must be a positive integer")
        if not isinstance(self.pdf_url, str) or not self.pdf_url.strip():
            raise ValueError("pdf_url must be a non-empty string")
        if self.success != self.complete:
            raise ValueError("success and complete must have the same value")
        if self.success and (self.artifact is None or self.error is not None):
            raise ValueError("successful PDF result requires an artifact and no error")
        if not self.success and (self.artifact is not None or self.error is None):
            raise ValueError("failed PDF result requires an error and no artifact")

    @property
    def failed(self) -> bool:
        """Whether this collection failed and the candidate is incomplete."""

        return not self.success

    @property
    def incomplete(self) -> bool:
        """Alias used by candidate orchestration code."""

        return not self.complete

    @property
    def logical_key(self) -> str:
        """The stable logical key associated with this Enbek ID."""

        return f"enbek-{self.enbek_id}"


# A vocabulary alias for callers following the design document spelling.
Pdf_Collection_Result = PdfCollectionResult


class PdfCollector:
    """Obtain and persist one PDF for a positive Enbek ID.

    ``network_client`` may be backed by a real adapter or the repository's
    local ``FakeTransport``.  No live request is made by this class itself;
    all transport behavior is delegated to ``NetworkClient``.
    """

    def __init__(
        self,
        network_client: NetworkClient,
        raw_store: RawArtifactStore | None = None,
        *,
        artifact_store: RawArtifactStore | None = None,
        pdf_min_bytes: int = DEFAULT_PDF_MIN_BYTES,
    ) -> None:
        if not isinstance(network_client, NetworkClient):
            raise TypeError("network_client must be a NetworkClient")
        if raw_store is not None and artifact_store is not None and raw_store is not artifact_store:
            raise ValueError("raw_store and artifact_store must refer to the same store")
        selected_store = raw_store if raw_store is not None else artifact_store
        if not isinstance(selected_store, RawArtifactStore):
            raise TypeError("raw_store or artifact_store must be a RawArtifactStore")
        if isinstance(pdf_min_bytes, bool) or not isinstance(pdf_min_bytes, int):
            raise ValueError("pdf_min_bytes must be an integer")
        if pdf_min_bytes < DEFAULT_PDF_MIN_BYTES:
            raise ValueError(
                f"pdf_min_bytes must be at least {DEFAULT_PDF_MIN_BYTES}"
            )
        self.network_client = network_client
        self.raw_store = selected_store
        self.pdf_min_bytes = pdf_min_bytes

    @staticmethod
    def logical_key_for(enbek_id: int) -> str:
        """Return the deterministic logical PDF key for ``enbek_id``."""

        if isinstance(enbek_id, bool) or not isinstance(enbek_id, int) or enbek_id <= 0:
            raise ValueError("enbek_id must be a positive integer")
        return f"enbek-{enbek_id}"

    # Short aliases are useful to property/integration callers without adding
    # a second identity implementation.
    logical_key = logical_key_for
    storage_key = logical_key_for

    def collect(
        self,
        enbek_id: int,
        pdf_url: str,
        *,
        fetched_at: datetime | str | None = None,
    ) -> PdfCollectionResult:
        """Fetch, validate and persist the PDF associated with ``enbek_id``.

        Network errors are returned as ``SourceError`` inside the typed result.
        For an HTTP 200 response, validation happens before ``store_pdf`` is
        called, so invalid bytes cannot create a valid raw artifact or manifest
        entry.  ``RawArtifactStore`` repeats the checks as a persistence
        boundary defense.
        """

        if isinstance(enbek_id, bool) or not isinstance(enbek_id, int) or enbek_id <= 0:
            error = SourceError(
                "Enbek_ID must be a positive integer",
                url=str(pdf_url),
                stage="pdf collection",
                path="$.enbek_id",
            )
            return self._failure(enbek_id, pdf_url, error)
        if not isinstance(pdf_url, str) or not pdf_url.strip():
            error = SourceError(
                "PDF URL must be a non-empty string",
                url=str(pdf_url),
                stage="pdf collection",
                path="$.url",
            )
            return self._failure(enbek_id, str(pdf_url), error)

        response_or_error = self.network_client.request(
            pdf_url,
            expected_kind="pdf",
        )
        if isinstance(response_or_error, SourceError):
            return self._failure(enbek_id, pdf_url, response_or_error)
        if not isinstance(response_or_error, SourceResponse):
            error = SourceError(
                "PDF request returned no usable HTTP response",
                url=pdf_url,
                stage="pdf collection",
                path="$",
            )
            return self._failure(enbek_id, pdf_url, error)

        response = response_or_error
        error = self._validate_response(response, pdf_url)
        if error is not None:
            return self._failure(enbek_id, pdf_url, error)

        try:
            artifact = self.raw_store.store_pdf(
                response,
                enbek_id=enbek_id,
                fetched_at=fetched_at,
            )
        except MonitorError as exc:
            return self._failure(enbek_id, pdf_url, exc)

        # RawArtifactStore derives this key from the same positive ID.  Keep an
        # explicit check here so a future store implementation cannot silently
        # break the collector's deterministic identity contract.
        expected_key = self.logical_key_for(enbek_id)
        if artifact.logical_key != expected_key:
            error = SourceError(
                "raw PDF store returned an unexpected logical key",
                url=pdf_url,
                status=response.status_code,
                stage="pdf collection",
                path="$.logical_key",
            )
            return self._failure(enbek_id, pdf_url, error)
        return PdfCollectionResult(
            enbek_id=enbek_id,
            pdf_url=pdf_url,
            success=True,
            complete=True,
            artifact=artifact,
        )

    fetch = collect
    collect_pdf = collect

    def _validate_response(
        self,
        response: SourceResponse,
        pdf_url: str,
    ) -> SourceError | None:
        attempts = response.attempts
        if response.status_code != 200:
            return SourceError(
                "PDF response status must be 200",
                url=pdf_url,
                attempts=attempts,
                status=response.status_code,
                stage="pdf collection",
                path="$.status",
            )
        if len(response.content) < self.pdf_min_bytes:
            return SourceError(
                f"PDF body must be at least {self.pdf_min_bytes} bytes",
                url=pdf_url,
                attempts=attempts,
                status=response.status_code,
                stage="pdf collection",
                path="$.body.byte_length",
            )
        content_type = self._header(response.headers, "content-type")
        if content_type is None or not content_type.strip():
            return SourceError(
                "PDF response Content-Type is required",
                url=pdf_url,
                attempts=attempts,
                status=response.status_code,
                stage="pdf collection",
                path="$.content_type",
            )
        if not content_type.lower().startswith("application/pdf"):
            return SourceError(
                "PDF response Content-Type must start with application/pdf",
                url=pdf_url,
                attempts=attempts,
                status=response.status_code,
                stage="pdf collection",
                path="$.content_type",
            )
        return None

    @staticmethod
    def _header(headers: Any, name: str) -> str | None:
        if headers is None or not hasattr(headers, "items"):
            return None
        wanted = name.casefold()
        for key, value in headers.items():
            if str(key).casefold() == wanted:
                return str(value)
        return None

    @staticmethod
    def _failure(
        enbek_id: int,
        pdf_url: str,
        error: MonitorError,
    ) -> PdfCollectionResult:
        # Keep the result constructible even for an invalid caller ID: the
        # typed result itself still requires a valid domain identity.
        if isinstance(enbek_id, bool) or not isinstance(enbek_id, int) or enbek_id <= 0:
            raise ValueError("enbek_id must be a positive integer")
        return PdfCollectionResult(
            enbek_id=enbek_id,
            pdf_url=pdf_url if isinstance(pdf_url, str) and pdf_url else "<invalid-url>",
            success=False,
            complete=False,
            error=error,
        )


# Vocabulary aliases used by collectors and callers following the design
# document's component names.
Pdf_Collector = PdfCollector

__all__ = [
    "PdfCollectionResult",
    "Pdf_Collection_Result",
    "PdfCollector",
    "Pdf_Collector",
]
