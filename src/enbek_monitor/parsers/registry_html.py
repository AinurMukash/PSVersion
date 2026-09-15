"""Pure HTML adapter for the captured Enbek registry selector contract."""

from __future__ import annotations

from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

from ..errors import ParseError
from ..network import SourceResponse
from .registry_mapping import (
    MissingValue,
    RegistryFieldValues,
    build_registry_page,
    parse_pagination,
)
from .source_schema import (
    REGISTRY_PATHS,
    HtmlResponseWrapper,
    RegistryPage,
    ResponseFormat,
    SourceFieldState,
    SourceSchemaViolation,
    is_absolute_source_url,
)


class _RegistryHTMLExtractor(HTMLParser):
    """Extract only the stable ``main#registry``/``registry-entry`` fields."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.registry_root: dict[str, str] | None = None
        self.records: list[dict[str, str]] = []

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {
            name.casefold(): value
            for name, value in attrs
            if value is not None
        }

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self._attributes(attrs)
        if values.get("id") == "registry" and self.registry_root is None:
            self.registry_root = values
        classes = set(values.get("class", "").split())
        if "registry-entry" in classes:
            self.records.append(values)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


class RegistryHtmlParser:
    """Parse one already received HTML registry response without browser/network use."""

    format = ResponseFormat.HTML

    def parse(self, url: str, response: SourceResponse) -> RegistryPage | ParseError:
        """Return a complete page or one aggregated schema error."""

        if not isinstance(response, SourceResponse):
            return ParseError(
                "registry parser requires a SourceResponse",
                url=str(url),
                path="$",
                invalid_fields=("$",),
                stage="registry_parser",
            )
        if not isinstance(url, str) or not is_absolute_source_url(url, allow_fixture=True):
            return ParseError(
                "registry source URL must be absolute",
                url=str(url),
                path="$",
                invalid_fields=("$",),
                stage="registry_parser",
            )

        try:
            wrapped = HtmlResponseWrapper.from_response(response, source_url=url)
        except ParseError as error:
            return error

        extractor = _RegistryHTMLExtractor()
        try:
            extractor.feed(wrapped.text)
            extractor.close()
        except Exception as error:
            return ParseError(
                f"malformed HTML syntax: {error}",
                url=url,
                path="$",
                invalid_fields=("$",),
                stage="registry_parser",
            )

        violations: list[SourceSchemaViolation] = []
        root = extractor.registry_root
        if root is None:
            violations.append(
                SourceSchemaViolation(
                    "$.main#registry",
                    "registry root selector was not found",
                    SourceFieldState.ABSENT,
                )
            )
            root = {}

        pagination, pagination_violations = parse_pagination(
            source_url=url,
            next_value=root.get("data-next", MissingValue),
            next_path=REGISTRY_PATHS.html_next,
            page_value=root.get("data-page", MissingValue),
            page_path=REGISTRY_PATHS.html_page,
            page_size_value=root.get("data-page-size", MissingValue),
            page_size_path=REGISTRY_PATHS.html_page_size,
            total_value=root.get("data-total", MissingValue),
            total_path=REGISTRY_PATHS.html_total,
            html=True,
        )
        violations.extend(pagination_violations)

        def extract(record: Mapping[str, Any]) -> RegistryFieldValues:
            raw_id = record.get("data-enbek-id", MissingValue)
            if isinstance(raw_id, str) and raw_id.strip().isdigit():
                raw_id = int(raw_id.strip())
            return RegistryFieldValues(
                enbek_id=raw_id,
                detail_url=record.get("data-detail-url", MissingValue),
                business_code=record.get("data-business-code", MissingValue),
            )

        return build_registry_page(
            source_url=url,
            records=extractor.records,
            extract_record=extract,
            paths=REGISTRY_PATHS,
            response_format=ResponseFormat.HTML,
            response=response,
            pagination=pagination,
            pagination_violations=violations,
            html=True,
        )

    parse_response = parse
    __call__ = parse


HTMLRegistryParser = RegistryHtmlParser
RegistryHTMLParser = RegistryHtmlParser
RegistryParser = RegistryHtmlParser


def parse_registry_html(url: str, response: SourceResponse) -> RegistryPage | ParseError:
    """Functional convenience wrapper around :class:`RegistryHtmlParser`."""

    return RegistryHtmlParser().parse(url, response)


__all__ = [
    "HTMLRegistryParser",
    "RegistryHTMLParser",
    "RegistryHtmlParser",
    "RegistryParser",
    "parse_registry_html",
]
