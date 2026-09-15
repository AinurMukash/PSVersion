"""Collector for one professional-standard detail page.

The collector owns only the detail-page boundary: it requests the exact URL
from a validated :class:`RegistryEntry`, persists the received response as a
raw artifact, and delegates decoding to a pure JSON or HTML parser.  It does
not resolve or download PDF URLs; that is intentionally a separate collector.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from ..errors import MonitorError, ParseError, SourceError
from ..models import ParserMode, RawArtifact, RegistryEntry, SourceAttributes, SourceProfile
from ..network import NetworkClient, SourceResponse
from ..parsers.detail_html import DetailHtmlParser
from ..parsers.detail_json import DetailJsonParser
from ..parsers.source_schema import DetailPage
from ..storage.raw import RawArtifactStore


@dataclass(frozen=True, slots=True)
class DetailCollectionResult:
    """Typed outcome of collecting one registry entry's detail page.

    A failed result never exposes parsed attributes or a ``DetailPage``.  A
    raw artifact may still be present for a parse failure because the original
    HTTP response was successfully received and is useful for diagnosis.
    """

    registry_entry: RegistryEntry
    detail_page_url: str
    success: bool
    complete: bool
    detail_page: DetailPage | None = None
    artifact: RawArtifact | None = None
    error: MonitorError | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.registry_entry, RegistryEntry):
            raise TypeError("registry_entry must be a RegistryEntry")
        if self.detail_page_url != self.registry_entry.detail_page_url:
            raise ValueError("detail_page_url must match registry_entry.detail_page_url")
        if self.success != self.complete:
            raise ValueError("success and complete must have the same value")
        if self.success:
            if self.detail_page is None or self.artifact is None or self.error is not None:
                raise ValueError("successful detail result requires page and artifact")
        elif self.detail_page is not None or self.error is None:
            raise ValueError("failed detail result requires an error and no page")

    @property
    def enbek_id(self) -> int:
        """The registry identity associated with this result."""

        return self.registry_entry.enbek_id

    @property
    def page_url(self) -> str:
        """Exact detail-page URL associated with the registry entry."""

        return self.detail_page_url

    @property
    def attributes(self) -> SourceAttributes | None:
        """Complete parsed attributes, or ``None`` for every failure."""

        return None if self.detail_page is None else self.detail_page.attributes

    @property
    def detail(self) -> DetailPage | None:
        """Short alias for callers that use ``detail`` as the page name."""

        return self.detail_page

    @property
    def page(self) -> DetailPage | None:
        """Alias for the complete parsed detail page."""

        return self.detail_page

    @property
    def raw_artifact(self) -> RawArtifact | None:
        """Alias for the persisted raw detail response."""

        return self.artifact

    @property
    def failed(self) -> bool:
        return not self.success

    @property
    def incomplete(self) -> bool:
        return not self.complete


Detail_Collection_Result = DetailCollectionResult


class DetailCollector:
    """Fetch, persist and parse one validated registry detail page.

    ``profile`` supplies the captured parser mode when available.  If no mode
    is configured, the response ``Content-Type`` selects JSON or HTML.  A
    ``browser`` profile deliberately still selects the pure parser from the
    received response content type; browser automation is a transport concern
    and is not embedded in this collector.
    """

    def __init__(
        self,
        network_client: NetworkClient,
        raw_store: RawArtifactStore,
        *,
        profile: SourceProfile | Mapping[str, Any] | None = None,
        parser_mode: ParserMode | str | None = None,
        parser: Any | None = None,
        json_parser: Any | None = None,
        html_parser: Any | None = None,
        run_id: str | None = None,
        staging_root: str | Path | None = None,
    ) -> None:
        if not isinstance(network_client, NetworkClient):
            raise TypeError("network_client must be a NetworkClient")
        if not isinstance(raw_store, RawArtifactStore):
            raise TypeError("raw_store must be a RawArtifactStore")
        if profile is not None and not isinstance(profile, (SourceProfile, Mapping)):
            raise TypeError("profile must be a SourceProfile, mapping, or None")
        if parser is not None and (json_parser is not None or html_parser is not None):
            raise ValueError("parser cannot be combined with json_parser or html_parser")
        self.network_client = network_client
        self.raw_store = raw_store
        self.profile = profile
        self.parser_mode = parser_mode
        self.parser = parser
        self.json_parser = json_parser if json_parser is not None else DetailJsonParser()
        self.html_parser = html_parser if html_parser is not None else DetailHtmlParser()
        self.run_id = run_id
        self.staging_root = staging_root

    def collect(
        self,
        registry_entry: RegistryEntry,
        *,
        fetched_at: datetime | str | None = None,
        run_id: str | None = None,
        staging_root: str | Path | None = None,
    ) -> DetailCollectionResult:
        """Collect exactly ``registry_entry.detail_page_url``.

        The method performs no PDF request, even when the parsed detail page
        contains a PDF URL.  The parser receives the same clean
        ``SourceResponse`` returned by ``NetworkClient``; raw persistence does
        not transform or reserialize the response body.
        """

        if not isinstance(registry_entry, RegistryEntry):
            raise TypeError("registry_entry must be a validated RegistryEntry")
        url = registry_entry.detail_page_url
        response_or_error = self.network_client.request(url, expected_kind="detail")
        if isinstance(response_or_error, SourceError):
            return self._failure(registry_entry, response_or_error)
        if not isinstance(response_or_error, SourceResponse):
            return self._failure(
                registry_entry,
                SourceError(
                    "detail request returned no usable HTTP response",
                    url=url,
                    stage="detail collection",
                ),
            )

        response = response_or_error
        artifact_or_error = self._store_response(
            response,
            registry_entry,
            fetched_at=fetched_at,
            run_id=self.run_id if run_id is None else run_id,
            staging_root=self.staging_root if staging_root is None else staging_root,
        )
        if isinstance(artifact_or_error, MonitorError):
            return self._failure(registry_entry, artifact_or_error)
        artifact = artifact_or_error

        selected_parser, parser_error = self._select_parser(url, response)
        if parser_error is not None:
            return self._failure(registry_entry, parser_error, artifact=artifact)

        try:
            parsed = selected_parser.parse(url, response)
        except ParseError as error:
            return self._failure(registry_entry, error, artifact=artifact)
        if isinstance(parsed, ParseError):
            return self._failure(registry_entry, parsed, artifact=artifact)
        if not isinstance(parsed, DetailPage):
            return self._failure(
                registry_entry,
                ParseError(
                    "detail parser returned an invalid result",
                    url=url,
                    stage="detail collection",
                    path="$",
                    invalid_fields=("$",),
                ),
                artifact=artifact,
            )

        identity_error = self._validate_identity(registry_entry, parsed)
        if identity_error is not None:
            return self._failure(registry_entry, identity_error, artifact=artifact)
        return DetailCollectionResult(
            registry_entry=registry_entry,
            detail_page_url=url,
            success=True,
            complete=True,
            detail_page=parsed,
            artifact=artifact,
        )

    fetch = collect
    collect_detail = collect

    def _store_response(
        self,
        response: SourceResponse,
        registry_entry: RegistryEntry,
        *,
        fetched_at: datetime | str | None,
        run_id: str | None,
        staging_root: str | None,
    ) -> RawArtifact | MonitorError:
        try:
            return self.raw_store.store_detail(
                response,
                enbek_id=registry_entry.enbek_id,
                run_id=run_id,
                staging_root=staging_root,
                fetched_at=fetched_at,
            )
        except MonitorError as error:
            return error
        except (TypeError, ValueError, OSError) as error:
            return SourceError(
                f"could not store detail response: {error}",
                url=registry_entry.detail_page_url,
                status=response.status_code,
                stage="detail collection",
                path="$.raw_artifact",
            )

    def _select_parser(
        self,
        url: str,
        response: SourceResponse,
    ) -> tuple[Any | None, ParseError | None]:
        if self.parser is not None:
            if not callable(getattr(self.parser, "parse", None)):
                return None, ParseError(
                    "detail parser must provide parse(url, response)",
                    url=url,
                    stage="detail collection",
                    path="$.parser",
                    invalid_fields=("$.parser",),
                )
            return self.parser, None

        mode = self._profile_parser_mode()
        if mode in {"json", "json_api"}:
            return self.json_parser, None
        if mode == "html":
            return self.html_parser, None
        if mode not in {None, "browser"}:
            return None, ParseError(
                f"unsupported detail parser mode: {mode}",
                url=url,
                stage="detail collection",
                path="$.parser_mode",
                invalid_fields=("$.parser_mode",),
            )

        content_type = self._header(response.headers, "content-type")
        media_type = content_type.split(";", 1)[0].strip().casefold() if content_type else ""
        if media_type == "application/json" or media_type.endswith("+json"):
            return self.json_parser, None
        if media_type in {"text/html", "application/xhtml+xml"}:
            return self.html_parser, None
        return None, ParseError(
            "detail response Content-Type must identify JSON or HTML",
            url=url,
            status=response.status_code,
            stage="detail collection",
            path="$.headers.content-type",
            invalid_fields=("$.headers.content-type",),
        )

    def _profile_parser_mode(self) -> str | None:
        configured = self.parser_mode
        if configured is None and self.profile is not None:
            if isinstance(self.profile, SourceProfile):
                configured = self.profile.parser_mode
            else:
                configured = self.profile.get("parser_mode")
        if configured is None:
            return None
        value = getattr(configured, "value", configured)
        if not isinstance(value, str):
            return str(value).casefold()
        return value.strip().casefold()

    @staticmethod
    def _validate_identity(
        registry_entry: RegistryEntry,
        detail_page: DetailPage,
    ) -> ParseError | None:
        if detail_page.source_url != registry_entry.detail_page_url:
            return ParseError(
                "detail parser returned a page URL different from the RegistryEntry",
                url=registry_entry.detail_page_url,
                stage="detail collection",
                path="$.page_url",
                invalid_fields=("$.page_url",),
            )
        if detail_page.attributes.enbek_id != registry_entry.enbek_id:
            return ParseError(
                "detail Enbek_ID does not match the RegistryEntry",
                url=registry_entry.detail_page_url,
                stage="detail collection",
                path="$.enbek_id",
                invalid_fields=("$.enbek_id",),
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
        registry_entry: RegistryEntry,
        error: MonitorError,
        *,
        artifact: RawArtifact | None = None,
    ) -> DetailCollectionResult:
        return DetailCollectionResult(
            registry_entry=registry_entry,
            detail_page_url=registry_entry.detail_page_url,
            success=False,
            complete=False,
            artifact=artifact,
            error=error,
        )


Detail_Collector = DetailCollector

__all__ = [
    "DetailCollectionResult",
    "Detail_Collection_Result",
    "DetailCollector",
    "Detail_Collector",
]
