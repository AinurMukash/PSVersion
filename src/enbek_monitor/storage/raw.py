"""Append-only storage for registry, detail-page and PDF source artifacts.

The store is intentionally small and side-effectful only at its explicit
boundary.  Callers provide source response bytes and metadata; the store
validates the response, resolves every path through :class:`WorkspaceGuard`,
creates immutable files with exclusive creation, and appends one JSON object to
``raw/manifest.jsonl``.  It never replaces or removes an existing artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import SourceError, WorkspaceError
from ..models import ArtifactKind, RawArtifact
from ..network import SourceResponse
from .workspace import WorkspaceGuard

DEFAULT_PDF_MIN_BYTES = 1024
_MANIFEST_NAME = "manifest.jsonl"


class RawArtifactStore:
    """Persist immutable source bytes and an append-only artifact manifest.

    ``workspace`` may be an existing :class:`WorkspaceGuard` or a workspace
    path.  A path is wrapped in a guard so the same allow-list and ``.agent``
    protection applies in either form.  The store does not acquire the
    workspace run lock; orchestration owns that lock, while this class keeps
    individual manifest writes serialized within the process.
    """

    def __init__(
        self,
        workspace: WorkspaceGuard | str | Path,
        *,
        pdf_min_bytes: int = DEFAULT_PDF_MIN_BYTES,
    ) -> None:
        if isinstance(workspace, WorkspaceGuard):
            self.guard = workspace
        else:
            self.guard = WorkspaceGuard(workspace)
        if isinstance(pdf_min_bytes, bool) or not isinstance(pdf_min_bytes, int):
            raise ValueError("pdf_min_bytes must be an integer")
        if pdf_min_bytes < DEFAULT_PDF_MIN_BYTES:
            raise ValueError(
                f"pdf_min_bytes must be at least {DEFAULT_PDF_MIN_BYTES}"
            )
        self.pdf_min_bytes = pdf_min_bytes
        self.guard.preflight()
        self._manifest_lock = threading.RLock()

    @property
    def manifest_path(self) -> Path:
        """Absolute, guard-validated path to the append-only manifest."""

        return self.guard.resolve_path(self._raw_root() / _MANIFEST_NAME)

    def store_registry(
        self,
        response: SourceResponse | Any,
        *,
        run_id: str | None = None,
        staging_root: str | Path | None = None,
        fetched_at: datetime | str | None = None,
    ) -> RawArtifact:
        """Store one successful registry page response."""

        return self._store_response(
            ArtifactKind.REGISTRY,
            response,
            run_id=run_id,
            staging_root=staging_root,
            fetched_at=fetched_at,
        )

    def store_detail(
        self,
        response: SourceResponse | Any,
        *,
        enbek_id: int,
        run_id: str | None = None,
        staging_root: str | Path | None = None,
        fetched_at: datetime | str | None = None,
    ) -> RawArtifact:
        """Store one successful detail-page response for ``enbek_id``."""

        return self._store_response(
            ArtifactKind.DETAIL,
            response,
            enbek_id=enbek_id,
            run_id=run_id,
            staging_root=staging_root,
            fetched_at=fetched_at,
        )

    def store_pdf(
        self,
        response: SourceResponse | Any,
        *,
        enbek_id: int,
        fetched_at: datetime | str | None = None,
    ) -> RawArtifact:
        """Store a validated PDF response under its stable Enbek ID.

        A PDF is accepted only for HTTP 200, a body at least ``pdf_min_bytes``
        long, and a content type beginning with ``application/pdf``.  All
        checks happen before any directory or file is created.
        """

        return self._store_response(
            ArtifactKind.PDF,
            response,
            enbek_id=enbek_id,
            fetched_at=fetched_at,
        )

    def store(
        self,
        kind: ArtifactKind | str,
        source_url: str,
        content: bytes | bytearray | memoryview,
        *,
        enbek_id: int | None = None,
        http_status: int | None = 200,
        content_type: str | None = None,
        fetched_at: datetime | str | None = None,
        run_id: str | None = None,
        staging_root: str | Path | None = None,
        extension: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> RawArtifact:
        """Store raw bytes supplied explicitly by a collector.

        ``store`` is the low-level API; response-oriented callers should use
        ``store_registry``, ``store_detail`` or ``store_pdf``.  PDF validation
        is applied here as well, so a caller cannot bypass it by selecting the
        generic method.
        """

        artifact_kind = self._coerce_kind(kind)
        body = self._coerce_bytes(content)
        url = self._required_url(source_url)
        status = self._coerce_status(http_status)
        media_type = self._normalise_content_type(content_type)
        record_id = self._coerce_enbek_id(enbek_id) if enbek_id is not None else None
        received = self._timestamp(fetched_at)
        self._validate_response(
            artifact_kind,
            url=url,
            body=body,
            status=status,
            content_type=media_type,
            enbek_id=record_id,
        )
        physical, logical_key = self._choose_path(
            artifact_kind,
            body,
            record_id,
            run_id=run_id,
            staging_root=staging_root,
            received_at=received,
            extension=extension,
        )
        self._write_immutable(physical, body)
        artifact = RawArtifact(
            logical_key=logical_key,
            kind=artifact_kind,
            physical_path=self._relative_path(physical),
            source_url=url,
            fetched_at=received,
            enbek_id=record_id,
            http_status=status,
            content_type=media_type,
            byte_length=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
        self._append_manifest(artifact)
        return artifact

    # Explicit aliases make the persistence boundary convenient for collectors
    # without creating a second implementation or a second manifest format.
    save = store
    write = store
    put = store

    def append_manifest(self, artifact: RawArtifact) -> None:
        """Append an already-created artifact descriptor to the manifest.

        This is intentionally public for commit/pipeline code that may create
        an artifact through a specialized collector.  The referenced path must
        be inside the workspace and point to bytes matching the descriptor;
        otherwise no manifest line is written.
        """

        if not isinstance(artifact, RawArtifact):
            raise TypeError("artifact must be a RawArtifact")
        path = self.guard.resolve_path(artifact.physical_path)
        if not path.is_file():
            raise WorkspaceError(
                "raw artifact path does not refer to an existing file",
                workspace_root=str(self.guard.root),
                path=str(path),
                operation="append raw artifact manifest",
            )
        body = path.read_bytes()
        expected_hash = artifact.sha256
        if expected_hash is None or hashlib.sha256(body).hexdigest() != expected_hash:
            raise WorkspaceError(
                "raw artifact bytes do not match its SHA-256 metadata",
                workspace_root=str(self.guard.root),
                path=str(path),
                operation="append raw artifact manifest",
            )
        if artifact.byte_length != len(body):
            raise WorkspaceError(
                "raw artifact byte length does not match its file",
                workspace_root=str(self.guard.root),
                path=str(path),
                operation="append raw artifact manifest",
            )
        self._append_manifest(artifact)

    def _store_response(
        self,
        kind: ArtifactKind,
        response: Any,
        *,
        enbek_id: int | None = None,
        run_id: str | None = None,
        staging_root: str | Path | None = None,
        fetched_at: datetime | str | None = None,
    ) -> RawArtifact:
        url, status, headers, body = self._response_parts(response)
        content_type = self._header(headers, "content-type")
        extension = self._extension_from_content_type(content_type, kind)
        return self.store(
            kind,
            url,
            body,
            enbek_id=enbek_id,
            http_status=status,
            content_type=content_type,
            fetched_at=fetched_at,
            run_id=run_id,
            staging_root=staging_root,
            extension=extension,
            headers=headers,
        )

    @staticmethod
    def _response_parts(response: Any) -> tuple[str, int | None, Mapping[str, str], bytes]:
        if isinstance(response, SourceResponse):
            return response.url, response.status_code, response.headers, response.content
        url = getattr(response, "url", None)
        status = getattr(response, "status_code", getattr(response, "status", None))
        headers = getattr(response, "headers", {}) or {}
        body = getattr(response, "content", getattr(response, "body", None))
        if body is None:
            raise SourceError(
                "source response has no body",
                url=str(url or "unknown"),
                stage="raw storage",
            )
        return str(url or ""), status, headers, body

    def _choose_path(
        self,
        kind: ArtifactKind,
        body: bytes,
        enbek_id: int | None,
        *,
        run_id: str | None,
        staging_root: str | Path | None,
        received_at: str,
        extension: str | None,
    ) -> tuple[Path, str]:
        suffix = self._extension(extension, kind)
        digest = hashlib.sha256(body).hexdigest()
        if kind is ArtifactKind.PDF:
            if enbek_id is None:
                raise ValueError("PDF artifacts require a positive enbek_id")
            directory = self.guard.resolve_path(
                self._raw_root() / "pdf" / str(enbek_id), allow_directory=True
            )
            filename = f"{self._filename_timestamp(received_at)}_{digest}.{suffix}"
            return self.guard.resolve_path(directory / filename), f"enbek-{enbek_id}"

        chosen_run = self._path_component(run_id or "standalone", "run_id")
        if staging_root is None:
            directory = self.guard.resolve_path(
                self._raw_root()
                / ("registry" if kind is ArtifactKind.REGISTRY else "detail")
                / "runs"
                / chosen_run,
                allow_directory=True,
            )
        else:
            staging = self.guard.resolve_path(staging_root, allow_directory=True)
            directory = self.guard.resolve_path(
                staging / self._raw_root() / ("registry" if kind is ArtifactKind.REGISTRY else "detail"),
                allow_directory=True,
            )

        directory.mkdir(parents=True, exist_ok=True)
        if kind is ArtifactKind.REGISTRY:
            index = 1
            while True:
                filename = f"page-{index:06d}.{suffix}"
                candidate = self.guard.resolve_path(directory / filename)
                if not candidate.exists():
                    return candidate, f"registry-{chosen_run}-page-{index:06d}"
                index += 1
        if enbek_id is None:
            raise ValueError("detail artifacts require a positive enbek_id")
        base = str(enbek_id)
        candidate = self.guard.resolve_path(directory / f"{base}.{suffix}")
        if not candidate.exists():
            return candidate, f"detail-{chosen_run}-{enbek_id}"
        index = 2
        while True:
            candidate = self.guard.resolve_path(directory / f"{base}-{index}.{suffix}")
            if not candidate.exists():
                return candidate, f"detail-{chosen_run}-{enbek_id}-{index}"
            index += 1

    def _append_manifest(self, artifact: RawArtifact) -> None:
        path = self.manifest_path
        with self._manifest_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = (
                json.dumps(
                    artifact.to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                # A JSONL manifest is line-oriented; preserve all existing
                # bytes and only append.  A legacy file without a final LF is
                # repaired by inserting the separator before the new line.
                if path.stat().st_size:
                    with path.open("rb") as existing:
                        existing.seek(-1, os.SEEK_END)
                        if existing.read(1) != b"\n":
                            os.write(fd, b"\n")
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)

    def _write_immutable(self, path: Path, body: bytes) -> None:
        path = self.guard.resolve_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # PDF paths are content-addressed.  Re-observing the same response
            # is idempotent; a collision with different bytes is corruption.
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise WorkspaceError(
                    f"could not inspect existing raw artifact: {exc}",
                    workspace_root=str(self.guard.root),
                    path=str(path),
                    operation="write immutable raw artifact",
                ) from exc
            if existing != body:
                raise WorkspaceError(
                    "refusing to overwrite an existing raw artifact with different bytes",
                    workspace_root=str(self.guard.root),
                    path=str(path),
                    operation="write immutable raw artifact",
                )
            return
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _validate_response(
        self,
        kind: ArtifactKind,
        *,
        url: str,
        body: bytes,
        status: int | None,
        content_type: str | None,
        enbek_id: int | None,
    ) -> None:
        if status is not None and status != 200:
            raise SourceError(
                f"HTTP response status {status} is not valid for a raw artifact",
                url=url,
                status=status,
                stage="raw storage",
            )
        if kind is ArtifactKind.PDF:
            if enbek_id is None:
                raise SourceError(
                    "PDF artifact requires a positive Enbek_ID",
                    url=url,
                    stage="raw storage",
                )
            if status != 200:
                raise SourceError(
                    "PDF response must have HTTP status 200",
                    url=url,
                    status=status,
                    stage="raw storage",
                )
            if len(body) < self.pdf_min_bytes:
                raise SourceError(
                    f"PDF body is shorter than {self.pdf_min_bytes} bytes",
                    url=url,
                    status=status,
                    stage="raw storage",
                )
            if not content_type or not content_type.lower().startswith("application/pdf"):
                raise SourceError(
                    "PDF response Content-Type must start with application/pdf",
                    url=url,
                    status=status,
                    stage="raw storage",
                )
        elif kind is ArtifactKind.DETAIL and enbek_id is None:
            raise ValueError("detail artifacts require a positive enbek_id")

    @staticmethod
    def _coerce_kind(kind: ArtifactKind | str) -> ArtifactKind:
        try:
            return kind if isinstance(kind, ArtifactKind) else ArtifactKind(kind)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported raw artifact kind: {kind!r}") from exc

    @staticmethod
    def _coerce_bytes(content: bytes | bytearray | memoryview) -> bytes:
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("raw artifact content must be bytes-like")
        return bytes(content)

    @staticmethod
    def _required_url(url: Any) -> str:
        if not isinstance(url, str) or not url.strip():
            raise SourceError(
                "raw artifact source URL must be a non-empty string",
                url=str(url or "unknown"),
                stage="raw storage",
            )
        return url

    @staticmethod
    def _coerce_status(status: Any) -> int | None:
        if status is None:
            return None
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise ValueError("http_status must be an HTTP status integer")
        return status

    @staticmethod
    def _coerce_enbek_id(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("enbek_id must be a positive integer")
        return value

    @staticmethod
    def _normalise_content_type(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("content_type must be a non-empty string or None")
        return value

    @staticmethod
    def _timestamp(value: datetime | str | None) -> str:
        if value is None:
            current = datetime.now(timezone.utc)
        elif isinstance(value, datetime):
            current = value
        elif isinstance(value, str):
            try:
                current = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("fetched_at must be an ISO-8601 datetime") from exc
        else:
            raise TypeError("fetched_at must be a datetime, ISO-8601 string or None")
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        return current.astimezone(timezone.utc).replace(microsecond=0).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    @staticmethod
    def _header(headers: Mapping[str, Any], name: str) -> str | None:
        wanted = name.casefold()
        for key, value in headers.items():
            if str(key).casefold() == wanted:
                return str(value)
        return None

    @staticmethod
    def _filename_timestamp(value: str) -> str:
        """Use a UTC timestamp that is safe in Windows and POSIX filenames."""

        return value.replace(":", "-")

    @staticmethod
    def _extension_from_content_type(
        content_type: str | None, kind: ArtifactKind
    ) -> str | None:
        if kind is ArtifactKind.PDF:
            return "pdf"
        if content_type:
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type == "application/json" or media_type.endswith("+json"):
                return "json"
            if media_type in {"text/html", "application/xhtml+xml"}:
                return "html"
        return "bin"

    @staticmethod
    def _extension(extension: str | None, kind: ArtifactKind) -> str:
        if extension is None:
            return "pdf" if kind is ArtifactKind.PDF else "bin"
        value = extension.strip().lstrip(".").lower()
        if not value or any(char in value for char in "/\\\x00"):
            raise ValueError("extension must be a path-safe non-empty suffix")
        return value

    @staticmethod
    def _path_component(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        value = value.strip()
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError(f"{field_name} must be path-safe")
        return value

    def _raw_root(self) -> Path:
        configured = self.guard.storage_dirs.get("raw_dir", Path("raw"))
        return Path(configured)

    def _relative_path(self, path: Path) -> str:
        resolved = self.guard.resolve_path(path)
        return resolved.relative_to(self.guard.root).as_posix()


# Vocabulary aliases for callers using the design document's shorter names.
RawArtifactStoreError = SourceError
Raw_Artifact_Store = RawArtifactStore

__all__ = [
    "DEFAULT_PDF_MIN_BYTES",
    "RawArtifactStore",
    "Raw_Artifact_Store",
    "RawArtifactStoreError",
]
